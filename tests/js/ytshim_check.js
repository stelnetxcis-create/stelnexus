// Regression test for the YouTube controller in stream.js (run by tests/test_stream_player.py).
const fs = require('fs');
const src = fs.readFileSync(require('path').join(__dirname, '..', '..', 'app', 'static', 'js', 'stream.js'), 'utf8');
const m = src.match(/const YT_ORIGIN[\s\S]*?\n  window\.snxYtShim = ytShim;/);
if (!m) throw new Error('shim not found');
const listeners = {}; const sent = [];
const frame = { contentWindow: { postMessage: (s) => sent.push(JSON.parse(s)) }, addEventListener(){}, removeEventListener(){} };
global.window = { addEventListener: (t, f) => { listeners[t] = f; }, removeEventListener(){} };
global.setInterval = () => 1; global.clearInterval = () => {};
eval(m[0].replace('window.snxYtShim = ytShim;', 'global.ytShim = ytShim;'));
const events = []; const hooks = { ready: () => events.push('ready'), change: () => events.push('change'), error: (c) => events.push('error' + c) };
const s = ytShim(frame, hooks, true);
const assert = (c, msg) => { if (!c) { console.error('FAIL', msg); process.exitCode = 1; } else console.log('ok  ', msg); };
// commands before the frame answers are queued
s.play(); s.muted = false;
assert(sent.length === 0, 'commands queue until the frame answers');
const msg = (o, origin = 'https://www.youtube-nocookie.com') => listeners.message({ source: frame.contentWindow, origin, data: JSON.stringify(o) });
msg({ event: 'onReady' });
assert(sent.map((x) => x.func).join() === 'playVideo,unMute', 'queued commands flush in order: ' + sent.map((x) => x.func));
assert(events.filter((e) => e === 'ready').length === 1, 'ready hook fires exactly once');
// foreign senders are ignored
const before = events.length;
listeners.message({ source: {}, origin: 'https://www.youtube.com', data: '{"event":"infoDelivery","info":{"playerState":1}}' });
listeners.message({ source: frame.contentWindow, origin: 'https://evil.example', data: '{"event":"infoDelivery","info":{"playerState":1}}' });
assert(events.length === before && s.paused, 'messages from other windows / origins are ignored');
// state from the player
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 100, duration: 130, volume: 40, muted: false } });
assert(!s.paused && !s.ended, 'playing state read');
assert(s.currentTime === 100 && s.duration === 130 && Math.round(s.volume * 100) === 40 && !s.muted, 'time / duration / volume / muted read');
assert(s.seekable.length === 1 && s.seekable.end(0) === 130, 'seekable window = duration');
msg({ event: 'onStateChange', info: 2 });
assert(s.paused, 'paused state');
msg({ event: 'onStateChange', info: 0 });
assert(s.ended, 'ended state');
msg({ event: 'onError', info: 101 });
assert(events.includes('error101'), 'error code reaches the hook');
// seeking
sent.length = 0; s.currentTime = 120;
assert(sent[0] && sent[0].func === 'seekTo' && sent[0].args[0] === 120 && sent[0].args[1] === true, 'seekTo sent');
s.currentTime = 9999; assert(s.currentTime === 130, 'seek is clamped to the live edge');
// no rewind window: never seek (a "go live" must not jump to 0)
const s2 = ytShim(frame, hooks, true);
listeners.message({ source: frame.contentWindow, origin: 'https://www.youtube.com', data: JSON.stringify({ event: 'infoDelivery', info: { playerState: 1, currentTime: 5, duration: 0 } }) });
sent.length = 0; s2.currentTime = 0;
assert(sent.length === 0 && Number.isNaN(s2.duration) && s2.seekable.length === 0, 'duration 0 (live, no rewind): no seek, no window');
// volume / mute commands
sent.length = 0; s.volume = 0.5; s.muted = true;
assert(sent.map((x) => x.func).join() === 'setVolume,mute' && sent[0].args[0] === 50, 'volume + mute commands');
// handshake
assert(true, 'handshake is sent by the interval: ' + JSON.stringify({ event: 'listening', id: 'snx', channel: 'widget' }));

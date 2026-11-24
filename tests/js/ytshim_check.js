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
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 100, duration: 130, volume: 40, muted: true } });
assert(!s.paused && !s.ended, 'playing state read');
assert(s.currentTime === 100 && s.duration === 130, 'time / duration read');
assert(s.volume === 1 && !s.muted, 'we set mute/volume ourselves, so YouTube\'s echo of its own saved volume is ignored');
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
s.currentTime = 9999; assert(Math.abs(s.currentTime - 130) < 0.5, 'seek is clamped to the live edge');
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

// YouTube's own live-edge numbers win, a seek holds the reading, and a frozen clock is detected
const y2 = ytShim(frame, hooks, true); msg({ event: 'onReady' });
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 5, duration: 3600, progressState: { current: 3590, duration: 3600, seekableStart: 10, seekableEnd: 3598, isAtLiveHead: false } } });
assert(y2.currentTime === 3590 && y2.seekable.end(0) === 3598 && y2.seekable.start(0) === 10, 'progressState gives the real position and seekable window');
assert(y2.atLiveHead === false, 'isAtLiveHead is read');
sent.length = 0; y2.currentTime = 99999;
assert(y2.currentTime === 3598 && sent[0].func === 'seekTo' && sent[0].args[0] === 3598, 'go live seeks to the seekable end, not the raw duration');
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 3590 } });
assert(y2.currentTime === 3598, 'the old position is ignored for a moment after a seek');
assert(y2.frozen === false, 'a clock that just moved is not frozen');

// pause: the live edge keeps moving while paused, so resuming shows the gap; a clock that restarts is not "frozen"
const realNow = Date.now; let clock = 5_000_000; Date.now = () => clock;
const y3 = ytShim(frame, hooks, true); msg({ event: 'onReady' });
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 1000, duration: 1000 } }); clock += 1000;
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 1001, duration: 1001 } });
msg({ event: 'onStateChange', info: 2 }); clock += 5000;
assert(Math.round(y3.seekable.end(0) - y3.currentTime) === 5, 'paused for 5 s on a live stream = about 5 s behind');
msg({ event: 'onStateChange', info: 1 }); clock += 1000;
assert(y3.frozen === false, 'right after resuming the clock is not called frozen');
clock += 5000; assert(y3.frozen === true, 'playing but the clock never moves for 5 s = frozen');
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 1010, duration: 1011, availableQualityLevels: ['hd1080', 'hd720', 'large'], playbackQuality: 'hd720' } });
assert(y3.frozen === false, 'a moving clock is not frozen');
assert(y3.qualityLevels.join() === 'hd1080,hd720,large', 'quality levels are read');
sent.length = 0; y3.setQuality('hd720', true);
assert(sent.map((x) => x.func).join() === 'setPlaybackQualityRange,setPlaybackQuality,seekTo', 'quality commands sent, then a re-seek in place to make YouTube refill its buffer');
assert(sent[0].args.join() === 'hd720,hd720' && sent[2].args[0] === 1010, 'range is locked to the choice; re-seek stays at the same time');
sent.length = 0; y3.setQuality('hd720');
assert(sent.map((x) => x.func).join() === 'setPlaybackQualityRange,setPlaybackQuality', 'no re-seek unless the viewer picked it (page load must not jump)');
sent.length = 0; y3.setQuality('auto');
assert(sent[0].func === 'setPlaybackQualityRange' && sent[0].args.join() === 'large,hd1080' && sent[1].args[0] === 'default', 'Auto frees the locked range again');
Date.now = realNow;

// first play re-asserts our volume (YouTube applies the viewer's saved youtube.com volume as playback starts)
const y4 = ytShim(frame, hooks, true); msg({ event: 'onReady' });
y4.volume = 1; y4.muted = false; sent.length = 0;
msg({ event: 'onStateChange', info: 1 });
assert(sent.map((x) => x.func).join() === 'setVolume,unMute' && sent[0].args[0] === 100, 'volume 100% and sound are re-asserted when playback starts');
msg({ event: 'infoDelivery', info: { volume: 75, muted: false } });
assert(y4.volume === 1, 'a later 75% from YouTube does not move our 100% slider');
// a seek holds long enough that the old position cannot snap the bar back
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 500, progressState: { current: 500, duration: 520, seekableStart: 0, seekableEnd: 520 } } });
y4.currentTime = 400; Date.now = () => realNow() + 3000;
msg({ event: 'infoDelivery', info: { currentTime: 521 } });
assert(y4.currentTime === 400, 'old position is still ignored 3 s after a seek');
Date.now = realNow;

const fresh = ytShim(frame, hooks, true); msg({ event: 'infoDelivery', info: { volume: 40, muted: false } });
assert(Math.round(fresh.volume * 100) === 40 && !fresh.muted, 'before we choose, volume / muted are read from YouTube');

// jump(): a relative move needs no window numbers, even with no duration at all
const y5 = ytShim(frame, hooks, true); msg({ event: 'onReady' });
msg({ event: 'infoDelivery', info: { playerState: 1, currentTime: 700, duration: 0 } });
sent.length = 0; y5.jump(690);
assert(sent.length === 1 && sent[0].func === 'seekTo' && sent[0].args[0] === 690 && y5.currentTime === 690 && y5.reportedAt === 0, 'jump seeks without a seekable window and clears the report time');

// YouTube says when the owner switched rewinding off (seen on a real 24/7 stream: allowSeeking:false)
const y6 = ytShim(frame, hooks, true); msg({ event: 'onReady' });
assert(y6.allowSeeking === null, 'allowSeeking unknown until YouTube says');
msg({ event: 'infoDelivery', info: { playerState: 1, progressState: { current: 5906328, duration: 5909882, seekableStart: 5863147, seekableEnd: 5906331, isAtLiveHead: true, allowSeeking: false } } });
assert(y6.allowSeeking === false && y6.atLiveHead === true && y6.seekable.end(0) === 5906331, 'allowSeeking:false, live head and the real seekable end are read (duration is an hour too long)');

// quality method 2: reload the same video with a suggested quality
const frame7 = { src: 'https://www.youtube.com/embed/5FOxrzVMBbA?enablejsapi=1', contentWindow: frame.contentWindow, addEventListener(){}, removeEventListener(){} };
const y7 = ytShim(frame7, hooks, true); msg({ event: 'onReady' }); sent.length = 0;
assert(y7.videoId === '5FOxrzVMBbA' && y7.restartAt('hd720') === true && sent[0].func === 'loadVideoById' && sent[0].args[0].videoId === '5FOxrzVMBbA' && sent[0].args[0].suggestedQuality === 'hd720', 'restartAt asks YouTube to load the same video at the suggested quality');

// Regression test for the phone fixes in stream.js: time format with hours, and the 10 minute bar span.
const fs = require('fs'), path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'stream.js'), 'utf8');
const m = src.match(/const fmt = \(s\) => \{[\s\S]*?\n  \};\n  const BAR_SPAN = (\d+);/);
if (!m) throw new Error('fmt / BAR_SPAN not found');
const fmt = eval('(' + m[0].replace(/^const fmt = /, '').replace(/;\n  const BAR_SPAN = \d+;$/, '') + ')');
const BAR_SPAN = Number(m[1]);
let bad = 0;
const eq = (a, b, msg) => { if (a !== b) { console.error('FAIL', msg, '->', a, '!=', b); bad = 1; } else console.log('ok  ', msg); };
eq(fmt(0), '00:00', '0 seconds');
eq(fmt(59), '00:59', 'under a minute');
eq(fmt(600), '10:00', 'ten minutes');
eq(fmt(3599), '59:59', 'just under an hour');
eq(fmt(3600), '1:00:00', 'an hour reads 1:00:00');
eq(fmt(47575), '13:12:55', 'a 13 hour rewind window no longer reads 792:55');
eq(fmt(-5), '00:00', 'never negative');
eq(BAR_SPAN, 600, 'the progress bar covers the last 10 minutes');
// the bar scale: a tap at the far left of a 13 hour window is at most BAR_SPAN seconds behind
const span = (dvr) => Math.min(dvr, BAR_SPAN);
eq(Math.round((1 - 0) * span(47520)), 600, 'far left of the bar = 10 minutes back, not 13 hours');
eq(Math.round((1 - 0) * span(120)), 120, 'a small window keeps its own size');
process.exit(bad);

// Runs app/static/js/dock.js in a fake page (jsdom) and checks the layouts. Exit code 0 = all good.
const fs = require('fs'), path = require('path');
const { JSDOM } = require('jsdom');
const SRC = fs.readFileSync(path.join(__dirname, '..', '..', 'app', 'static', 'js', 'dock.js'), 'utf8');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
const ok = (cond, msg) => { if (!cond) { failures++; console.log('FAIL  ' + msg); } else console.log('ok    ' + msg); };

const HTML = `<!doctype html><body>
<div data-stage class="fc-stagewrap">
  <div data-vp-screen></div>
  <div class="fc-overlay" data-fc-overlay data-fp-ready="1"><button data-fc-toggle></button><div data-fc-log></div><span data-fc-unread hidden></span></div>
  <div class="fc-overlay bp" data-bp data-fp-ready="1"><button data-bp-toggle></button><b data-sl="name">Game 6</b></div>
  <div data-pbs><div class="pbs-stage"></div></div>
</div></body>`;

function make({ w, h, fine = true, memory = null, fullscreen = false, pbsH = 280 }) {
  const dom = new JSDOM(HTML, { runScripts: 'outside-only', url: 'http://localhost/' });
  const win = dom.window, doc = win.document;
  if (memory) win.localStorage.setItem('snx.dk.wide', JSON.stringify(memory));
  const dims = { w, h };
  const stage = doc.querySelector('[data-stage]'), screen = doc.querySelector('[data-vp-screen]');
  const chat = doc.querySelector('[data-fc-overlay]'), bet = doc.querySelector('[data-bp]');
  Object.defineProperty(screen, 'offsetWidth', { get: () => dims.w });
  Object.defineProperty(screen, 'offsetHeight', { get: () => dims.h });
  Object.defineProperty(doc.querySelector('.pbs-stage'), 'offsetHeight', { get: () => pbsH });
  doc.querySelector('[data-fc-toggle]').addEventListener('click', () => chat.classList.toggle('is-collapsed'));   // what floatpanel does
  doc.querySelector('[data-bp-toggle]').addEventListener('click', () => bet.classList.toggle('is-collapsed'));
  const ros = [];
  win.ResizeObserver = class { constructor(cb) { ros.push(cb); } observe() {} disconnect() {} };
  win.matchMedia = (q) => ({ matches: /hover/.test(q) ? fine : false, addEventListener() {}, removeEventListener() {} });
  win.snx = { fitScene() {} };
  if (fullscreen) stage.classList.add('is-fs');
  win.eval(SRC);
  const tab = (k) => doc.querySelector('[data-dk="' + k + '"]');
  const isOpen = (el) => !el.classList.contains('is-collapsed');
  const resize = async (nw, nh) => { dims.w = nw; dims.h = nh; ros.forEach((cb) => cb()); await sleep(60); };
  const stored = () => JSON.parse(win.localStorage.getItem('snx.dk.wide') || 'null');
  return { win, doc, stage, chat, bet, tab, isOpen, resize, stored, key: (k) => win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: k })) };
}

(async () => {
  // 1. a wide PC window: wide layout, everything open at once
  let p = make({ w: 1450, h: 800 }); await sleep(300);
  ok(p.stage.classList.contains('dk-on') && p.stage.classList.contains('dk-wide'), 'wide PC: dock is on and in the wide layout');
  ok(p.isOpen(p.chat) && p.isOpen(p.bet), 'wide PC: chat AND bet are open together by default');
  ok(p.stage.style.getPropertyValue('--dk-pbs-h') === '280px', 'wide PC: the payout card height is measured (280px)');
  ok(p.stage.classList.contains('dk-tight'), 'wide PC, 800px tall with a 280px payout card: bet slip uses the tighter spacing');
  ok(p.tab('pay') && p.tab('chat') && p.tab('bet'), 'the Payout / Chat / Bet tabs exist');

  // 2. tabs work one by one and are remembered
  p.tab('chat').click(); await sleep(60);
  ok(!p.isOpen(p.chat) && p.isOpen(p.bet), 'Chat tab closes only the chat; the bet slip stays open');
  ok(JSON.stringify(p.stored()) === JSON.stringify({ chat: false, bet: true }), 'the choice is remembered');
  p.tab('chat').click(); await sleep(60);
  ok(p.isOpen(p.chat) && p.isOpen(p.bet), 'Chat tab opens the chat again without touching the bet slip');
  p.tab('bet').click(); await sleep(60);
  ok(p.isOpen(p.chat) && !p.isOpen(p.bet), 'Bet tab closes only the bet slip');
  p.win.snx.dockOpen('bet'); await sleep(60);
  ok(p.isOpen(p.chat) && p.isOpen(p.bet), 'opening the bet slip when a game opens (dockOpen) leaves the chat open in the wide layout');
  p.doc.querySelector('[data-fc-toggle]').click(); await sleep(60);       // the chat panel's own chevron
  ok(JSON.stringify(p.stored()) === JSON.stringify({ chat: false, bet: true }), "the panel's own chevron is remembered too");
  p.key('Escape'); await sleep(30);
  ok(p.isOpen(p.bet), 'Escape does not fold the panels in the wide layout');

  // 3. a bigger screen has room: no tight spacing
  await p.resize(1920, 1000);
  ok(p.stage.classList.contains('dk-wide') && !p.stage.classList.contains('dk-tight'), '1920x1000: wide and roomy');

  // 4. memory on the next visit
  p = make({ w: 1450, h: 800, memory: { chat: false, bet: true } }); await sleep(300);
  ok(!p.isOpen(p.chat) && p.isOpen(p.bet), 'next visit: opens as the viewer left it (chat closed, bet open)');

  // 5. window shrinks to half a screen: back to the one-panel dock, nothing else changes
  p = make({ w: 1450, h: 800 }); await sleep(300);
  await p.resize(800, 500); await sleep(100);
  ok(p.stage.classList.contains('dk-on') && !p.stage.classList.contains('dk-wide'), 'half-width window: dock stays, wide layout switches off');
  ok(!p.isOpen(p.chat) && !p.isOpen(p.bet), 'half-width window: panels fold, picture first');
  p.tab('chat').click(); await sleep(60);
  p.tab('bet').click(); await sleep(60);
  ok(p.isOpen(p.bet) && !p.isOpen(p.chat), 'half-width window: opening the bet slip closes the chat (one at a time, as before)');
  p.key('Escape'); await sleep(30);
  ok(!p.isOpen(p.bet), 'half-width window: Escape still closes the open panel');
  await p.resize(1450, 800); await sleep(100);
  ok(p.stage.classList.contains('dk-wide') && p.isOpen(p.chat) && p.isOpen(p.bet), 'window widens again: wide layout returns with both panels open');

  // 6. things that must NOT get the wide layout
  p = make({ w: 800, h: 450 }); await sleep(300);
  ok(!p.stage.classList.contains('dk-wide') && !p.isOpen(p.chat) && !p.isOpen(p.bet), 'half-width PC window at start: old dock, panels folded');
  p = make({ w: 1400, h: 800, fine: false }); await sleep(300);
  ok(!p.stage.classList.contains('dk-wide') && p.stage.classList.contains('dk-on'), 'touch screen (no mouse) even when wide: old dock');
  p = make({ w: 1400, h: 380 }); await sleep(300);
  ok(!p.stage.classList.contains('dk-wide'), 'a very short picture (380px tall): old dock');
  p = make({ w: 999, h: 700 }); await sleep(300);
  ok(!p.stage.classList.contains('dk-wide'), '999px wide: just under the threshold, old dock');
  p = make({ w: 1000, h: 700 }); await sleep(300);
  ok(p.stage.classList.contains('dk-wide'), '1000px wide: wide layout starts');

  // 7. full screen on a PC
  p = make({ w: 1920, h: 1000, fullscreen: true }); await sleep(300);
  ok(p.stage.classList.contains('dk-wide') && p.isOpen(p.chat) && p.isOpen(p.bet), 'full screen on a PC: wide layout, chat and bet open together');
  p = make({ w: 700, h: 400, fullscreen: true }); await sleep(300);
  ok(p.stage.classList.contains('dk-on') && !p.stage.classList.contains('dk-wide') && !p.isOpen(p.bet), 'full screen on a small screen / phone sideways: old dock');

  console.log(failures ? failures + ' FAILED' : 'ALL PASSED');
  process.exit(failures ? 1 : 0);
})().catch((e) => { console.log('ERROR ' + e.stack); process.exit(2); });

/* Shared floating panel for the fight view (chat, bet panel, anything else that floats over the video).
   One module owns: drag (pointer + arrow keys), S/M/L size, transparency, collapse, a saved position stored as a
   fraction of the free space (so it survives resize, full screen and rotation), and "last touched comes to front".

     const fp = snx.floatPanel(overlay, {
       store: 'snx.fc.',                 // localStorage prefix: keys are alpha, size, pos, collapsed
       stage, screen,                    // the stage box and the video screen the panel travels inside
       head,                             // drag handle (the panel header)
       grip,                             // the title: double-click resets, arrows nudge
       toggle,                           // collapse button
       cog, settings,                    // optional: button + box that holds the size / alpha / reset controls
       sizeBtns, alpha, alphaOut, resetBtn,
       corner: { fx: 0, fy: 0 },         // where it sits until dragged (0..1 of the free space)
       defaultSize: () => 'm',
       defaultCollapsed: () => false,
       onCollapse(collapsed), onSize(size), onLayout()   // optional hooks
     });
     fp.collapsed(), fp.setCollapsed(bool), fp.place(), fp.reset(), fp.dragging()

   Layout sizes (offsetWidth/Height) are used, never getBoundingClientRect, so a stage that is rotated with CSS
   (the iPhone landscape fallback) still measures in its own frame; the drag deltas are rotated to match. */
(function () {
  'use strict';
  const snx = (window.snx = window.snx || {});
  let zTop = 5;
  const peers = new Set();                                  // every panel on the page: on a narrow stage only one stays open

  snx.floatPanel = function floatPanel(overlay, o) {
    const store = {
      get: (k) => { try { return localStorage.getItem(o.store + k); } catch (e) { return null; } },
      set: (k, v) => { try { localStorage.setItem(o.store + k, v); } catch (e) { /* blocked */ } },
    };
    const stage = o.stage, screenEl = o.screen || stage, head = o.head, grip = o.grip;
    const corner = o.corner || { fx: 0, fy: 0 };
    let api = null;
    const call = (fn, a) => { if (typeof fn === 'function') fn(a); };
    const collapsed = () => overlay.classList.contains('is-collapsed');
    const rotated = () => !!stage && stage.classList.contains('is-rot');

    // ---- collapse
    function setCollapsed(c, persist) {
      overlay.classList.toggle('is-collapsed', c);
      if (o.toggle) {
        o.toggle.setAttribute('aria-expanded', String(!c));
        o.toggle.setAttribute('aria-label', (c ? 'Expand ' : 'Collapse ') + (o.label || 'panel'));
      }
      call(o.onCollapse, c);
      if (!c && api && draggable() && screenEl.offsetWidth < 520) {   // two floating panels do not fit side by side on a phone
        peers.forEach((p) => { if (p !== api && !p.collapsed()) p.setCollapsed(true, false); });
      }
      clamp();
      if (persist) store.set('collapsed', c ? '1' : '0');
    }

    // ---- transparency
    function setAlpha(pct, persist) {
      pct = Math.min(100, Math.max(20, Math.round(+pct || 82)));
      overlay.style.setProperty('--fc-a', (pct / 100).toFixed(2));
      if (o.alpha) o.alpha.value = String(pct);
      if (o.alphaOut) o.alphaOut.textContent = pct + '%';
      if (persist) store.set('alpha', String(pct));
    }

    // ---- size presets (the geometry is CSS; this only records the choice)
    const sizeBtns = o.sizeBtns ? Array.from(o.sizeBtns) : [];
    function setSize(sz, persist) {
      if (!['s', 'm', 'l'].includes(sz)) sz = 'm';
      overlay.dataset.size = sz;
      sizeBtns.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.fpSize === sz || b.dataset.fcSize === sz)));
      if (persist) store.set('size', sz);
      clamp();
      call(o.onSize, sz);
    }

    // ---- position
    let pos = null;                                          // {fx, fy}: fraction of the free space, or null = corner
    try { pos = JSON.parse(store.get('pos') || 'null'); } catch (e) { pos = null; }
    const eff = () => pos || corner;
    const draggable = () => !(stage && stage.classList.contains('dk-on')) && getComputedStyle(overlay).position === 'absolute';   // the dock places panels itself
    function free() {                                        // room the panel can travel inside the video screen
      const cs = getComputedStyle(overlay);
      const padX = parseFloat(cs.left) || parseFloat(cs.right) || 12;
      const top = parseFloat(cs.top) || padX;
      return {
        w: Math.max(0, screenEl.offsetWidth - 2 * padX - overlay.offsetWidth),
        h: Math.max(0, screenEl.offsetHeight - top - padX - overlay.offsetHeight),
      };
    }
    function place() {
      if (!draggable()) { overlay.style.setProperty('--fc-x', '0px'); overlay.style.setProperty('--fc-y', '0px'); return; }
      const f = free(), p = eff();
      overlay.style.setProperty('--fc-x', Math.round(p.fx * f.w) + 'px');
      overlay.style.setProperty('--fc-y', Math.round(p.fy * f.h) + 'px');
    }
    function clamp() { requestAnimationFrame(place); }

    // ---- drag
    let drag = null;
    const front = () => { overlay.style.zIndex = String(++zTop > 60 ? (zTop = 6) : zTop); };
    overlay.addEventListener('pointerdown', front, { passive: true });
    head.addEventListener('pointerdown', (ev) => {
      if (ev.button > 0 || !draggable() || ev.target.closest('button,input,a,select')) return;
      const f = free(), p = eff();
      drag = { id: ev.pointerId, sx: ev.clientX, sy: ev.clientY, curX: p.fx * f.w, curY: p.fy * f.h, f };
      overlay.classList.add('is-dragging');
      try { head.setPointerCapture(ev.pointerId); } catch (e) { /* ignore */ }
    });
    head.addEventListener('pointermove', (ev) => {
      if (!drag || ev.pointerId !== drag.id) return;
      let dx = ev.clientX - drag.sx, dy = ev.clientY - drag.sy;
      if (rotated()) { const t = dx; dx = dy; dy = -t; }      // CSS rotate(90deg): local x runs down the screen, local y runs left
      const x = Math.min(drag.f.w, Math.max(0, drag.curX + dx));
      const y = Math.min(drag.f.h, Math.max(0, drag.curY + dy));
      pos = { fx: drag.f.w ? x / drag.f.w : 0, fy: drag.f.h ? y / drag.f.h : 0 };
      overlay.style.setProperty('--fc-x', Math.round(x) + 'px');
      overlay.style.setProperty('--fc-y', Math.round(y) + 'px');
    });
    const endDrag = (ev) => {
      if (!drag || (ev && ev.pointerId !== drag.id)) return;
      drag = null; overlay.classList.remove('is-dragging');
      if (pos) store.set('pos', JSON.stringify(pos));
    };
    head.addEventListener('pointerup', endDrag);
    head.addEventListener('pointercancel', endDrag);

    function reset() { pos = null; store.set('pos', 'null'); place(); }
    if (grip) {
      grip.addEventListener('dblclick', reset);
      grip.tabIndex = 0;
      grip.addEventListener('keydown', (ev) => {               // arrow keys nudge when the title is focused
        const d = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[ev.key];
        if (!d || !draggable()) return;
        ev.preventDefault();
        const f = free(), p = eff(), x = p.fx * f.w + d[0] * 24, y = p.fy * f.h + d[1] * 24;
        pos = { fx: f.w ? Math.min(1, Math.max(0, x / f.w)) : 0, fy: f.h ? Math.min(1, Math.max(0, y / f.h)) : 0 };
        place(); store.set('pos', JSON.stringify(pos));
      });
    }
    if (o.resetBtn) o.resetBtn.addEventListener('click', reset);

    // ---- wiring
    if (o.alpha) o.alpha.addEventListener('input', () => setAlpha(o.alpha.value, true));
    sizeBtns.forEach((b) => b.addEventListener('click', () => setSize(b.dataset.fpSize || b.dataset.fcSize, true)));
    if (o.cog && o.settings) {
      o.cog.addEventListener('click', () => {
        const open = o.settings.hidden;
        o.settings.hidden = !open; o.cog.setAttribute('aria-expanded', String(open));
      });
    }
    if (o.toggle) o.toggle.addEventListener('click', () => setCollapsed(!collapsed(), true));

    // re-fit whenever the stage changes size (window resize, full screen on/off, rotate)
    if ('ResizeObserver' in window) {
      const ro = new ResizeObserver(() => { call(o.onLayout); if (!drag) place(); });
      if (stage) ro.observe(stage);
      if (screenEl && screenEl !== stage) ro.observe(screenEl);
      ro.observe(overlay);
    }
    if (stage) stage.addEventListener('snx:fs', () => { call(o.onLayout); clamp(); });

    // ---- start-up
    setAlpha(store.get('alpha') || 82);
    setSize(store.get('size') || (o.defaultSize ? o.defaultSize() : 'm'), false);
    const saved = store.get('collapsed');
    setCollapsed(saved === null ? !!(o.defaultCollapsed && o.defaultCollapsed()) : saved === '1', false);
    place();

    api = { collapsed, setCollapsed, place, reset, dragging: () => !!drag, setSize, setAlpha, clamp };
    overlay.dataset.fpReady = '1';                            // the dock waits for this before it folds a panel
    peers.add(api);
    return api;
  };
})();

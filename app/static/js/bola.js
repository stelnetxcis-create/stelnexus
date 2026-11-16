/* Bola bet slip: shows/hides number boxes per ticket type and asks the server for a live preview.
   The server stays the source of truth; this only displays what /bola/draws/<id>/preview returns. */
(function () {
  const form = document.querySelector('[data-bola-slip]');
  if (!form) return;
  const COUNT = { pair: 2, casas3: 3, casas6: 4, casas10: 5 };
  const kind = form.querySelector('[name=kind]');
  const out = form.querySelector('[data-bola-preview]');
  const peso = (c) => '\u20B1' + (c / 100).toLocaleString('en-PH', { minimumFractionDigits: 2 });
  let timer = null;

  const host = form.querySelector('[data-bola-grid]');
  const balls = [];

  function boxes() { return Array.from(form.querySelectorAll('[data-num]')); }
  function picked() {
    return boxes().map((b) => parseInt(b.value, 10)).filter((n) => Number.isInteger(n) && n > 0);
  }
  function paint() {
    const on = new Set(picked());
    balls.forEach((b) => b.setAttribute('aria-pressed', on.has(+b.dataset.n) ? 'true' : 'false'));
  }
  function setPicked(list) {
    boxes().forEach((el, i) => { el.value = list[i] !== undefined ? String(list[i]) : ''; });
    paint();
    refresh();
  }
  function buildGrid() {
    if (!host) return;
    const max = parseInt(host.dataset.max, 10) || 40;
    for (let n = 1; n <= max; n += 1) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'bola-ball';
      b.dataset.n = String(n);
      b.textContent = String(n);
      b.setAttribute('aria-pressed', 'false');
      b.setAttribute('aria-label', 'Number ' + n);
      balls.push(b);
      host.appendChild(b);
    }
    const note = document.createElement('div');
    note.className = 'bola-grid-note';
    host.appendChild(note);
    host.addEventListener('click', (ev) => {
      const b = ev.target.closest('.bola-ball');
      if (!b) return;
      const n = +b.dataset.n;
      const cur = picked();
      const at = cur.indexOf(n);
      const limit = COUNT[kind.value] || 2;
      if (at >= 0) cur.splice(at, 1);
      else if (cur.length < limit) cur.push(n);
      else { note.textContent = 'This ticket type takes ' + limit + ' numbers. Tap one to remove it first.'; return; }
      note.textContent = '';
      setPicked(cur);
    });
  }
  function sync() {
    const n = COUNT[kind.value] || 2;
    boxes().forEach((el, i) => { el.closest('label').hidden = i >= n; if (i >= n) el.value = ''; });
    paint();
    refresh();
  }
  function refresh() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const nums = boxes().map((b) => b.value.trim()).filter(Boolean).join(',');
      const t = form.querySelector('[name=tumbok]').value || '0';
      const s = form.querySelector('[name=sahod]').value || '0';
      if (!nums) { out.textContent = ''; return; }
      const url = form.dataset.preview + '?kind=' + encodeURIComponent(kind.value) +
        '&numbers=' + encodeURIComponent(nums) + '&tumbok=' + encodeURIComponent(t) +
        '&sahod=' + encodeURIComponent(s);
      try {
        const r = await fetch(url, { credentials: 'same-origin' });
        const d = await r.json();
        if (!d.ok) { out.textContent = d.error; out.className = 'small muted'; return; }
        const rows = d.lines.map((l) => '<tr><td class="num">' + l.first + '\u2013' + l.second +
          '</td><td class="right num">' + peso(l.tumbok_payout_cents) +
          '</td><td class="right num">' + peso(l.sahod_payout_cents) + '</td></tr>').join('');
        out.className = 'small';
        out.innerHTML = '<strong>' + d.line_count + ' line(s)</strong> \u00B7 total ' + peso(d.stake_cents) +
          ' \u00B7 best possible win ' + peso(d.max_payout_cents) +
          '<table class="table"><thead><tr><th>Line</th><th class="right">If drawn in order</th>' +
          '<th class="right">If drawn reversed</th></tr></thead><tbody>' + rows + '</tbody></table>';
      } catch (e) { out.textContent = ''; }
    }, 250);
  }
  form.addEventListener('input', () => { paint(); refresh(); });
  kind.addEventListener('change', sync);
  buildGrid();
  sync();
})();

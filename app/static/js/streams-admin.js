/* Admin Panel -> Streams: "Check link" (server detects + probes, nothing is saved) and the per-source Preview. */
(function () {
  const boot = document.querySelector('[data-st-boot]');
  const form = document.querySelector('[data-st-form]');
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  if (form) {
    const btn = form.querySelector('[data-st-detect]'), out = form.querySelector('[data-st-result]');
    btn.addEventListener('click', async () => {
      const url = form.querySelector('[data-st-url]').value.trim();
      if (!url) { out.hidden = false; out.innerHTML = '<p class="muted">Paste a link first.</p>'; return; }
      btn.disabled = true; out.hidden = false; out.innerHTML = '<p class="muted">Checking…</p>';
      try {
        const body = new FormData();
        body.set('url', url); body.set('csrf_token', form.querySelector('[name=csrf_token]').value);
        const r = await fetch('/admin/streams/detect', { method: 'POST', body, headers: { Accept: 'application/json' }, credentials: 'same-origin' });
        const d = await r.json();
        if (!d.ok) { out.innerHTML = '<p class="st-bad">' + esc(d.error || 'That link cannot be used.') + '</p>'; return; }
        const mark = (v, yes, no) => (v === true ? '<li class="st-ok">' + yes + '</li>' : v === false ? '<li class="st-bad">' + no + '</li>' : '');
        const rows = [
          mark(d.reachable, 'Reachable', 'Not reachable'),
          mark(d.playable, 'Playable', 'Not playable'),
          d.live === true ? '<li class="st-ok">Looks live right now</li>' : d.live === false ? '<li>Looks like a finished video (replay)</li>' : '',
          mark(d.embeddable, 'Can be embedded', 'Embedding is blocked'),
          d.native ? '<li class="st-ok">Native player: Battle Pass rules enforced</li>' : '<li>Embed: Battle Pass rules limited</li>',
        ].join('');
        out.innerHTML = '<p><b>' + esc(d.kind_label) + ' detected.</b> ' + esc(d.label) + '</p><ul class="st-list">' + rows +
          (d.notes || []).map((n) => '<li>' + esc(n) + '</li>').join('') + '</ul>';
      } catch (e) {
        out.innerHTML = '<p class="st-bad">Could not check the link. Try again.</p>';
      } finally { btn.disabled = false; }
    });
  }

  const af = document.querySelector('[data-st-assign]');
  if (af) {
    const scopePick = af.querySelector('[data-st-scope-pick]'), modePick = af.querySelector('[data-st-mode]');
    const sync = () => {
      af.querySelectorAll('[data-st-scope]').forEach((el) => el.classList.toggle('st-hide', !el.dataset.stScope.split(' ').includes(scopePick.value)));
      af.querySelectorAll('[data-st-chainbox]').forEach((el) => el.classList.toggle('st-hide', modePick.value === 'off'));
    };
    scopePick.addEventListener('change', sync); modePick.addEventListener('change', sync); sync();

    // Preview first: show the chosen primary in the Sources list below before anything is saved.
    const prevBtn = af.querySelector('[data-st-prev-primary]'), impact = af.querySelector('[data-st-impact]');
    if (prevBtn) prevBtn.addEventListener('click', () => {
      const id = af.querySelector('[name=s1]').value;
      const b = id && document.querySelector('[data-st-preview="' + id + '"]');
      if (!b) { impact.hidden = false; impact.innerHTML = '<p class="st-bad">Choose a Primary source first.</p>'; return; }
      const row = document.querySelector('[data-st-prow="' + id + '"]');
      if (row && row.hidden) b.click();
      b.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });

    // Then take: before saving, the server says who it changes and what it replaces; that text becomes the confirmation.
    const save = af.querySelector('[data-st-save]');
    if (save) save.addEventListener('click', async (ev) => {
      ev.preventDefault();
      save.disabled = true; impact.hidden = true;
      try {
        const r = await fetch('/admin/streams/assign/impact', { method: 'POST', body: new FormData(af), headers: { Accept: 'application/json' }, credentials: 'same-origin' });
        const d = await r.json();
        if (!d.ok) { impact.hidden = false; impact.innerHTML = '<p class="st-bad">' + esc(d.error || 'Check the form.') + '</p>'; return; }
        impact.hidden = false; impact.innerHTML = '<p><b>Before you save:</b> ' + esc(d.text) + '</p>';
        af.setAttribute('data-confirm', d.text + '\n\nSave it?');
        save.disabled = false;
        af.requestSubmit(save);                                    // requestSubmit does not click again, so no loop
      } catch (e) {
        impact.hidden = false; impact.innerHTML = '<p class="st-bad">Could not check the impact. Try again.</p>';
      } finally { save.disabled = false; }
    });
  }

  function loadHls(cb) {
    if (window.Hls) return cb();
    const s = document.createElement('script');
    s.src = boot && boot.dataset.hlsUrl; s.onload = cb; s.onerror = () => cb(new Error('hls'));
    document.head.appendChild(s);
  }

  document.querySelectorAll('[data-st-preview]').forEach((b) => {
    b.addEventListener('click', () => {
      const row = document.querySelector('[data-st-prow="' + b.dataset.stPreview + '"]');
      const box = row.querySelector('[data-st-pbox]');
      if (!row.hidden) { row.hidden = true; box.replaceChildren(); return; }
      row.hidden = false; box.replaceChildren();
      const kind = b.dataset.kind;
      if (kind === 'hls' || kind === 'file') {
        const v = document.createElement('video');
        v.controls = true; v.muted = true; v.playsInline = true; box.appendChild(v);
        if (kind === 'file' || v.canPlayType('application/vnd.apple.mpegurl')) v.src = b.dataset.src;
        else loadHls((err) => {
          if (err || !window.Hls || !window.Hls.isSupported()) { box.textContent = 'This browser cannot play HLS.'; return; }
          const h = new window.Hls(); h.loadSource(b.dataset.src); h.attachMedia(v);
        });
      } else {
        const f = document.createElement('iframe');
        f.src = b.dataset.embed; f.loading = 'lazy'; f.allowFullscreen = true;
        f.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
        f.allow = 'autoplay; fullscreen; picture-in-picture'; f.referrerPolicy = 'strict-origin-when-cross-origin';
        box.appendChild(f);
      }
    });
  });
})();

/* Staff chat: poll for new messages, post without reloading. Text is always set via textContent. */
(function () {
  'use strict';
  const box = document.querySelector('[data-chat-log]');
  const form = document.querySelector('[data-chat-form]');
  if (!box || !form) return;
  let last = parseInt(box.getAttribute('data-last') || '0', 10);
  const me = box.getAttribute('data-me');

  function add(m) {
    const row = document.createElement('div');
    row.className = 'chat-msg' + (m.user === me ? ' mine' : '');
    const head = document.createElement('small');
    head.textContent = m.user + ' · ' + m.role + ' · ' + m.at;
    const body = document.createElement('div');
    body.textContent = m.body;
    row.appendChild(head); row.appendChild(body);
    box.appendChild(row);
    last = Math.max(last, m.id);
  }
  function scroll() { box.scrollTop = box.scrollHeight; }

  async function poll() {
    if (document.hidden) return;
    try {
      const data = await window.StelNexus.api('/chat/messages?after=' + last, { method: 'GET' });
      if (data.messages && data.messages.length) { data.messages.forEach(add); scroll(); }
    } catch (e) { /* offline: try again next tick */ }
  }
  scroll();
  setInterval(poll, 4000);

  form.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const field = form.querySelector('input[name="body"]');
    const body = field.value.trim();
    if (!body) return;
    field.value = '';
    try {
      await window.StelNexus.api('/chat/send', { method: 'POST', body: { body: body } });
      await poll();
    } catch (e) { field.value = body; }
  });
})();

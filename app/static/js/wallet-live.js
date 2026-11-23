/* Live credit: when credit arrives or leaves (a load, a transfer, a win, a cash-out, a bet), every place that shows
   it on the open page updates within a few seconds, with no refresh. It reuses the shared poller (ETag: an idle
   poll is a few header bytes; nothing runs while the tab is hidden; errors back off).
   Marked with [data-wallet-balance]; an element with data-cents (Bola) keeps that number current too.
   A page that changes the credit itself (the bet panel) calls snx.walletSync(cents) so the header moves at once and
   the next poll is not mistaken for someone else's change. */
(function () {
  const snx = window.snx;
  if (!snx || !snx.poller) return;
  const SEL = '[data-wallet-balance]';
  const first = document.querySelector('.wallet-chip [data-cents]');
  let last = first ? Number(first.dataset.cents) : null;
  if (!document.querySelector(SEL) && !document.querySelector('.wallet-chip')) return;   // this account holds no credit

  const paintText = (cents, text) => {
    document.querySelectorAll(SEL).forEach((el) => {
      if (el.textContent !== text) el.textContent = text;
      if ('cents' in el.dataset) el.dataset.cents = String(cents);
    });
  };
  const flash = (dir) => document.querySelectorAll('.wallet-chip').forEach((chip) => {
    chip.classList.remove('bal-up', 'bal-down'); void chip.offsetWidth;       // restart the animation
    chip.classList.add(dir === 'up' ? 'bal-up' : 'bal-down');
    setTimeout(() => chip.classList.remove('bal-up', 'bal-down'), 1800);
  });
  const pending = (n) => {
    const chip = document.querySelector('.wallet-chip');
    if (!chip) return;
    let dot = chip.querySelector('.dot');
    if (n > 0) {
      if (!dot) { dot = document.createElement('span'); dot.className = 'dot'; chip.appendChild(dot); }
      if (dot.textContent !== String(n)) dot.textContent = String(n);
    } else if (dot) dot.remove();
  };

  snx.walletLocalAt = 0;
  snx.walletSync = (cents) => {                                                // this page changed my credit itself
    snx.walletLocalAt = performance.now(); last = cents; paintText(cents, snx.money(cents));
  };

  snx.poller('/_live/wallet', (d, firstPoll, sentAt) => {
    if (sentAt < snx.walletLocalAt) return;                                    // asked before this page's own bet landed: stale
    if (typeof d.pending === 'number') pending(d.pending);
    if (d.balance_cents == null) return;
    const prev = last;
    last = d.balance_cents;
    paintText(d.balance_cents, d.balance);
    if (prev === null || prev === d.balance_cents) return;
    const up = d.balance_cents > prev;
    flash(up ? 'up' : 'down');
    if (snx.toast) snx.toast(up ? 'Credit received' : 'Credit deducted', { level: up ? 'success' : 'warning', sub: (up ? '+' : '-') + snx.money(Math.abs(d.balance_cents - prev)) });
    document.dispatchEvent(new CustomEvent('snx:wallet', { detail: { cents: d.balance_cents, delta: d.balance_cents - prev } }));
  }, 5000);
})();

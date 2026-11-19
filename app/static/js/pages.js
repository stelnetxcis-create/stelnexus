/* Page behaviour that used to sit in inline <script> tags. The CSP only allows scripts from /static,
   so inline scripts never ran. Everything here is progressive: each block no-ops if its page is absent. */
(function () {
  'use strict';

  /* Lobby: filter the event boxes by status. Delegated from the document, because the filter bar and the grid are
     replaced in place when a new event appears (StelNexus.refreshRegion); the chosen filter is re-applied after that. */
  var lobbyFilter = 'all';
  function applyLobbyFilter() {
    var bar = document.querySelector('[data-lobby-filter]');
    var grid = document.querySelector('[data-lobby]');
    if (!bar || !grid) return;
    bar.querySelectorAll('.chip-btn').forEach(function (b) { b.classList.toggle('is-on', b.getAttribute('data-f') === lobbyFilter); });
    grid.querySelectorAll('.lobby-card').forEach(function (card) {
      card.hidden = !(lobbyFilter === 'all' || card.getAttribute('data-status') === lobbyFilter);
    });
  }
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-lobby-filter] [data-f]') : null;
    if (!btn) return;
    lobbyFilter = btn.getAttribute('data-f');
    applyLobbyFilter();
  });
  document.addEventListener('snx:region', function (e) {
    if (e.detail && e.detail.name === 'lobby' && lobbyFilter !== 'all') applyLobbyFilter();
  });

  /* Create event: a venue pre-fills games / payout model / tong; ?create=1 opens the form. */
  var loc = document.getElementById('ev-location');
  if (loc) {
    var games = document.querySelector('[name=fight_count]');
    var model = document.querySelector('[name=payout_model]');
    var cut = document.querySelector('[name=house_cut_bps]');
    loc.addEventListener('change', function () {
      var o = loc.selectedOptions[0];
      if (!o || !o.value) return;
      if (o.dataset.fights) games.value = o.dataset.fights;
      model.value = o.dataset.model || '';
      cut.value = o.dataset.cut || '';
    });
    var link = document.getElementById('loc-quick-link');
    if (link) link.addEventListener('click', function (e) { e.preventDefault(); });
    if (location.search.indexOf('create=1') > -1) {
      var open = document.querySelector('[data-modal="event-modal"]');
      if (open) open.click();
    }
  }

  /* Users > Create user: an Agent or Sub Agent reports to someone. Each upline option lists
     (data-for, space separated) the account types it may take; show only the ones that fit. */
  var role = document.getElementById('new-role');
  var upline = document.getElementById('new-upline');
  if (role && upline) {
    var field = document.getElementById('upline-field');
    var sync = function () {
      var need = ['agent', 'sub_agent'].indexOf(role.value) > -1;
      var firstShown = null;
      Array.prototype.forEach.call(upline.options, function (o) {
        var ok = need && (o.getAttribute('data-for') || '').split(' ').indexOf(role.value) > -1;
        o.hidden = !ok; o.disabled = !ok;
        if (ok && !firstShown) firstShown = o;
      });
      if (field) field.hidden = !need;
      upline.disabled = !need;                       // a disabled field is not submitted
      if (need && firstShown && upline.selectedOptions[0] && upline.selectedOptions[0].disabled) firstShown.selected = true;
    };
    role.addEventListener('change', sync);
    sync();
  }
})();

/* Referral code form: show only the uplines that fit the chosen account type. Each upline option
   lists (data-for, space separated) the account types it may take. */
(function () {
  var role = document.getElementById('ref-role'), up = document.getElementById('ref-upline'),
      field = document.getElementById('ref-upline-field');
  if (!role || !up || !field) return;
  function sync() {
    var r = role.value;
    Array.prototype.forEach.call(up.options, function (o) {
      if (!o.value) return;
      var ok = (o.getAttribute('data-for') || '').split(' ').indexOf(r) > -1;
      o.hidden = !ok;
      o.disabled = !ok;
    });
    up.value = '';
    // Only the Admin sees an upline picker; a Master Agent code has none (it sits under the Admin).
    field.style.display = r === 'master' ? 'none' : '';
  }
  role.addEventListener('change', sync);
  sync();
})();

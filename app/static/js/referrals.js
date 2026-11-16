/* Referral code form (Admin): show only the uplines that fit the chosen account type. */
(function () {
  var role = document.getElementById('ref-role'), up = document.getElementById('ref-upline'),
      field = document.getElementById('ref-upline-field');
  if (!role || !up || !field) return;
  function sync() {
    var r = role.value;
    Array.prototype.forEach.call(up.options, function (o) {
      if (!o.value) return;
      var ok = o.getAttribute('data-for') === r;
      o.hidden = !ok;
      o.disabled = !ok;
    });
    up.value = '';
    field.style.display = r === 'master' ? 'none' : '';
  }
  role.addEventListener('change', sync);
  sync();
})();

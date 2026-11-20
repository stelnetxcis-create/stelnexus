/* Seller desk page (/sell): mounts the shared seller component (sellcore.js) in page mode.
   The same component runs as the bet panel inside the fight-view video (betpanel / fightview.js). */
(() => {
  "use strict";
  const root = document.getElementById("sell");
  if (!root || !window.snx || !window.snx.Seller) return;
  window.snx.Seller(root, {});
})();

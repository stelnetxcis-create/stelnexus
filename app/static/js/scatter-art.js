/* Scatter: the artwork. Every symbol is drawn here as an inline SVG <symbol>, so the game ships with no image
   files, nothing to download, and the pictures stay sharp at any size. The sprite is injected once; tiles and the
   paytable just <use href="#sx-ruby"> them. Everything is original artwork. */
(function () {
  "use strict";

  // ---- gradients --------------------------------------------------------------------------------------
  function grad(id, top, mid, bottom, rim) {
    return '<linearGradient id="' + id + '" x1="0" y1="0" x2="0.6" y2="1">' +
      '<stop offset="0" stop-color="' + top + '"/><stop offset=".5" stop-color="' + mid + '"/>' +
      '<stop offset="1" stop-color="' + bottom + '"/></linearGradient>' +
      '<linearGradient id="' + id + '-rim" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="' + rim +
      '"/><stop offset="1" stop-color="' + bottom + '"/></linearGradient>';
  }
  function radial(id, a, b, c) {
    return '<radialGradient id="' + id + '" cx=".35" cy=".3" r=".85"><stop offset="0" stop-color="' + a +
      '"/><stop offset=".55" stop-color="' + b + '"/><stop offset="1" stop-color="' + c + '"/></radialGradient>';
  }

  var defs = "<defs>" +
    grad("sxg-ruby", "#ff8fa0", "#e0213f", "#6e0a1d", "#ffd0d8") +
    grad("sxg-emerald", "#8dffb9", "#16b866", "#06532f", "#d2ffe5") +
    grad("sxg-sapphire", "#9fd0ff", "#2677e8", "#0b2a73", "#d8ecff") +
    grad("sxg-amethyst", "#e2a8ff", "#9a45e0", "#3f1275", "#f3d9ff") +
    grad("sxg-gold", "#fff2b0", "#f4c542", "#9a6410", "#fff8d6") +
    grad("sxg-silver", "#ffffff", "#c9d6ee", "#6b7fa6", "#ffffff") +
    radial("sxg-comet", "#ffffff", "#7fe9ff", "#1676c8") +
    radial("sxg-sun", "#fffbe0", "#ffd24a", "#f07a12") +
    radial("sxg-portal", "#ffffff", "#c9a0ff", "#3b1a8c") +
    radial("sxg-orb-lo", "#e4f4ff", "#4aa8ff", "#123e91") +
    radial("sxg-orb-mid", "#f6e0ff", "#c15bff", "#4a1396") +
    radial("sxg-orb-hi", "#fff6c4", "#ffb52e", "#b3240b") +
    '<linearGradient id="sxg-tail" x1="1" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#9ff0ff" stop-opacity=".95"/>' +
    '<stop offset="1" stop-color="#3a6bff" stop-opacity="0"/></linearGradient>' +
    '<filter id="sxf-soft" x="-30%" y="-30%" width="160%" height="160%"><feGaussianBlur stdDeviation="3"/></filter>' +
    "</defs>";

  // a faceted gem: silhouette + lit top facet + shaded left facet + rim + glint
  function gem(id, pts, top, shade, key, glint) {
    var gx = glint || [34, 22];
    return '<symbol id="sx-' + id + '" viewBox="0 0 100 100">' +
      '<polygon points="' + pts + '" fill="url(#sxg-' + key + ')" stroke="url(#sxg-' + key + '-rim)" stroke-width="3" stroke-linejoin="round"/>' +
      '<polygon points="' + top + '" fill="#fff" opacity=".34"/>' +
      '<polygon points="' + shade + '" fill="#000" opacity=".16"/>' +
      '<polygon points="' + pts + '" fill="none" stroke="#fff" stroke-opacity=".35" stroke-width="1" transform="translate(50 50) scale(.78) translate(-50 -50)"/>' +
      '<ellipse cx="' + gx[0] + '" cy="' + gx[1] + '" rx="9" ry="3.6" fill="#fff" opacity=".85" transform="rotate(-24 ' + gx[0] + " " + gx[1] + ')"/>' +
      "</symbol>";
  }

  // sun rays, generated so the file stays small
  var rays = "";
  for (var i = 0; i < 12; i++) {
    rays += '<polygon points="50,2 56,24 44,24" fill="url(#sxg-gold)" stroke="#fff3b0" stroke-width="1" transform="rotate(' + (i * 30) + ' 50 50)"/>';
  }

  var symbols =
    // 0 ruby: hexagon
    gem("ruby", "50,6 88,27 88,71 50,94 12,71 12,27", "50,6 88,27 50,45 12,27", "12,27 50,45 50,94 12,71", "ruby") +
    // 1 emerald: octagon (emerald cut)
    gem("emerald", "30,8 70,8 92,30 92,70 70,92 30,92 8,70 8,30", "30,8 70,8 92,30 8,30", "8,30 92,30 92,70 8,70", "emerald", [36, 18]) +
    // 2 sapphire: brilliant diamond
    gem("sapphire", "50,5 92,38 50,95 8,38", "50,5 92,38 50,48 8,38", "8,38 50,48 50,95", "sapphire", [36, 26]) +
    // 3 amethyst: rounded trillion
    gem("amethyst", "50,6 94,84 6,84", "50,6 72,45 28,45", "6,84 28,45 50,84", "amethyst", [44, 34]) +
    // 4 moon: crescent + star
    '<symbol id="sx-moon" viewBox="0 0 100 100">' +
      '<path d="M64 8 A43 43 0 1 0 64 92 A34 34 0 1 1 64 8 Z" fill="url(#sxg-silver)" stroke="#ffffff" stroke-width="2.5" stroke-linejoin="round"/>' +
      '<path d="M64 8 A43 43 0 0 0 26 24 A38 38 0 0 1 64 17 Z" fill="#fff" opacity=".75"/>' +
      '<path d="M22 60 A40 40 0 0 0 52 90 A44 44 0 0 1 22 60Z" fill="#3c4f7a" opacity=".28"/>' +
      '<polygon points="76,30 79,39 88,39 81,45 84,54 76,48 68,54 71,45 64,39 73,39" fill="url(#sxg-gold)" stroke="#fff6c8" stroke-width="1.2"/>' +
    "</symbol>" +
    // 5 comet: glowing head with a swept tail
    '<symbol id="sx-comet" viewBox="0 0 100 100">' +
      '<path d="M78 24 L8 78 L24 90 L90 38 Z" fill="url(#sxg-tail)" filter="url(#sxf-soft)"/>' +
      '<path d="M76 26 L14 76 L26 84 L84 36 Z" fill="url(#sxg-tail)"/>' +
      '<circle cx="70" cy="34" r="24" fill="url(#sxg-comet)" stroke="#e6fbff" stroke-width="2.5"/>' +
      '<ellipse cx="62" cy="25" rx="9" ry="5" fill="#fff" opacity=".85" transform="rotate(-30 62 25)"/>' +
      '<polygon points="22,22 24,28 30,28 25,32 27,38 22,34 17,38 19,32 14,28 20,28" fill="#fff" opacity=".9"/>' +
      '<polygon points="38,60 39.5,64 44,64 40.5,66.5 42,71 38,68 34,71 35.5,66.5 32,64 36.5,64" fill="#fff" opacity=".7"/>' +
    "</symbol>" +
    // 6 sun: rays + disc
    '<symbol id="sx-sun" viewBox="0 0 100 100">' + rays +
      '<circle cx="50" cy="50" r="27" fill="url(#sxg-sun)" stroke="#fff3b0" stroke-width="3"/>' +
      '<circle cx="50" cy="50" r="19" fill="none" stroke="#fff" stroke-opacity=".45" stroke-width="1.5"/>' +
      '<ellipse cx="41" cy="40" rx="9" ry="5" fill="#fff" opacity=".8" transform="rotate(-30 41 40)"/>' +
    "</symbol>" +
    // 7 crown: the top prize
    '<symbol id="sx-crown" viewBox="0 0 100 100">' +
      '<path d="M10 78 L6 30 L30 52 L50 14 L70 52 L94 30 L90 78 Z" fill="url(#sxg-gold)" stroke="#fff8d6" stroke-width="3" stroke-linejoin="round"/>' +
      '<path d="M10 78 L90 78 L88 90 L12 90 Z" fill="url(#sxg-gold)" stroke="#fff8d6" stroke-width="3" stroke-linejoin="round"/>' +
      '<path d="M30 52 L50 14 L50 78 L10 78 L6 30Z" fill="#fff" opacity=".18"/>' +
      '<circle cx="50" cy="60" r="8" fill="url(#sxg-ruby)" stroke="#ffd0d8" stroke-width="1.5"/>' +
      '<circle cx="27" cy="66" r="5" fill="url(#sxg-sapphire)" stroke="#d8ecff" stroke-width="1.2"/>' +
      '<circle cx="73" cy="66" r="5" fill="url(#sxg-emerald)" stroke="#d2ffe5" stroke-width="1.2"/>' +
      '<circle cx="6" cy="30" r="4.5" fill="#fff6c8"/><circle cx="94" cy="30" r="4.5" fill="#fff6c8"/><circle cx="50" cy="14" r="4.5" fill="#fff6c8"/>' +
      '<ellipse cx="36" cy="40" rx="8" ry="3" fill="#fff" opacity=".8" transform="rotate(-40 36 40)"/>' +
    "</symbol>" +
    // 8 scatter: the nexus portal
    '<symbol id="sx-scatter" viewBox="0 0 100 100">' +
      '<circle cx="50" cy="50" r="46" fill="#7a3cff" opacity=".35" filter="url(#sxf-soft)"/>' +
      '<circle cx="50" cy="50" r="40" fill="url(#sxg-portal)" stroke="#f1e2ff" stroke-width="3"/>' +
      '<circle cx="50" cy="50" r="32" fill="none" stroke="#6fe8ff" stroke-width="3" stroke-dasharray="14 9" stroke-linecap="round"/>' +
      '<circle cx="50" cy="50" r="24" fill="none" stroke="#ff7ad9" stroke-width="2.5" stroke-dasharray="9 7" stroke-linecap="round"/>' +
      '<polygon points="50,18 56,44 82,50 56,56 50,82 44,56 18,50 44,44" fill="#fff" stroke="#fff4b8" stroke-width="2" stroke-linejoin="round"/>' +
      '<circle cx="50" cy="50" r="5" fill="#ffe27a"/>' +
    "</symbol>" +
    // 9 multiplier orbs, one look per tier (the number is written over them by the page)
    '<symbol id="sx-orb-lo" viewBox="0 0 100 100"><circle cx="50" cy="50" r="43" fill="url(#sxg-orb-lo)" stroke="#bfe3ff" stroke-width="3"/><ellipse cx="38" cy="30" rx="15" ry="8" fill="#fff" opacity=".6" transform="rotate(-30 38 30)"/></symbol>' +
    '<symbol id="sx-orb-mid" viewBox="0 0 100 100"><circle cx="50" cy="50" r="43" fill="url(#sxg-orb-mid)" stroke="#f0c8ff" stroke-width="3"/><ellipse cx="38" cy="30" rx="15" ry="8" fill="#fff" opacity=".6" transform="rotate(-30 38 30)"/></symbol>' +
    '<symbol id="sx-orb-hi" viewBox="0 0 100 100"><circle cx="50" cy="50" r="43" fill="url(#sxg-orb-hi)" stroke="#ffe9a6" stroke-width="3"/><ellipse cx="38" cy="30" rx="15" ry="8" fill="#fff" opacity=".6" transform="rotate(-30 38 30)"/></symbol>' +
    // a coin for the win showers
    '<symbol id="sx-coin" viewBox="0 0 100 100"><circle cx="50" cy="50" r="44" fill="url(#sxg-gold)" stroke="#fff8d6" stroke-width="4"/><circle cx="50" cy="50" r="32" fill="none" stroke="#9a6410" stroke-width="3" opacity=".6"/><polygon points="50,24 57,42 76,44 61,56 66,75 50,64 34,75 39,56 24,44 43,42" fill="#fff4b8" opacity=".9"/></symbol>';

  var NAMES = ["ruby", "emerald", "sapphire", "amethyst", "moon", "comet", "sun", "crown", "scatter"];
  var COLORS = ["#ff4d6a", "#27d27c", "#4c9bff", "#c470ff", "#dfe8ff", "#6fe9ff", "#ffc83d", "#ffd75e", "#c9a0ff"];

  function orbTier(v) { return v >= 20 ? "hi" : (v >= 6 ? "mid" : "lo"); }

  function inject() {
    if (document.getElementById("sx-sprite")) return;
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("id", "sx-sprite");
    svg.setAttribute("width", "0");
    svg.setAttribute("height", "0");
    svg.setAttribute("aria-hidden", "true");
    svg.style.cssText = "position:absolute;width:0;height:0;overflow:hidden";
    svg.innerHTML = defs + symbols;
    document.body.insertBefore(svg, document.body.firstChild);
  }

  window.SCATTER_ART = { names: NAMES, colors: COLORS, orbTier: orbTier, inject: inject };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", inject);
  else inject();
})();

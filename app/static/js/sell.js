/* Seller desk. Uses the same server rules as the old "Sell ticket" form; this is only a faster front. */
(() => {
  "use strict";
  const root = document.getElementById("sell");
  if (!root || !window.snx) return;
  const { toast, Sound, poller, money, csrf, store } = window.snx;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const drawOn = root.dataset.draw === "1";
  const undoWindow = Number(root.dataset.undo);          // -1 = this account cannot undo
  const canUndo = undoWindow >= 0;

  const S = { eventId: Number(root.dataset.event), fights: [], fightId: 0, side: "", amount: "", playerId: 0, hide: false, busy: false, key: "", poll: null };
  const el = {
    event: $("#sell-event"), player: $("#sell-player"), games: $("#sell-games"), name: $("#sell-name"), state: $("#sell-state"),
    amt: $("#sell-amt span"), amtBox: $("#sell-amt"), go: $("#sell-go"), goMain: $("#go-main"), goSub: $("#go-sub"),
    split: $("#sell-split"), barM: $("#bar-m"), barW: $("#bar-w"), closed: $("#sell-closed"), closedText: $("#sell-closed-text"),
    next: $("#sell-next"), recent: $("#sell-recent"),
  };
  const fight = () => S.fights.find((f) => f.id === S.fightId);
  const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  const stateText = (f) => (f.res ? f.res.toUpperCase() : f.st.toUpperCase());

  /* ---- player: remember the last one, resolve "name · ref" back to an id through the datalist */
  const options = $$("#sell-players option");
  function resolvePlayer() {
    const hit = options.find((o) => o.value === el.player.value);
    S.playerId = hit ? Number(hit.dataset.id) : 0;
    if (hit) store.set("snx.sell.player", hit.value);
    render();
  }
  el.player.addEventListener("input", resolvePlayer);
  el.player.addEventListener("change", resolvePlayer);
  const last = store.get("snx.sell.player");
  if (last && options.some((o) => o.value === last)) { el.player.value = last; resolvePlayer(); }

  /* ---- live game state */
  function startPolling() {
    if (S.poll) S.poll.stop();
    root.classList.add("is-loading");
    S.poll = poller("/_live/event/" + S.eventId, (d, first) => {
      const before = fight();
      S.hide = d.hide;
      const prev = new Map(S.fights.map((f) => [f.id, f.st + ":" + f.res]));
      S.fights = d.fights;
      if (first || !fight()) pickDefault();
      const now = fight();
      if (!first && now && before && (before.st !== now.st || before.res !== now.res)) {
        Sound.play(now.st === "locked" ? "lock" : now.res ? "win" : "open");
        if (now.st !== "open") toast("Game " + now.n + ": betting " + (now.res ? "ended, " + now.res.toUpperCase() + " wins" : "is closed"), { level: "warning" });
      }
      // a new game opening elsewhere in the list is worth a quiet tick
      if (!first && d.fights.some((f) => f.st === "open" && prev.get(f.id) && prev.get(f.id) !== "open:" && f.id !== S.fightId)) Sound.play("tick");
      root.classList.remove("is-loading");
      render();
    }, 4000);
  }
  function pickDefault() {
    const open = S.fights.find((f) => f.st === "open") || S.fights.find((f) => f.st === "locked") || S.fights[0];
    S.fightId = open ? open.id : 0;
    S.side = "";
    S.key = "";
  }
  el.event.addEventListener("change", () => {
    S.eventId = Number(el.event.value); S.fights = []; S.fightId = 0; S.side = "";
    history.replaceState(null, "", "/sell?event_id=" + S.eventId);
    startPolling();
  });
  el.next.addEventListener("click", () => {
    const f = S.fights.find((x) => x.st === "open" && x.n > ((fight() || {}).n || 0)) || S.fights.find((x) => x.st === "open");
    if (f) { S.fightId = f.id; S.side = ""; S.key = ""; render(); }
  });

  /* ---- rendering */
  function render() {
    const f = fight();
    el.games.innerHTML = "";
    S.fights.forEach((g) => {
      const b = document.createElement("button");
      b.type = "button"; b.dataset.id = g.id; b.setAttribute("role", "option");
      b.className = "sell-g st-" + g.st + (g.res ? " res-" + g.res : "") + (g.id === S.fightId ? " is-on" : "");
      b.innerHTML = "<small>GAME</small>" + g.n;
      b.addEventListener("click", () => { S.fightId = g.id; S.side = ""; S.key = ""; render(); });
      el.games.appendChild(b);
    });
    const on = $(".is-on", el.games); if (on) on.scrollIntoView({ inline: "center", block: "nearest" });
    if (!f) { el.name.textContent = "No games yet"; el.go.disabled = true; return; }
    el.name.textContent = "Game " + f.n;
    el.state.textContent = stateText(f);
    el.state.className = "sell-state s-" + (f.res ? "settled" : f.st);
    const sellable = f.st === "open";
    const allowed = { meron: f.mode === "all" || f.mode === "meron", wala: f.mode === "all" || f.mode === "wala", draw: drawOn && f.mode === "all" };
    ["meron", "wala", "draw"].forEach((s) => {
      const b = $("#side-" + s); if (!b) return;
      b.disabled = !sellable || !allowed[s];
      b.classList.toggle("is-on", S.side === s);
      b.setAttribute("aria-pressed", String(S.side === s));
    });
    if (S.side && (!sellable || !allowed[S.side])) S.side = "";
    $("#nm-meron").textContent = f.nm; $("#nm-wala").textContent = f.nw;
    const hasPool = f.pm !== undefined && !S.hide;
    el.split.hidden = !hasPool;
    if (hasPool) {
      const t = f.pm + f.pw, m = t ? Math.round(f.pm / t * 100) : 50;
      $(".bar .m", el.split).style.width = m + "%"; $(".bar .w", el.split).style.width = (100 - m) + "%";
      el.barM.textContent = "Meron " + m + "%"; el.barW.textContent = "Wala " + (100 - m) + "%";
      $("#pl-meron").textContent = money(f.pm); $("#pl-wala").textContent = money(f.pw);
      $("#sh-meron").textContent = f.cm + (f.cm === 1 ? " ticket" : " tickets"); $("#sh-wala").textContent = f.cw + (f.cw === 1 ? " ticket" : " tickets");
    } else { ["#pl-meron", "#pl-wala", "#sh-meron", "#sh-wala"].forEach((q) => { $(q).textContent = ""; }); }
    el.closed.hidden = sellable;
    if (!sellable) {
      el.closedText.textContent = f.res ? "Game " + f.n + " is finished: " + f.res.toUpperCase() + "." : "Betting is closed on game " + f.n + ".";
      el.next.hidden = !S.fights.some((x) => x.st === "open");
    }
    el.amt.textContent = Number(S.amount || 0).toLocaleString("en-PH");
    // the big sell button says exactly what it will do, or what is still missing
    const amount = Number(S.amount || 0);
    let lack = "";
    if (!sellable) lack = "Betting closed";
    else if (!S.playerId) lack = "Choose a player";
    else if (!S.side) lack = "Tap Meron or Wala";
    else if (!amount) lack = "Enter an amount";
    el.go.className = "sell-go" + (S.side && !lack ? " " + S.side[0] : "") + (S.busy ? " is-busy" : "");
    el.go.disabled = !!lack || S.busy;
    el.goMain.textContent = lack || ("Sell " + money(amount * 100).replace(/\.00$/, "") + " on " + S.side.toUpperCase());
    el.goSub.textContent = lack ? "" : "Game " + f.n + " \u00B7 " + (S.side === "meron" ? f.nm : S.side === "wala" ? f.nw : "Draw");
  }

  /* ---- input: sides, chips, numpad, keyboard */
  root.addEventListener("click", (e) => {
    const side = e.target.closest("[data-side]");
    if (side && !side.disabled) { S.side = S.side === side.dataset.side ? "" : side.dataset.side; Sound.play("tick"); render(); return; }
    const chip = e.target.closest("[data-amt]");
    if (chip) { S.amount = chip.dataset.amt; bump(); return; }
    const key = e.target.closest("[data-key]");
    if (key) { press(key.dataset.key); }
  });
  function press(k) {
    if (k === "C") S.amount = "";
    else if (k === "back") S.amount = S.amount.slice(0, -1);
    else if (/^\d$/.test(k) && (S.amount + k).length <= 7) S.amount = (S.amount + k).replace(/^0+/, "");
    bump();
  }
  function bump() {
    Sound.play("tick");
    if (!window.snx.reduce) { el.amtBox.classList.remove("is-bump"); void el.amtBox.offsetWidth; el.amtBox.classList.add("is-bump"); }
    render();
  }
  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input,select,textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (/^\d$/.test(k)) { press(k); e.preventDefault(); }
    else if (k === "backspace") { press("back"); e.preventDefault(); }
    else if (k === "escape") { S.amount = ""; S.side = ""; render(); }
    else if (k === "m") { const b = $("#side-meron"); if (b && !b.disabled) { S.side = "meron"; render(); } }
    else if (k === "w") { const b = $("#side-wala"); if (b && !b.disabled) { S.side = "wala"; render(); } }
    else if (k === "enter" && !el.go.disabled) { sell(); e.preventDefault(); }
  });
  $("[data-sell-fullscreen]") && $("[data-sell-fullscreen]").addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen(); else document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
  });

  /* ---- sell + undo */
  async function api(url, body) {
    const r = await fetch(url, { method: "POST", credentials: "same-origin", body,
      headers: { Accept: "application/json", "X-CSRF-Token": csrf } });
    let data = {}; try { data = await r.json(); } catch (e) { /* empty */ }
    if (!r.ok) { const err = new Error(data.detail || "Something went wrong. Please try again."); err.status = r.status; throw err; }
    return data;
  }
  async function sell() {
    const f = fight();
    if (S.busy || !f || !S.side || !S.playerId || !Number(S.amount)) return;
    S.busy = true; render();
    S.key = S.key || newKey();                       // same key on a retry, so a double-tap can never sell twice
    const body = new FormData();
    body.set("fight_id", f.id); body.set("side", S.side); body.set("amount", S.amount);
    body.set("player_id", S.playerId); body.set("idempotency_key", S.key); body.set("csrf_token", csrf);
    try {
      const d = await api("/sell/place", body);
      S.key = ""; S.amount = "";
      addRecent(d.ticket);
      toast("Ticket " + d.ticket.code + " sold", { level: "success", sub: money(d.ticket.stake) + " on " + d.ticket.side.toUpperCase() + " \u00B7 Game " + d.ticket.fight });
    } catch (err) {
      if (err.status && err.status < 500) S.key = "";   // a rejected sale is final; a network blip keeps its key
      toast(err.message, { level: "danger" });
    } finally { S.busy = false; render(); }
  }
  el.go.addEventListener("click", sell);

  function addRecent(t) {
    const first = el.recent.firstElementChild;
    if (first && !first.dataset.code) first.remove();
    const li = document.createElement("li");
    li.dataset.code = t.code;
    const when = new Date().toLocaleTimeString("en-PH", { hour: "2-digit", minute: "2-digit" });
    li.innerHTML = '<span class="code"></span><div><b></b><div class="meta"></div></div><span class="act"></span>';
    $(".code", li).textContent = t.code;
    $("b", li).textContent = money(t.stake) + " \u00B7 " + t.side.toUpperCase();
    $(".meta", li).textContent = "Game " + t.fight + " \u00B7 " + when;
    const act = $(".act", li);
    const link = document.createElement("a");
    link.href = "/tickets/" + t.id; link.target = "_blank"; link.rel = "noopener"; link.className = "btn btn-sm btn-ghost"; link.textContent = "Open";
    act.appendChild(link);
    if (canUndo) {
      const b = document.createElement("button");
      b.type = "button"; b.className = "undo";
      const stop = Date.now() + Math.max(undoWindow, 10) * 1000;
      const tick = () => {
        const left = Math.ceil((stop - Date.now()) / 1000);
        if (left <= 0) { b.disabled = true; b.textContent = "Undo"; clearInterval(iv); return; }
        b.textContent = "Undo " + left + "s";
      };
      const iv = setInterval(() => { if (!b.isConnected) clearInterval(iv); else tick(); }, 500); tick();
      b.addEventListener("click", async () => {
        b.disabled = true;
        try {
          await api("/sell/undo/" + t.id, new FormData());
          li.classList.add("is-undone"); b.textContent = "Undone"; clearInterval(iv);
          toast("Ticket " + t.code + " cancelled", { level: "info", sub: "Stake refunded to the player" });
        } catch (err) { b.disabled = false; toast(err.message, { level: "danger" }); }
      });
      act.appendChild(b);
    }
    el.recent.prepend(li);
    while (el.recent.children.length > 6) el.recent.lastElementChild.remove();
  }

  startPolling();
})();

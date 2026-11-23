/* Bet component. Uses the same server rules as /bets/place; this is only a
   faster front. It is mounted twice:
     - page mode:  a standalone page with its own event picker and games row (no page uses it now).
     - panel mode: the bet panel floating over the fight-view video. The host (fightview.js) feeds it the event's
                   games (setFights) and tells it which game is on screen (select); keys only count while the panel has focus.
   Every element is found by data-sl="name" inside the mount point, so the same markup can live anywhere. */
(() => {
  "use strict";
  if (!window.snx) return;
  const { toast, Sound, poller, money, csrf, store } = window.snx;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  window.snx.Seller = function Seller(root, opts) {
  opts = opts || {};
  const panelMode = !!opts.panel;
  const sl = (name) => root.querySelector('[data-sl="' + name + '"]');
  // endpoints: the livestream bet panel sets data-place-url / data-undo-url on its root
  const PLACE_URL = root.dataset.placeUrl || "/bets/live/place", UNDO_URL = root.dataset.undoUrl || "/bets/live/undo/";
  const SELF = root.dataset.self === "1";          // the logged-in account bets for itself: no player to choose
  const drawOn = root.dataset.draw === "1";
  const undoWindow = Number(root.dataset.undo);          // -1 = this account cannot undo
  const canUndo = undoWindow >= 0;

  const S = { eventId: Number(root.dataset.event), fights: [], fightId: 0, side: "", amount: "", stack: [], last: 0, playerId: 0, hide: false, busy: false, key: "", poll: null };
  const el = {
    event: sl("event"), player: sl("player"), games: sl("games"), name: sl("name"), state: sl("state"),
    amt: $("span", sl("amt")), amtBox: sl("amt"), go: sl("go"), goMain: sl("go-main"), goSub: sl("go-sub"),
    split: sl("split"), barM: sl("bar-m"), barW: sl("bar-w"), closed: sl("closed"), closedText: sl("closed-text"),
    next: sl("next"), recent: sl("recent"), stack: sl("stack"), clear: root.querySelector("[data-clear]"),
  };
  const fight = () => S.fights.find((f) => f.id === S.fightId);
  const newKey = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  const stateText = (f) => (f.res ? f.res.toUpperCase() : f.st.toUpperCase());

  /* ---- player: remember the last one, resolve "name · ref" back to an id through the datalist */
  const options = sl("players") ? $$("option", sl("players")) : [];
  function resolvePlayer() {
    const hit = options.find((o) => o.value === el.player.value);
    S.playerId = hit ? Number(hit.dataset.id) : 0;
    if (hit) store.set("snx.sell.player", hit.value);
    render();
  }
  if (el.player) { el.player.addEventListener("input", resolvePlayer); el.player.addEventListener("change", resolvePlayer); }
  const last = store.get("snx.sell.player");
  if (el.player && last && options.some((o) => o.value === last)) { el.player.value = last; resolvePlayer(); }

  /* ---- live game state */
  function startPolling() {
    if (panelMode) return;                              // the fight view feeds the panel (setFights)
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
  if (el.event) el.event.addEventListener("change", () => {
    S.eventId = Number(el.event.value); S.fights = []; S.fightId = 0; S.side = "";
    history.replaceState(null, "", "/sell?event_id=" + S.eventId);
    startPolling();
  });
  if (el.next) el.next.addEventListener("click", () => {
    const f = S.fights.find((x) => x.st === "open" && x.n > ((fight() || {}).n || 0)) || S.fights.find((x) => x.st === "open");
    if (!f) return;
    if (panelMode && opts.onPick) { opts.onPick(f.id); return; }     // the fight view switches the video too
    S.fightId = f.id; S.side = ""; S.key = ""; render();
  });

  /* ---- rendering */
  function render() {
    const f = fight();
    if (el.games) el.games.innerHTML = "";
    if (el.games) S.fights.forEach((g) => {
      const b = document.createElement("button");
      b.type = "button"; b.dataset.id = g.id; b.setAttribute("role", "option");
      b.className = "sell-g st-" + g.st + (g.res ? " res-" + g.res : "") + (g.id === S.fightId ? " is-on" : "");
      b.innerHTML = "<small>GAME</small>" + g.n;
      b.addEventListener("click", () => { S.fightId = g.id; S.side = ""; S.key = ""; render(); });
      el.games.appendChild(b);
    });
    const on = el.games && $(".is-on", el.games); if (on) on.scrollIntoView({ inline: "center", block: "nearest" });
    if (!f) { el.name.textContent = "No games yet"; el.go.disabled = true; el.goMain.textContent = "No game selected"; el.goSub.textContent = ""; return; }
    el.name.textContent = "Game " + f.n;
    el.state.textContent = stateText(f);
    el.state.className = "sell-state s-" + (f.res ? "settled" : f.st);
    const sellable = f.st === "open" && !S.live;
    const allowed = { meron: f.mode === "all" || f.mode === "meron", wala: f.mode === "all" || f.mode === "wala", draw: drawOn && f.mode === "all" };
    ["meron", "wala", "draw"].forEach((s) => {
      const b = sl("side-" + s); if (!b) return;
      b.disabled = !sellable || !allowed[s];
      b.classList.toggle("is-on", S.side === s);
      b.setAttribute("aria-pressed", String(S.side === s));
    });
    if (S.side && (!sellable || !allowed[S.side])) S.side = "";
    sl("nm-meron").textContent = f.nm; sl("nm-wala").textContent = f.nw;
    const hasPool = f.pm !== undefined && !S.hide;
    el.split.hidden = !hasPool;
    if (hasPool) {
      const t = f.pm + f.pw, m = t ? Math.round(f.pm / t * 100) : 50;
      $(".bar .m", el.split).style.width = m + "%"; $(".bar .w", el.split).style.width = (100 - m) + "%";
      el.barM.textContent = "Meron " + m + "%"; el.barW.textContent = "Wala " + (100 - m) + "%";
      sl("pl-meron").textContent = money(f.pm); sl("pl-wala").textContent = money(f.pw);
      sl("sh-meron").textContent = f.cm + (f.cm === 1 ? " ticket" : " tickets"); sl("sh-wala").textContent = f.cw + (f.cw === 1 ? " ticket" : " tickets");
    } else { ["pl-meron", "pl-wala", "sh-meron", "sh-wala"].forEach((q) => { sl(q).textContent = ""; }); }
    el.closed.hidden = sellable;
    if (!sellable) {
      el.closedText.textContent = S.live ? "Betting is closed: this game has started." : f.res ? "Game " + f.n + " is finished: " + f.res.toUpperCase() + "." : "Betting is closed on game " + f.n + ".";
      if (el.next) el.next.hidden = !S.fights.some((x) => x.st === "open");
    }
    el.amt.textContent = Number(S.amount || 0).toLocaleString("en-PH");
    renderStack();
    // the big sell button says exactly what it will do, or what is still missing
    const amount = Number(S.amount || 0);
    let lack = "";
    if (!sellable) lack = S.live ? "Stream started" : "Betting closed";
    else if (!S.playerId && !SELF) lack = "Choose a player";
    else if (!S.side) lack = "Tap Meron or Wala";
    else if (!amount) lack = "Enter an amount";
    el.go.className = "sell-go" + (S.side && !lack ? " " + S.side[0] : "") + (S.busy ? " is-busy" : "");
    el.go.disabled = !!lack || S.busy;
    el.goMain.textContent = lack || ((root.dataset.verb || "Sell") + " " + money(amount * 100).replace(/\.00$/, "") + " on " + S.side.toUpperCase());
    el.goSub.textContent = lack ? "" : "Game " + f.n + " \u00B7 " + (S.side === "meron" ? f.nm : S.side === "wala" ? f.nw : "Draw");
  }

  /* ---- amount: chips ADD up. The taps are kept as a stack of tokens so one can be taken back out; typing on the
     keypad replaces the stack with the typed number. With nothing entered, the row offers to repeat the last bet. */
  const MAXAMT = 9999999;
  const fromStack = () => { const n = S.stack.reduce((a, b) => a + b, 0); S.amount = n ? String(n) : ""; };
  function addChip(v) {
    if (!S.stack.length && Number(S.amount)) S.stack = [Number(S.amount)];     // a typed number becomes the first token
    if (S.stack.reduce((a, b) => a + b, 0) + v > MAXAMT) { toast("That is the largest amount you can bet at once.", { level: "warning" }); return; }
    S.stack.push(v); fromStack(); bump();
  }
  function resetAmount() { S.amount = ""; S.stack = []; }
  function renderStack() {
    const box = el.stack, amount = Number(S.amount || 0);
    if (el.clear) el.clear.hidden = !amount;
    if (!box) return;
    box.textContent = "";
    const mk = (cls, text, attr, val) => {
      const b = document.createElement("button"); b.type = "button"; b.className = cls; b.textContent = text; b.setAttribute(attr, val); return b;
    };
    if (S.stack.length) {
      const groups = [];
      S.stack.forEach((v) => { const g = groups.find((x) => x.v === v); if (g) g.n += 1; else groups.push({ v, n: 1 }); });
      groups.forEach((g) => {
        const t = mk("amt-tok", "\u20B1" + g.v.toLocaleString("en-PH") + (g.n > 1 ? " \u00D7" + g.n : ""), "data-tok", String(g.v));
        t.title = "Tap to take one off"; box.appendChild(t);
      });
    } else if (!amount && S.last) {
      box.appendChild(mk("amt-repeat", "\u21BB Repeat last \u20B1" + S.last.toLocaleString("en-PH"), "data-repeat", String(S.last)));
    }
    box.hidden = !box.childNodes.length;
  }

  /* ---- input: sides, chips, numpad, keyboard */
  root.addEventListener("click", (e) => {
    const side = e.target.closest("[data-side]");
    if (side && !side.disabled) { S.side = S.side === side.dataset.side ? "" : side.dataset.side; Sound.play("tick"); render(); return; }
    const chip = e.target.closest("[data-amt]");
    if (chip) { addChip(Number(chip.dataset.amt) || 0); return; }
    if (e.target.closest("[data-clear]")) { resetAmount(); bump(); return; }
    const tok = e.target.closest("[data-tok]");
    if (tok) {                                                 // take one of that value back out
      const i = S.stack.lastIndexOf(Number(tok.dataset.tok));
      if (i >= 0) { S.stack.splice(i, 1); fromStack(); bump(); }
      return;
    }
    const again = e.target.closest("[data-repeat]");
    if (again) { S.stack = [Number(again.dataset.repeat)]; fromStack(); bump(); return; }
    const key = e.target.closest("[data-key]");
    if (key) { press(key.dataset.key); }
  });
  function press(k) {
    if (k === "C") resetAmount();
    else if (k === "back") { S.stack = []; S.amount = S.amount.slice(0, -1); }
    else if (/^\d$/.test(k) && (S.amount + k).length <= 7) { S.stack = []; S.amount = (S.amount + k).replace(/^0+/, ""); }
    bump();
  }
  function bump() {
    Sound.play("tick");
    if (!window.snx.reduce) { el.amtBox.classList.remove("is-bump"); void el.amtBox.offsetWidth; el.amtBox.classList.add("is-bump"); }
    render();
  }
  // page: anywhere on the page. panel: only while focus is inside it, so digits typed elsewhere (chat) never reach the keypad.
  (panelMode ? root : document).addEventListener("keydown", (e) => {
    if (e.target.closest("input,select,textarea") || e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    if (/^\d$/.test(k)) { press(k); e.preventDefault(); }
    else if (k === "backspace") { press("back"); e.preventDefault(); }
    else if (k === "escape") { resetAmount(); S.side = ""; render(); }
    else if (k === "m") { const b = sl("side-meron"); if (b && !b.disabled) { S.side = "meron"; render(); } }
    else if (k === "w") { const b = sl("side-wala"); if (b && !b.disabled) { S.side = "wala"; render(); } }
    else if (k === "enter" && !el.go.disabled) { sell(); e.preventDefault(); }
  });
  const fsBtn = !panelMode && $("[data-sell-fullscreen]");
  if (fsBtn) fsBtn.addEventListener("click", () => {
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
  // the panel shows the bettor's own credit; the server sends the new balance (cents) with every bet / undo
  function showBalance(cents) {
    const b = sl("balance");
    if (b && typeof cents === "number") b.textContent = money(cents);
  }
  // the server sends the game's fresh pools / live multiples with every bet and undo; the page repaints from it at once
  function pushRow(row) { if (row) document.dispatchEvent(new CustomEvent("snx:live-row", { detail: row })); }
  async function sell() {
    const f = fight();
    if (S.busy || !f || !S.side || (!S.playerId && !SELF) || !Number(S.amount)) return;
    S.busy = true; render();
    S.key = S.key || newKey();                       // same key on a retry, so a double-tap can never sell twice
    const body = new FormData();
    body.set("fight_id", f.id); body.set("side", S.side); body.set("amount", S.amount);
    if (S.playerId) body.set("player_id", S.playerId);
    body.set("idempotency_key", S.key); body.set("csrf_token", csrf);
    try {
      const d = await api(PLACE_URL, body);
      S.last = Number(S.amount) || S.last; S.key = ""; resetAmount();
      showBalance(d.balance);
      pushRow(d.row);
      addRecent(d.ticket);
      toast("Ticket " + d.ticket.code + (root.dataset.verb ? " placed" : " sold"), { level: "success", sub: money(d.ticket.stake) + " on " + d.ticket.side.toUpperCase() + " \u00B7 Game " + d.ticket.fight });
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
          const u = await api(UNDO_URL + t.id, new FormData());
          showBalance(u.balance);
          pushRow(u.row);
          li.classList.add("is-undone"); b.textContent = "Undone"; clearInterval(iv);
          toast("Ticket " + t.code + " cancelled", { level: "info", sub: "Stake refunded to your credit" });
        } catch (err) { b.disabled = false; toast(err.message, { level: "danger" }); }
      });
      act.appendChild(b);
    }
    el.recent.prepend(li);
    while (el.recent.children.length > 6) el.recent.lastElementChild.remove();
  }

  /* ---- host API (panel mode) */
  function setFights(list, hide) {
    const before = fight();
    S.hide = !!hide; S.fights = list;
    // a game that has just closed or been declared: drop a half-made selection, the buttons are disabled anyway
    if (before && fight() && (before.st !== fight().st || before.res !== fight().res)) { S.key = ""; }
    root.classList.remove("is-loading");
    render();
  }
  function setLive(on) { if (S.live === !!on) return; S.live = !!on; if (S.live) { S.side = ""; S.key = ""; resetAmount(); } render(); }
  function select(id) {
    if (S.fightId === id) return;
    S.fightId = id; S.side = ""; S.key = ""; render();
  }

  startPolling();
  render();
  return { setFights, select, setLive, fight, state: S };
  };
})();

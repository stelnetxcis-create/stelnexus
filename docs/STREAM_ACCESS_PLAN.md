# Stream access by credit: plan

Status: IMPLEMENTED. The rule ships switched OFF (Admin Panel -> Variables -> Streaming -> "Need credit to watch the stream"). All open questions are answered (see "Decisions" at the bottom).
Written against the current StelNexus build.

## The rule, in one paragraph

A viewer must have credit to watch any stream. If someone is watching and betting and their credit drops too low
to place another bet, they keep watching **until the fight they bet on is finished** (declared or void). Only then
does a **top-up screen** replace the picture. Once they have credit again, the stream resumes by itself.

## Words used in this plan

| Term | Meaning |
|---|---|
| Enough credit | Wallet balance is at least the **stream minimum** (default: the minimum bet, `payout.min_stake_cents`, currently PHP 20) |
| Low credit | Balance is below the stream minimum |
| Pending fight | The fight the viewer has an **active ticket** on and that is not yet `ended` (see `events.stream_phase`) |
| Gate | The server's decision on whether this viewer may receive the video right now |
| Exempt | Accounts that must watch to do their job: Staff (Declarator, Teller) and Dev (Pawn, Bishop, Admin) |

## Who is gated

| Branch | Gated? |
|---|---|
| Users: player, sub agent, agent, master | **Yes, all four tiers** (confirmed), whenever they hold a wallet |
| Staff: declarator, teller | No (they run the event) |
| Dev: pawn, bishop, admin | No, including the DEV "view as player" preview (preview shows the gate state read-only) |

## What the viewer sees

| Situation | Result |
|---|---|
| Enough credit | Stream plays as today |
| Low credit, no pending fight (e.g. just opened the page, or lost the last fight) | **Locked.** No video is sent. Card: "Add credit to watch" with a Top up button |
| Low credit **because** they just bet, pending fight exists | **Grace.** Stream keeps playing. Small banner: "Your credit is low. You can watch until this fight ends." |
| Pending fight ends (settled or void), balance still low | Result and payout are shown first, then the **top-up screen** over the picture before the next fight |
| Pending fight ends, a win lifts balance above the minimum | No prompt. Keeps watching |
| Top-up request sent, not approved yet | Locked card changes to "Waiting for your load to be approved". Resumes automatically when the balance changes |
| Fight is voided (stakes refunded) | Grace ends; re-check the balance after the refund |

Rule of order at the end of a fight: **show the result, settle the payout, then check the balance, then decide.**
Never lock before the viewer has seen what happened to their own bet.

## Where it fits in the code

* `GET /_live/stream` (routes_streams.py) returns one payload for everybody, cached for 5 seconds
  (`ttlcache`). The gate **cannot live inside that cached payload**. Apply it **after** the cache, per viewer:
  if not allowed, return `{"mode": "locked", "gate": {...}}` with **no URL, no embed reference**.
* `events.stream_phase()` already says `open / onair / waiting / ended` per fight. The grace rule is
  "pending fight is not `ended`".
* The wallet balance is already read for the top bar (`wallet_snapshot` in deps.py, live via `/_live/wallet`).
  Reuse it. Do not add a second balance query path.
* Top up is the existing cash request flow (`cashdesk`, Wallet page). The gate only links to it.
* New service `app/services/stream_access.py` with one pure decision function:
  `decide(balance, minimum, pending_fight_state, exempt) -> allow | grace | locked_no_credit | locked_after_fight`.
  Pure on purpose: easy to test with no database.
* Settings (Admin Panel -> Variables): `stream.require_credit` (on/off, default **off** for rollout),
  `stream.min_credit_cents` (default = minimum bet), `stream.grace_until_fight_end` (on/off).

## Important limit: what we can and cannot enforce

* **Our own player (HLS and video file):** fully enforceable. The server simply does not send the address.
  Better still, serve HLS through short-lived signed addresses so a copied link stops working.
* **YouTube / Facebook / embedded pages:** the video lives on someone else's server. We can stop **our page**
  from showing it and from ever sending the embed address to a locked viewer, but anyone who learns the
  public link can watch it elsewhere. If paid access matters, use HLS or a private/unlisted setup on the
  provider's side.

## Edge cases to decide or test

1. **Two devices or tabs.** The gate is decided per request on the server, so a second tab cannot bypass it.
2. **Bet placed, then cancelled before the fight starts.** The pending fight disappears; re-check balance now.
3. **Several bets on the same fight.** Still one pending fight.
4. **Bet on fight 5, top up during fight 5.** Balance becomes enough, grace is no longer needed; no prompt.
5. **Replay and standby modes.** Gated like live (confirmed). Only `off` and the plain slate are free, because there is nothing to watch.
6. **Page opened mid-fight with low credit and no bet.** Locked at once (nothing pending to protect).
7. **Race between settlement and the check.** The server evaluates after the payout is posted, in the same
   request, so a winner is never prompted by mistake.
8. **Number game (Bola) draws.** Not affected: the number game has no stream (confirmed). Nothing to build there.
9. **Self-excluded or suspended viewers.** They are blocked by their own rules first; the gate message
   should not say "top up" to someone who cannot bet.

## Build steps

1. `stream_access.py` with `decide()` and the three settings. Unit tests for every row of the tables above.
2. Per-viewer gate in `/_live/stream` and the event/fight pages that embed the player. Withhold URLs when locked.
3. Player UI (`_player.html` and the player script): locked card, grace banner, top-up screen, waiting state,
   automatic resume when `/_live/wallet` reports enough credit. **The front-end script is compiled, so run
   your normal build step (`npm run build`) after this change.**
4. Admin Panel: the three settings, plus a counter on the Stream page: viewers locked, viewers in grace,
   top-ups that followed a lock.
5. Roll out with `stream.require_credit` off, switch on for one test event, watch the counter, then enable.

## Tests to write

* Pure `decide()` table: every state and every exempt role.
* Locked viewer never receives a stream URL (check the JSON body, not only the screen).
* Grace holds until the fight is settled, and also when it is voided.
* Win on the pending fight cancels the top-up prompt.
* Two sessions of the same account get the same answer.
* Exempt roles are never gated, and the DEV preview shows the state without changing anything.

## Decisions (confirmed)

1. **"No credit" means below PHP 20,** the minimum bet. Stored as `stream.min_credit_cents`, default equal to `payout.min_stake_cents`.
2. **Replay and standby clips are gated** like live streams.
3. **Everyone in the Users branch is gated:** players, sub agents, agents and masters.
4. **The number game has no stream,** so it is out of scope.
5. **All screens are in English only.**
6. **Players cannot sign in yet** in this build. Until the player login is built, the rule only reaches sub agents,
   agents and masters who hold a wallet. Nothing in this plan needs to change once players can sign in.

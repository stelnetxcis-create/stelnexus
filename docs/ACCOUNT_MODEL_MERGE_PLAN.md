# One account type, three branches: plan

Goal: every person is one kind of account with one login. Branch and tier decide what they are called, what they
may see and whether they earn commission.

    Users   player -> sub agent -> agent -> master     higher tier = more authority; above player = commission + referral codes
    Staff   declarator, teller
    Dev     pawn -> bishop -> admin

## Where the code is today (found in the build, not assumed)
* Staff, agents and masters are rows in `users`. Players are rows in `players`, a separate table.
* `auth.authenticate` only looks in `users`, and `sessions.user_id` points at `users`. A player therefore has a
  stored password but nothing can sign it in. `dev.db` confirms it: every session belongs to a `users` row.
* A player's wallet, tickets, bola tickets and cash requests hang off `players.id` (`wallet:player:<id>`,
  `tickets.player_id`, ...). Agents already bet for themselves from `wallet:user:<id>` (the "self bet" path).
* About 25 files mention Player: account_admin, players, betting, accounts, cashdesk, bola, referrals,
  commissions, identity, deps, routes_*, api, reports.

## Stage 0 (shipped with the People page): the model, as labels
`app/core/branches.py` defines the branches, tiers and who earns commission. The People page shows
"Users / Staff / Dev" and the tier next to every person. Nothing about login or money changes.

## Stage 1: players get a login (same /login, same session, same rules)
1. `Role.PLAYER` + `users.player_id` (nullable, unique). Extend the `users.role` CHECK and add the column in
   `app/db/migrate.py` (the project has no Alembic).
2. Registering an online player (referral signup, "Register player", API) also creates the login row, copying the
   password hash. Existing online players get theirs in a start-up back-fill. `parent_id` stays empty so a
   player never appears in an agent's downline or commission tree.
3. Rights: `Role.PLAYER` resolves to the existing Player rights (`PLAYER_PERMS`, still editable in Access).
   `User.preview_player_id` returns `player_id` for a real player, so wallet, own-ticket lists and the
   dashboard already written for the "view as player" preview apply unchanged.
4. Betting / cash requests: an actor with `player_id` is routed through the player path (`place_bet(player_id=...)`,
   Bola, cash requests). `players.get_player` allows the player itself.
5. Lists: Team, Accounts, counts and trees skip `role = player`; Online now includes players.
6. Status changes (suspend / remove) update both the player record and its login.

## Stage 2: fold the player record into the user (the real merge)
Only after Stage 1 has run on real data without problems. Move `wallet:player:<id>` ledger accounts to
`wallet:user:<id>`, `tickets.player_id` / `bola_tickets.player_id` / `cash_requests.player_id` to `user_id`, give the
player user a `parent_id` (its agent) so commission chains read the same for everyone, then retire `players`.
Money tables are touched, so: backup, run on a copy first, and reconcile balances before and after
(`/admin/audit` ledger integrity must stay green).

## Test checklist for every stage
Sign in as a player, an agent, a teller, a declarator and the admin. Check: login, Online now, wallet balance,
place a Sabong bet and a Bola bet, request a load, an agent's downline count, commission lines after a settled fight.

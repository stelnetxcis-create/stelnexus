# StelNexus

Betting operations console (FastAPI + SQLAlchemy). Rewrite of the legacy PHP "OnlineBukis" panel.

## Setup
    pip install -r requirements.txt
    cp .env.example .env            # then: python -m app.cli gen-keys  -> paste into .env
    python -m app.cli init-db       # tables + system accounts
    python -m app.cli create-admin --username admin   # the hidden dev account (prompts for password)
    python -m app.cli create-master --username boss   # the top visible account
    python -m app.cli seed-demo     # optional, ENV=dev only
    uvicorn app.main:app --port 8000

For a quick local try without Postgres: `DATABASE_URL=sqlite:///./dev.db COOKIE_SECURE=false COOKIE_SAMESITE=lax`.

## API
`POST /api/v1/auth/token {"username","password"}` returns a bearer token (or create one at /profile).
Docs at `/api/docs` when `ENV=dev`. Mutating calls accept an `idempotency_key`.

## Production checklist
`ENV=prod` refuses to start unless SECRET_KEY, TRUSTED_HOSTS, FRAME_ANCESTORS, COOKIE_SAMESITE and COOKIE_SECURE are set safely.
Set PII_KEY to encrypt player address / GCash numbers.

## Upgrading an existing database
`python -m app.cli init-db` (or simply starting the app) now runs `app/db/migrate.py`: it adds the new columns/tables and widens two CHECK constraints (`users.role` gets `declarator`, `tickets.side` gets `draw`). It never drops data. Back up `dev.db` / your Postgres first anyway.

## Added to match the old OnlineBukis sabong side
- **Draw** is a bettable side at a fixed multiplier (`payout.draw_odds_bps`, default 8.00x). On a Draw result Draw tickets win and Meron/Wala are refunded; otherwise Draw stakes go to the house.
- **Minimum odds** (`payout.min_odds_bps`, default 1.40x): a pool fight whose winner would pay less is cancelled and refunded automatically. Set 0 to turn off.
- **Llamado / Dehado**: set the favourite per game; declare "Llamado wins" / "Dehado wins".
- **Force bet side** per game (all / Meron only / Wala only).
- **Emergency withdrawal switch** on Load / Cash Out (also in Settings).
- **Cancel whole event** (refunds every unsettled game).
- Per-game figures: Revolving amount, System over, Hidden over.
- **Public live board** at `/live` (toggle: Settings -> `live.board_enabled`).
- **Event expenses** (manual, informational), event summary, **Game History**, **Report Archives** with CSV.
- **Declarator** role: declares results, never sees stakes, pools or payouts.
- **Staff chat** for the Admin and for Pawns granted `chat`. Not Master.
- Terminology: Tong, Credit/Load/Remit credit, Cancel ticket, Event / Game.

## Tests
    pip install -r requirements-dev.txt
    python -m pytest -q
Tests use their own temporary SQLite file and never touch `dev.db`.

## Online-only
There is no physical cash anywhere in the platform: no tellers, stations/drawers, paper booklets, counter cash-in/out or ticket-claim desk. Money moves one way in and one way out:

- **In:** a player's load is a GCash or bank request with a reference number, approved by Master/Admin (Cash In/Out page).
- **Bet:** every bet is paid from the player's credit. There are no walk-in or cash tickets.
- **Out:** winnings are credited to the player's credit automatically at settlement; cash-outs are approved requests.

**Upgrading an older database:** starting the app closes any existing `teller` accounts (and revokes their sessions) and relabels the clearing account. Nothing is deleted: old stations, tickets and ledger rows stay readable, so history and the ledger integrity check keep balancing. The `teller` role and the `station_cash` / `cash_on_hand` account kinds remain in the code only so those old rows can still be loaded.

## Network, cuts and the hidden layer
**Roles:** `admin` (hidden) · `master` · `cavo` · `kubrador` (+ `declarator`). Players are not users.

**Invites** (Referral Codes): the role of the invitee is fixed by the issuer. Master's code makes a Cavo, a Cavo's code makes a Kubrador, a Kubrador's code makes a normal (player) account under them. One use, timed. The **Admin** may pick any type for a code (Master, Cavo, Kubrador or Player) and, for the last three, who the new account reports to (stored on the code, `referral_codes.parent_id`).

**Cut formula** (per ticket, summed per game; every figure in Settings -> Commissions, per-account overrides on the Cuts page):

    pool       = basis x pool%                  (basis = ticket's share of the house win, or its stake)
    cavo_gross = pool x Volume %                (default 30%)
    kubrador   = cavo_gross x SOP %             (default 20%, taken OUT of the Cavo's cut)
    cavo       = cavo_gross - kubrador
    master     = pool - cavo_gross              (Management, default 70%)

master + cavo + kubrador always equals the pool exactly. Master and Admin edit Volume % / SOP % (Master within the limits set in Settings); every change is audited (`cut.update`), every settlement writes `commission.split`, and each commission line stores its formula inputs.

**Admin vs Master:** identical operational powers. Admin alone sees Settings, Audit Trail, ledger integrity, System Over / Hidden Over, the settlement House column and per-line formula. The Admin account never appears in lists, trees, counts, chat or receipts for anyone else (shown as "System").

**Upgrading:** starting the app renames gold -> cavo and sub -> kubrador in existing rows and widens the role CHECK. `audit_logs` is left as written.

## Roles, wallet and Admin Panel (v2)
- **Wallet** (click the balance in the top bar): overview, load, cash out, transfer, money history (what you earned/received and spent/sent), and for Admin/Master the approvals queue + emergency withdrawal switch. Cash In/Out and Transfer are no longer in the side menu or Users page.
- **Transfer** is open to every account that holds credit (Admin, Master, Cavo, Kubrador). Who can receive is `wallet.transfer_scope` (anyone | network) in Admin Panel -> Variables -> Credit.
- **Own-wallet load / cash-out** are approved requests; only the Admin can approve a request for their own credit.
- **Declarator** and **Pawn** have no balance and cannot hold, send, receive, load, cash out or bet.
- **Pawn**: created by the Admin only (Admin Panel -> Pawns). A declarator-style spectator that **can declare results by default** (not a grant); the Admin ticks which Admin Panel sections it may additionally see or change (variable groups, locations, cuts, audit, players, who is online, totals, staff chat, `agents:create`...). Nothing extra is on by default.
- **Admin Panel** (`/admin`): Variables (was Settings), Locations, Cuts, Pawns, Audit trail. Old URLs (/settings, /cuts, /audit, /integrity, /cashin_out) redirect.
- **Users** page: Master/Admin only. **Players** page, **Cuts** and **who is online** (`/clp/online`): Admin (or a Pawn with the grant). Master no longer has Cuts or Players.
- **Locations**: Master/Admin create an event at a location and can register a venue on the spot; the Admin edits, archives, deletes, adds custom fields and per-venue defaults (games, payout model, tong). `event.require_location` (default on) applies once a location exists.
- Upgrade: start the app; the migration adds `users.grants`, `events.location_id`, `cash_requests.target`, the `locations` table and the `pawn` role. Nothing is dropped.

## Games, results and pictures (v3)
- **Who declares:** Admin, Pawn and Declarator. **Master cannot declare or redeclare** (and has no staff chat); Master still runs events, locks games and can approve a redeclare when approval is required. Cavo and Kubrador never declare.
- **Direct account creation:** the Admin can create Master, Cavo, Kubrador, Declarator and Pawn accounts directly (no referral code) and picks the upline for a Cavo/Kubrador. A Pawn with the grant `agents:create` can create Cavo/Kubrador the same way. Master can create Cavo and Declarator.
- **Games lobby:** `/events` shows one box per event; opening it shows numbered game tiles with status. Status filter chips narrow the list.
- **Result pictures:** a **Pawn must upload one to declare** (setting `fight.pawn_proof_required`, on by default). It is optional for Admin and Declarator. JPEG/PNG/WebP only (type is read from the file's first bytes), max `MAX_UPLOAD_BYTES` (5 MB). Stored in `var/uploads/proofs` under a random name, served only to signed-in staff at `/fights/{id}/proof`. A replaced picture stays on disk as evidence.
- **Game History** shows Location and Picture.
- **Upgrade note:** the migration adds `fights.proof_file`, `fights.proof_mime`, `fights.proof_by_id`, `fights.proof_at` and `referral_codes.parent_id`. Nothing is dropped.

## Backups
`deploy/backup.sh` writes a Postgres dump **and** a tar of `var/uploads` (result pictures) to `BACKUP_DIR`, both kept 14 days. Back up `.env` (PII_KEY) separately.

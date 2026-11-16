# StelNexus

StelNexus is a web console and JSON API for running a managed betting operation. It is built with FastAPI and SQLAlchemy, serves server-rendered HTML pages, and exposes a token-authenticated API for mobile clients.

It handles real-money flows (GCash / bank cash-in and cash-out), so it must be deployed and operated in line with the gambling laws and licensing rules that apply where you run it. See [Legal and compliance](#legal-and-compliance).

## Contents

- [What it does](#what-it-does)
- [Tech stack](#tech-stack)
- [Project layout](#project-layout)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [CLI reference](#cli-reference)
- [Roles and permissions](#roles-and-permissions)
- [Money model](#money-model)
- [Betting and settlement](#betting-and-settlement)
- [Commissions (cuts)](#commissions-cuts)
- [HTTP API](#http-api)
- [Testing](#testing)
- [Production deployment](#production-deployment)
- [Backups](#backups)
- [Security](#security)
- [Upgrading an existing database](#upgrading-an-existing-database)
- [Legal and compliance](#legal-and-compliance)

## What it does

- **Events and games.** Staff create events, each with one or more games (fights). Players bet on a side; a declarator or an authorised pawn declares the result.
- **Sides.** Meron and Wala are pool sides. Draw is a bettable side at a fixed multiplier. Llamado / Dehado (favourite / underdog) can be set per game, and a game can be restricted to one side.
- **Online-only wallet.** All money moves through credit. There is no physical cash handling: loads are GCash or bank requests with a reference number and are approved by staff, bets are paid from credit, and winnings are credited automatically at settlement.
- **Agent network.** Accounts form a tree (Master, Cavo, Kubrador, then players). Invite codes create accounts under the issuer, and commissions are split automatically at settlement.
- **Audit and integrity.** Every sensitive action is written to an audit log, the ledger has an integrity check, and audit-log tables are guarded against edits.
- **Live board.** A public read-only board at `/live` (toggle with the `live.board_enabled` setting).
- **Reports.** Game history, event summaries, event expenses, report archives with CSV export, and per-player history.
- **Staff chat.** For the Admin and for Pawns that have been granted the `chat` permission.

## Tech stack

| Area | Technology |
| --- | --- |
| Web framework | FastAPI, Starlette, Uvicorn |
| Database | SQLAlchemy 2.x. PostgreSQL (psycopg 3) in production, SQLite for local development |
| Templates | Jinja2, with static CSS / JS under `app/static` |
| Validation and settings | Pydantic v2, pydantic-settings |
| Password hashing | Argon2 (argon2-cffi) |
| Encryption of personal data | `cryptography` (field-level, keyed by `PII_KEY`) |
| Sessions and tokens | itsdangerous, signed cookies, bearer tokens for the API |
| QR codes | segno |
| Tests | pytest, httpx |
| Deployment | systemd unit and Caddy reverse proxy (examples in `deploy/`) |

## Project layout

```
app/
  main.py              Application factory, middleware, router registration
  cli.py               Management commands (python -m app.cli ...)
  api/v1/              JSON API routes (mounted at /api/v1)
  core/                Config, errors, money helpers, RBAC, middleware, settings registry, audit
  db/                  Models, session, migrations (migrate.py), audit-log guards
  services/            Business logic: accounts, wallet, cashdesk, betting, settlement,
                       commissions, referrals, events, reports, ledger, locations, presence, proofs
  web/                 HTML routes and Jinja templates (templates/)
  static/              CSS, JS, images, service worker, web manifest
deploy/                stelnexus.service, Caddyfile, backup.sh
scripts/               security_check.sh
tests/                 pytest suite
SECURITY.md            Production security checklist
```

## Quick start

Requirements: Python 3.11 or newer. PostgreSQL is optional for local use.

```bash
git clone https://github.com/stelnetxcis-create/stelnexus.git
cd stelnexus

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python -m app.cli gen-keys          # prints SECRET_KEY and PII_KEY; paste them into .env

python -m app.cli init-db           # creates tables and system accounts
python -m app.cli create-admin --username admin     # hidden super account (prompts for password)
python -m app.cli create-master --username boss     # top visible account

python -m app.cli seed-demo         # optional demo data, ENV=dev only

uvicorn app.main:app --port 8000
```

For a quick local run without PostgreSQL, put these in `.env` (or export them):

```
ENV=dev
DATABASE_URL=sqlite:///./dev.db
COOKIE_SECURE=false
COOKIE_SAMESITE=lax
```

Open `http://localhost:8000`. In `ENV=dev` the interactive API docs are at `/api/docs`.

## Configuration

Settings are read from environment variables or `.env`. Settings that are lists are written as JSON.

| Variable | Purpose |
| --- | --- |
| `ENV` | `dev` or `prod`. `prod` refuses to start unless the security settings below are safe |
| `DATABASE_URL` | SQLAlchemy URL, for example `postgresql+psycopg://user:pass@localhost/stelnexus` or `sqlite:///./dev.db` |
| `SECRET_KEY` | Signs sessions and tokens. Generate with `python -m app.cli gen-keys` |
| `PII_KEY` | Encrypts player addresses and GCash numbers. Back it up separately |
| `COOKIE_SECURE` | `true` in production (HTTPS only) |
| `COOKIE_SAMESITE` | `lax` recommended |
| `TRUSTED_HOSTS` | JSON list of allowed `Host` headers, for example `["your.domain"]` |
| `TRUSTED_PROXIES` | JSON list of proxy IPs whose forwarded headers are trusted, for example `["127.0.0.1"]` |
| `FRAME_ANCESTORS` | CSP frame-ancestors value, for example `'self'` |
| `DEBUG` | Must be `false` in production |
| `UPLOAD_DIR` | Where result pictures and payment proofs are stored (default `var/uploads`) |
| `BACKUP_DIR` | Used by `deploy/backup.sh` (default `/var/backups/stelnexus`) |

Operational values (odds, limits, commission rates, withdrawal switch, transfer scope, live board) are not environment variables. They live in the database and are edited by the Admin in **Admin Panel, then Variables**.

## CLI reference

Run as `python -m app.cli <command>`.

| Command | Description |
| --- | --- |
| `init-db` | Creates tables, runs schema upgrades, creates system accounts |
| `create-admin --username NAME` | Creates the hidden Admin account (prompts for a password) |
| `create-master --username NAME` | Creates the top visible Master account |
| `create-pawn --username NAME` | Creates a Pawn (a restricted staff account) |
| `seed-locations` | Seeds venue locations |
| `seed-demo` | Loads demo data (`ENV=dev` only) |
| `gen-keys` | Generates `SECRET_KEY` and `PII_KEY` values |
| `encrypt-pii` | Encrypts existing plaintext player data after `PII_KEY` is set |

## Roles and permissions

| Role | Summary |
| --- | --- |
| `admin` | Hidden super account. Same operational powers as Master, plus Variables, audit trail, ledger integrity, system / hidden figures and per-line commission formulas. Never appears in lists, trees, chat or receipts for other users (shown as "System") |
| `master` | Top visible account. Approves loads and cash-outs, manages the network, edits cuts within the limits set by the Admin |
| `cavo` | Mid-level agent. Can invite Kubradors and holds credit |
| `kubrador` | Lowest agent level. Can invite player accounts under them |
| `declarator` | Declares results. Never sees stakes, pools or payouts. Has no balance |
| `pawn` | Created by the Admin only. Can declare results by default; the Admin ticks any extra Admin Panel sections it may see or change (for example locations, cuts, audit, players, online users, totals, staff chat). No balance, cannot bet or transfer |
| Player | Not a login role in the staff sense. Bets from credit under an agent |

**Invites.** The invitee's role is fixed by the issuer: a Master code creates a Cavo, a Cavo code creates a Kubrador, a Kubrador code creates a player. Codes are single-use and time-limited. The Admin can issue any type and choose who the new account reports to.

## Money model

- **In:** a load is a GCash or bank request with a reference number. Staff approve it, and only then does money move.
- **Bet:** every bet is paid from the player's credit. There are no walk-in or counter tickets.
- **Out:** winnings are credited automatically at settlement. Cash-outs are requests that staff approve.
- **Requests are a state machine** (`pending`, then `approved` or `rejected`) protected by a row lock and a unique idempotency key, so a request cannot be approved twice.
- **Transfers** between accounts are open to every role that holds credit. Who may receive is controlled by `wallet.transfer_scope` (`anyone` or `network`).
- **Own-wallet requests** by staff can only be approved by the Admin.
- **Emergency stop:** an Admin switch turns off all withdrawals instantly.
- **Ledger:** all movements are double-entry rows. The integrity check (Admin only) verifies that the ledger balances.

## Betting and settlement

- **Draw:** bettable at a fixed multiplier (`payout.draw_odds_bps`, default 8.00x). On a Draw result, Draw tickets win and Meron / Wala stakes are refunded. Otherwise Draw stakes go to the house.
- **Minimum odds:** `payout.min_odds_bps` (default 1.40x). A pool fight whose winner would pay less is cancelled and refunded automatically. Set to 0 to disable.
- **Cancellation:** a single ticket, a game, or a whole event (refunds every unsettled game) can be cancelled.
- **Per-game figures:** revolving amount, system over, and hidden over (the last two are Admin-only).
- **Result pictures** are stored as evidence under `var/uploads` and are included in backups.

## Commissions (cuts)

Computed per ticket and summed per game. All percentages are editable in the Admin Panel, with per-account overrides on the Cuts page.

```
pool       = basis x pool%             (basis = the ticket's share of the house win, or its stake)
cavo_gross = pool x Volume %           (default 30%)
kubrador   = cavo_gross x SOP %        (default 20%, taken out of the Cavo's cut)
cavo       = cavo_gross - kubrador
master     = pool - cavo_gross         (management share, default 70%)
```

`master + cavo + kubrador` always equals the pool exactly. Every change to a cut is audited (`cut.update`), every settlement writes a `commission.split` entry, and each commission line stores its formula inputs.

## HTTP API

- `POST /api/v1/auth/token` with `{"username", "password"}` returns a bearer token. Tokens can also be created from the profile page.
- Send `Authorization: Bearer <token>` on later requests.
- Mutating calls accept an `idempotency_key` so retries are safe.
- Interactive docs at `/api/docs` and the schema at `/api/openapi.json` are available only when `ENV=dev`.
- `GET /healthz` is a health probe that checks database connectivity.

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Tests use their own temporary SQLite database and never touch `dev.db`.

## Production deployment

1. Provision PostgreSQL and create a database and user.
2. Create `.env` with `ENV=prod`, a real `SECRET_KEY` and `PII_KEY`, `COOKIE_SAMESITE=lax`, `COOKIE_SECURE=true`, `TRUSTED_HOSTS`, `TRUSTED_PROXIES`, `FRAME_ANCESTORS='self'` and `DEBUG=false`. The app will refuse to start if these are unsafe.
3. Install into `/opt/stelnexus`, create the virtualenv and install `requirements.txt`.
4. Run `python -m app.cli init-db` and create the Admin and Master accounts.
5. Install `deploy/stelnexus.service` as a systemd unit (adjust the user and paths). It binds to `127.0.0.1:8000` and runs **one worker**, because the rate limiter keeps state in process memory.
6. Put Caddy (or nginx) in front for HTTPS. `deploy/Caddyfile` is a working example: replace the domain, then run `caddy run --config deploy/Caddyfile`.
7. Run `scripts/security_check.sh` before every deploy.

## Backups

`deploy/backup.sh` writes a compressed `pg_dump` plus an archive of `var/uploads` to `BACKUP_DIR` and deletes backups older than 14 days. Schedule it with cron:

```
15 2 * * * /opt/stelnexus/deploy/backup.sh
```

Copy the output off the machine. Back up `.env` separately, because without `PII_KEY` the encrypted player data in a backup cannot be read.

## Security

See [SECURITY.md](SECURITY.md) for the production checklist. Highlights:

- Argon2 password hashing, CSRF protection, strict security headers, trusted-host enforcement.
- Field-level encryption of personal data (`PII_KEY`).
- Idempotency keys and row locks on money-moving operations.
- Audit log with database-level guards against modification.
- Known limits: no two-factor login yet, and non-login rate limits (bets, cash desk) are per-process memory.

Never commit `.env`, database files, backups, or `var/uploads` (payment proofs and result pictures contain other people's data). The provided `.gitignore` excludes them. If a secret was ever committed or shared, rotate it.

## Upgrading an existing database

Starting the app or running `python -m app.cli init-db` runs `app/db/migrate.py`, which adds new columns and tables and widens CHECK constraints for new roles and ticket sides. It never drops data. Back up your database before upgrading anyway.

## Legal and compliance

Online betting is regulated or prohibited in many jurisdictions, including parts of the Philippines. This repository is software only. You are responsible for making sure that your use of it is lawful where you operate and where your players are, including any licensing, age verification, anti-money-laundering and tax obligations.

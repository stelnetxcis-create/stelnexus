# StelNexus

Betting operations console (FastAPI + SQLAlchemy). Rewrite of the legacy PHP "OnlineBukis" panel.

## Setup
    pip install -r requirements.txt
    cp .env.example .env            # then: python -m app.cli gen-keys  -> paste into .env
    python -m app.cli init-db       # tables + system accounts
    python -m app.cli create-admin --username admin   # prompts for password
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
- **Cancel whole main event** (refunds every unsettled game).
- **Claim Ticket** page: camera QR scan, USB scanner or typed code; tracks claimed vs unclaimed cash payouts.
- Per-game figures: Total unclaimed, Revolving amount, System over, Hidden over.
- **Public live board** at `/live` (toggle: Settings -> `live.board_enabled`).
- **Event expenses** (manual, informational), event summary, **Game History**, **Report Archives** with CSV.
- **Declarator** role: declares results, never sees stakes, pools or payouts.
- **Staff chat** for admin and tellers.
- Terminology: Tong, Load credit / Remit credit, Cancel ticket, Main Event / Game. Full list in [TERMINOLOGY.md](TERMINOLOGY.md).

## Tests
    pip install -r requirements-dev.txt
    python -m pytest -q
Tests use their own temporary SQLite file and never touch `dev.db`.

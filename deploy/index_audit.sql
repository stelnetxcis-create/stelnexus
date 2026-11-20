-- StelNexus index changes for PostgreSQL. REVIEW BEFORE RUNNING; nothing here runs automatically.
--
-- Run section 1 any time. Do section 2 only after `python scripts/index_audit.py` on the
-- production server shows the index has had ~0 scans over a full week or more of real traffic.
-- Run with:  psql "$DATABASE_URL" -f deploy/index_audit.sql      (NOT with -1: CONCURRENTLY cannot
-- run inside a transaction). Take a backup first (deploy/backup.sh).

-- =========================================================== 1. add (safe, online, idempotent)

-- Wallet history: WHERE account_id = ? ORDER BY id DESC LIMIT n. Reads in index order, no sort.
-- (New installs get this from the model already; existing databases need it created here.)
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_ledger_entries_account_id_id
    ON ledger_entries (account_id, id);

-- Game history page: settled/void games newest first. A partial index that matches the query
-- exactly, so the sort is skipped and the index only holds finished games.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_fights_history
    ON fights (declared_at DESC NULLS LAST, id DESC)
    WHERE status IN ('settled', 'void');

-- Optional: the audit / bet-log search boxes use ILIKE '%text%', which a normal b-tree cannot serve.
-- Worth it only once audit_logs or tickets reach hundreds of thousands of rows.
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_audit_logs_action_trgm ON audit_logs USING gin (action gin_trgm_ops);

-- ================================================ 2. drop candidates (single-column, already covered)
-- Each is the leading column(s) of a composite or unique index, so lookups on that column already
-- use the wider index. Dropping saves write cost and disk. Uncomment one at a time, after checking
-- idx_scan in pg_stat_user_indexes, and keep the CREATE line to roll back.
--
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_draws_draw_date;          -- covered by ix_bola_draws_date_status
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_draws_location_id;        -- covered by unique (location_id, draw_date, slot)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_fights_event_id;               -- covered by unique (event_id, number)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_teams_sport;                   -- covered by unique (sport, code)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_users_parent_id;               -- covered by ix_users_parent_status
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_runs_draw_id;             -- covered by unique (draw_id, version)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_schedules_location_id;    -- covered by unique (location_id, slot)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_tickets_draw_id;          -- covered by ix_bola_tickets_draw_status
-- DROP INDEX CONCURRENTLY IF EXISTS ix_settlement_runs_fight_id;      -- covered by unique (fight_id, version)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_commissions_agent_id;     -- covered by ix_bola_commissions_agent_status
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_commissions_ticket_id;    -- covered by unique (ticket_id, agent_id, run_id)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_bola_lines_draw_id;            -- covered by ix_bola_lines_pair
-- DROP INDEX CONCURRENTLY IF EXISTS ix_tickets_player_id;             -- covered by ix_tickets_player_placed
-- DROP INDEX CONCURRENTLY IF EXISTS ix_tickets_fight_id;              -- covered by ix_tickets_fight_status
-- DROP INDEX CONCURRENTLY IF EXISTS ix_commissions_ticket_id;         -- covered by unique (ticket_id, agent_id, run_id)
-- DROP INDEX CONCURRENTLY IF EXISTS ix_commissions_agent_id;          -- covered by ix_commissions_agent_status
-- DROP INDEX CONCURRENTLY IF EXISTS ix_ledger_entries_account_id;     -- covered by ix_ledger_entries_account_id_id (after section 1)

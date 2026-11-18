# Master Agent / Agent / Sub Agent (Nov 17)

Ladder on top of Cavo / Kubrador. Master keeps its stored role `master` (label "Master Agent"); new roles `agent`, `sub_agent`.

## Rules
- The three have NO cut and no percentage. Only Cavo and Kubrador have one (Volume % / SOP %), unchanged.
- Invites: Master -> agent (default) or cavo. Agent -> sub_agent (default) or cavo. Sub Agent -> cavo.
  Cavo -> kubrador. Kubrador -> player. (`INVITE_ROLES` in `db/models.py`.) The old rule is back: nobody above a Cavo
  creates Kubradors or players.
- Uplines (Admin / Pawn picks): agent under master, sub_agent under agent, cavo under master/agent/sub_agent,
  kubrador under cavo, player under kubrador (`UPLINE_ROLES`).
- Agent and Sub Agent hold the same permissions as a Cavo.

## Money (Bola only)
- Meron / Wala is untouched: the management share still goes to the Master.
- Bola: the management share (pool - cavo_gross, the 70%) goes whole to the Cavo's nearest Sub Agent / Agent / Master Agent
  (`agent_tiers=True` in `compute_lines`). Others above get nothing from that Cavo. Line tier = the receiver's role.
- Sub Agent / Agent are always paid; the Master Agent only with `bola.commission.pay_master` (old rule D4, unchanged).

## Decisions to confirm
1. Meron / Wala still pays its original Master / Cavo / Kubrador commission (old code, untouched). If sabong should pay
   no commission at all, that is a separate change.
2. Ticket snapshots (`kubrador`, `cavo` names on the Betting List) are unchanged; no Agent columns.

## Migration
`users.role_valid` CHECK widened by `db/migrate.py` (SQLite and PostgreSQL). Run `python -m app.cli init-db` once. No data changes.

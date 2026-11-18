# Cavo + Kubrador merged into Agent + Sub Agent; statements + commission merged (Nov 18)

## Tree
Master Agent -> Agent -> Sub Agent -> Player. An Agent may also recruit players directly.
Invites: Master -> agent; Agent -> sub_agent (default) or player; Sub Agent -> player.
Cavo / Kubrador remain as retired stored values only (nobody can be given them).

## Money (both games)
pool = basis x pool%;  agent_gross = pool x Volume% (default 30);  sub_agent = agent_gross x SOP% (default 30, out of the Agent's cut);
agent = agent_gross - sub_agent;  master = pool - agent_gross.  Default per 1,000 of pool: Master 700 / Agent 210 / Sub Agent 90.
- Meron/Wala and Bola keep their own basis and pool %, and their own default Volume % / SOP %:
  `commission.cavo_cut_bps` / `commission.kubrador_cut_bps` (Meron/Wala; old key names kept so saved values carry over)
  `bola.commission.agent_cut_bps` / `bola.commission.sub_agent_cut_bps` (Bola).
- A cut set on one account (Cuts page) beats the game default and applies to every game.
- Bola: the old "nearest Sub Agent / Agent / Master gets the management share" rule is gone; the Master's remainder follows
  the same rule as before: a commission line only when `bola.commission.pay_master` is on, otherwise it stays with the house.

## Statements + commission = /commissions
One page, both games: your own totals, a statement row for you AND everyone below you (operator tier: everyone visible),
and every commission line in that network (with "Earned by"). Game filter, date range (default last 7 days), CSV (`/commissions.csv`),
per-account page `/commissions/{id}`. `/bola/agents*` redirect there. Entry point stays the Commission link in the wallet panel.

## Migration (`python -m app.cli init-db`, also runs on start)
cavo -> agent, kubrador -> sub_agent in users, referral_codes, commissions.tier, bola_commissions.tier. Amounts untouched.
Cavos that sat under the short-lived Nov 17 Agent / Sub Agent accounts are listed as "review:" in the migration log.
Old tickets keep their `cavo` / `kubrador` name columns; new tickets store the Agent / Sub Agent there (headers say Agent / Sub Agent).

## Tests
`tests/test_agent_tiers.py` (old ladder) replaced by `tests/test_agent_merge.py`. Suite: 278 passed; the 2 failures
(`test_zz_chat_store` script versions, `test_zz_store_v2` css) were failing before this change and are unrelated.

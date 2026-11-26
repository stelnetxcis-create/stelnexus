"""The system pool: the house reserve.

Pinned here:
* every settled fight / Bola draw puts a share of the game's cut (30% by default) in the pool, paid to nobody;
* the Master / Agent / Sub Agent split is taken from the rest, and reserve + split always equal the cut;
* a redeclare reverses the reserve with the commissions (never doubled);
* the pool shows on the Teller page to the Admin, Tellers and a Pawn who was granted it, and to nobody else.
"""
import re

import pytest
from sqlalchemy import select

from app.db.models import Commission, Result, Role
from app.services import agents, ledger, settlement, system_pool
from tests.conftest import PASSWORD
from tests.test_agent_merge import _pool, _sabong, _tiers, tree  # noqa: F401  (the Master -> Agent -> Sub Agent tree)
from tests.test_web import login


def test_the_reserve_is_set_aside_and_the_rest_is_split(tree):
    w = tree
    before = _pool(w.db)
    rows = _sabong(w, w.sub, w.p_sub)                          # the cut: 5% Tong of 20,000 = 1,000
    reserve = _pool(w.db) - before
    assert reserve == 300                                       # 30% of the cut, in the system pool
    assert sum(_tiers(rows).values()) + reserve == 1000         # reserve + Master/Agent/Sub Agent = the whole cut
    assert ledger.verify_integrity(w.db)["ok"]
    got = system_pool.summary(w.db)
    assert got["balance"] == _pool(w.db) and got["sabong"] >= 300 and got["recent"]


def test_a_redeclare_reverses_the_reserve_instead_of_doubling_it(tree):
    w = tree
    before = _pool(w.db)
    _sabong(w, w.sub, w.p_sub, result=Result.MERON)
    assert _pool(w.db) - before == 300
    run = settlement.request_correction(w.db, w.admin, fight_id=w.games[0].id, result=Result.WALA,
                                        reason="declared the wrong side", settings=w.store)
    if run.status.value == "pending":
        settlement.approve(w.db, w.master, run_id=run.id, settings=w.store)      # maker-checker: a second person
    assert _pool(w.db) - before == 300                          # reversed, then posted again: still one 300
    assert ledger.verify_integrity(w.db)["ok"]


def test_a_full_reserve_pays_the_network_nothing(tree):
    w = tree
    w.store.set("commission.reserve_bps", "10000", w.admin.id)
    try:
        before = _pool(w.db)
        rows = _sabong(w, w.sub, w.p_sub)
        assert rows == [] and _pool(w.db) - before == 1000      # the whole cut stays in the pool
        assert ledger.verify_integrity(w.db)["ok"]
    finally:
        w.store.set("commission.reserve_bps", "3000", w.admin.id)


def test_who_sees_the_pool_on_the_teller_page(tree):
    w = tree
    _sabong(w, w.sub, w.p_sub)
    w.db.commit()
    tag = w.tag
    teller = agents.create_agent(w.db, w.admin, username=f"tl{tag}", password=PASSWORD, role=Role.TELLER,
                                 full_name="Tess Teller", settings=w.store, gcash_number="09171234567")
    pawn = agents.create_agent(w.db, w.admin, username=f"pn{tag}", password=PASSWORD, role=Role.PAWN,
                               full_name="Pawn", settings=w.store)
    agents.set_pawn_grants(w.db, w.admin, pawn.id, ["staff:teller"])
    for u in (teller, pawn):
        u.must_change_password = False
    w.db.commit()

    for who in ("admin", f"tl{tag}"):
        html = login(who)[0].get("/staff/teller").text
        assert "System pool" in html and "locked" in html.lower()
    assert "System pool" not in login(f"pn{tag}")[0].get("/staff/teller").text          # a Pawn: not unless granted
    agents.set_pawn_grants(w.db, w.admin, pawn.id, ["staff:teller", "systempool:view"])
    w.db.commit()
    assert "System pool" in login(f"pn{tag}")[0].get("/staff/teller").text
    assert login(f"ag{tag}")[0].get("/staff/teller").status_code in (302, 303, 403, 404)   # an Agent: not at all
    assert "System pool" not in login(f"ag{tag}")[0].get("/staff/teller").text

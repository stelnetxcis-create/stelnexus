"""Per-area Bola caps: a Location can override the global limit and liability caps.

NULL on the Location = follow the global ``bola.*`` setting, 0 = no cap in that area.
"""
import datetime as dt

import pytest

from app.core.errors import LockedState, ValidationError
from app.db.models import BolaKind, DrawStatus
from app.services import bola, locations
from tests.test_bola import _bet, _set, bw  # noqa: F401  (bw is a fixture)


@pytest.fixture()
def two_areas(bw):
    """bw's draw sits in area A. Add area B (no overrides) with its own open draw."""
    w = bw
    loc_b = locations.create_location(w.db, w.admin, name=f"Area B {w.tag}")
    soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    draw_b = bola.create_draw(w.db, w.admin, location_id=loc_b.id, draw_date=dt.date.today(), slot=1,
                              settings=w.store, scheduled_at=soon, status=DrawStatus.OPEN)
    w.area_a, w.area_b, w.draw_b = w.draw.location, loc_b, draw_b
    yield w
    loc_b.is_active = False
    w.db.flush()


def _bet_in(w, draw, numbers, tumbok, sahod=0):
    return bola.place_bola_bet(w.db, w.kub, draw_id=draw.id, kind=BolaKind.PAIR, numbers=numbers,
                               tumbok_cents=tumbok, sahod_cents=sahod, settings=w.store)


def test_effective_caps_null_follows_global_and_override_wins(two_areas):
    w = two_areas
    assert bola.effective_caps(w.store, w.area_a) == (150_000, 0, 0)          # all global
    w.area_a.bola_limit_per_line_cents = 50_000
    w.area_a.bola_max_liability_per_outcome_cents = 7_000_000
    assert bola.effective_caps(w.store, w.area_a) == (50_000, 7_000_000, 0)
    assert bola.effective_caps(w.store, w.area_b) == (150_000, 0, 0)          # B unaffected


def test_line_limit_override_applies_only_to_that_area(two_areas):
    w = two_areas
    w.area_a.bola_limit_per_line_cents = 50_000                               # PHP 500 in A
    w.db.flush()
    _bet_in(w, w.draw, [12, 24], 50_000)                                      # exactly the cap: ok
    with pytest.raises(LockedState):
        _bet_in(w, w.draw, [12, 24], 100)                                     # one peso more: refused
    _bet_in(w, w.draw_b, [12, 24], 60_000)                                    # same pair in B: global 1,500


def test_override_zero_means_no_cap_even_when_global_is_tight(two_areas):
    w = two_areas
    _set(w, "bola.limit_per_line_cents", 5_000)
    try:
        with pytest.raises(LockedState):
            _bet_in(w, w.draw, [3, 4], 6_000)                                 # global PHP 50 refuses
        w.area_a.bola_limit_per_line_cents = 0
        w.db.flush()
        _bet_in(w, w.draw, [3, 4], 6_000)                                     # A: no cap
        with pytest.raises(LockedState):
            _bet_in(w, w.draw_b, [3, 4], 6_000)                               # B still follows global
    finally:
        _set(w, "bola.limit_per_line_cents", 150_000)


def test_clearing_override_goes_back_to_global(two_areas):
    w = two_areas
    w.area_a.bola_limit_per_line_cents = 5_000
    w.db.flush()
    with pytest.raises(LockedState):
        _bet_in(w, w.draw, [5, 6], 6_000)
    w.area_a.bola_limit_per_line_cents = None
    w.db.flush()
    _bet_in(w, w.draw, [5, 6], 6_000)


def test_liability_caps_override_per_area(two_areas):
    w = two_areas
    w.area_a.bola_max_liability_per_outcome_cents = 10_000_000                # PHP 100,000 = 125 x 800
    w.area_a.bola_max_liability_per_draw_cents = 10_000_000
    w.db.flush()
    _bet_in(w, w.draw, [7, 9], 12_500)                                        # exactly the cap
    with pytest.raises(LockedState):
        _bet_in(w, w.draw, [7, 9], 100)
    _bet_in(w, w.draw_b, [7, 9], 12_500)                                      # B has no liability cap
    _bet_in(w, w.draw_b, [7, 9], 100)


def test_exposure_summary_uses_the_areas_cap(two_areas):
    w = two_areas
    w.area_a.bola_max_liability_per_outcome_cents = 10_000_000
    w.db.flush()
    _bet_in(w, w.draw, [7, 9], 5_000)                                         # 50 x 800 = PHP 40,000 owed
    a = bola.exposure_summary(w.db, w.draw, w.store)
    b = bola.exposure_summary(w.db, w.draw_b, w.store)
    assert a["outcome_cap_cents"] == 10_000_000 and a["headroom_cents"] == 10_000_000 - 4_000_000
    assert b["outcome_cap_cents"] == 0 and b["headroom_cents"] is None


def test_location_form_values_parse_and_audit(two_areas):
    w = two_areas
    loc = locations.update_location(
        w.db, w.admin, w.area_b.id, name=w.area_b.name, address=None, city=None, notes=None,
        custom_fields=None, default_fight_count=None, default_payout_model=None,
        default_house_cut_bps=None, bola_limit_per_line="500", bola_max_liability_per_outcome="1,000.50",
        bola_max_liability_per_draw="")
    assert (loc.bola_limit_per_line_cents, loc.bola_max_liability_per_outcome_cents,
            loc.bola_max_liability_per_draw_cents) == (50_000, 100_050, None)
    # A caller that does not pass the caps leaves them alone.
    loc = locations.update_location(
        w.db, w.admin, w.area_b.id, name=w.area_b.name, address=None, city=None, notes=None,
        custom_fields=None, default_fight_count=None, default_payout_model=None,
        default_house_cut_bps=None)
    assert loc.bola_limit_per_line_cents == 50_000
    # "0" is a real value (no cap), not blank.
    loc = locations.update_location(
        w.db, w.admin, w.area_b.id, name=w.area_b.name, address=None, city=None, notes=None,
        custom_fields=None, default_fight_count=None, default_payout_model=None,
        default_house_cut_bps=None, bola_limit_per_line="0", bola_max_liability_per_outcome="",
        bola_max_liability_per_draw="")
    assert loc.bola_limit_per_line_cents == 0 and loc.bola_max_liability_per_outcome_cents is None


@pytest.mark.parametrize("bad", ["abc", "-5"])
def test_bad_cap_values_are_refused(two_areas, bad):
    w = two_areas
    with pytest.raises(ValidationError):
        locations.update_location(
            w.db, w.admin, w.area_b.id, name=w.area_b.name, address=None, city=None, notes=None,
            custom_fields=None, default_fight_count=None, default_payout_model=None,
            default_house_cut_bps=None, bola_limit_per_line=bad, bola_max_liability_per_outcome="",
            bola_max_liability_per_draw="")


def test_admin_form_saves_and_shows_the_caps_over_http(two_areas):
    from tests.test_v2 import _csrf, _login

    w = two_areas
    w.db.flush()
    w.db.commit()
    c = _login("admin")
    page = c.get("/admin/locations").text
    assert 'name="bola_limit_per_line"' in page and "Bola caps for this area" in page
    r = c.post(f"/admin/locations/{w.area_b.id}/update", data={
        "csrf_token": _csrf(c), "name": w.area_b.name,
        "bola_limit_per_line": "750", "bola_max_liability_per_outcome": "",
        "bola_max_liability_per_draw": "0"})
    assert r.status_code == 303
    w.db.expire_all()
    assert (w.area_b.bola_limit_per_line_cents, w.area_b.bola_max_liability_per_outcome_cents,
            w.area_b.bola_max_liability_per_draw_cents) == (75_000, None, 0)
    html = c.get("/admin/locations").text
    assert "line ₱750.00" in html and "draw no cap" in html

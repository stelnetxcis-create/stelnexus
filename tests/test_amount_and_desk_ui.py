"""Number-game slip: one unified amount control. Declarator desk: spectate shortcut, clock and keys."""
from tests.test_bola import _web_setup, _web_teardown
from tests.test_web import login


def test_slip_has_one_unified_amount_control():
    ids = _web_setup()
    try:
        c, _ = login("admin")
        html = c.get(f"/bola/draws/{ids['draw']}").text
        assert 'data-slot="tumbok"' in html and 'data-slot="sahod"' in html          # the two amount pills
        assert 'data-chip="20"' in html and 'data-chip="0"' in html                   # quick amounts + "None"
        # the real fields stay (the server is unchanged) but are hidden: nothing to type per side any more
        assert '<input type="hidden" name="tumbok"' in html and '<input type="hidden" name="sahod"' in html
        assert 'data-target="' not in html and "Tumbok ₱ (per line)" not in html and "Sahod ₱ (per line)" not in html
    finally:
        _web_teardown(ids)


def test_desk_has_spectate_clock_and_keys(world):
    from tests.test_declarator_desk import _desk
    c, _ = _desk(world)
    html = c.get("/staff/declarator").text
    assert "data-clock" in html and "watch live" in html and "next due" in html
    css = open("app/static/css/declarator.css", encoding="utf-8").read()
    assert "--lane-air:var(--air)" in css and "--bc:var(--air)" in css   # on air is ivory; red is Meron's


def test_bet_panel_has_additive_chips_clear_and_stack(world):
    c, _ = login("admin")        # the panel is for accounts that hold credit; a Declarator has none, so it is not drawn
    html = c.get(f"/change_name?event_id={world.event.id}&fight_id={world.games[0].id}").text
    assert 'data-amt="100"' in html and ">+100<" in html                # chips say they add
    assert "data-clear" in html and 'data-sl="stack"' in html           # Clear + the stack of tapped chips

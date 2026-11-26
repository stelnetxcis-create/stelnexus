"""Phone held upright and not full screen: the picture is just the picture; everything else lives in full screen."""
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_dock_gates_touch_devices_that_are_narrow_and_not_full_screen():
    js = _read("app/static/js/dock.js")
    assert "(pointer: coarse)" in js                                         # touch devices only: narrow desktop windows keep the stacked layout
    assert "stage.classList.toggle('fv-gate', !on && coarse.matches)" in js  # `on` = full screen or a wide stage
    assert "fv-gate-msg" in js and "data-fv-gate-full" in js                  # the message and its Full screen button
    assert "ev.target.closest('[data-fv-strip]')" in js and "stopImmediatePropagation" in js   # the strip does not respond, the message does
    assert "}, true);" in js                                                  # capture phase: before the strip's own handlers


def test_css_hides_the_panels_and_shows_the_message_only_when_gated():
    css = _read("app/static/css/store.css")
    for sel in ("[data-fc-overlay]", "[data-bp]", ".pbs", ".pbs-fab", ".dk-bar"):
        assert f".fc-stagewrap.fv-gate {sel}" in css, sel
    assert ".fv-gate-msg{display:none}" in css                               # not shown anywhere else
    assert ".fc-stagewrap.fv-gate .fv-gate-msg{display:flex" in css
    assert "prefers-reduced-motion" in css


def test_fight_page_still_ships_the_panels_the_gate_hides_in_full_screen(world):
    """The gate only hides them with CSS; full screen needs them in the page."""
    from tests.test_web import login
    c, _ = login("admin")
    r = c.get(f"/events/{world.event.id}?fight_id={world.games[0].id}")     # /change_name is only a redirect now
    assert r.status_code == 200
    html = r.text
    assert "data-stage" in html and "data-pbs" in html and "data-bp" in html and "data-fc-overlay" in html
    assert "data-fv-strip" in html and "data-vp-full" in html

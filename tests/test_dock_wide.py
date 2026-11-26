"""Wide PC stream layout: payout top left, bet slip lower left, chat on the right, all open together (dock.js + store.css).
The behaviour test runs the real dock.js in a fake page (jsdom); it is skipped when jsdom is not installed
(npm install --no-save jsdom)."""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _jsdom_available() -> bool:
    node = shutil.which("node")
    if not node:
        return False
    return subprocess.run([node, "-e", "require('jsdom')"], cwd=ROOT, capture_output=True).returncode == 0


@pytest.mark.skipif(not _jsdom_available(), reason="node + jsdom not installed (npm install --no-save jsdom)")
def test_dock_behaviour_in_a_fake_page():
    out = subprocess.run(["node", os.path.join("tests", "js", "dock_wide.cjs")], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stdout[-1500:] + out.stderr[-500:]
    assert "ALL PASSED" in out.stdout and "FAIL" not in out.stdout


def test_dock_script_only_goes_wide_for_a_mouse_on_a_big_picture():
    js = _read("app", "static", "js", "dock.js")
    assert "WIDE_MIN_W = 1000" in js and "(hover: hover) and (pointer: fine)" in js
    assert "stage.classList.toggle('dk-wide', wide)" in js
    assert "&& !isWide()) setOpen(other, false)" in js              # one-at-a-time only outside the wide layout


def test_wide_layout_css_places_the_three_things():
    css = _read("app", "static", "css", "store.css")
    for needle in (
        ".fc-stagewrap.dk-wide .pbs{place-items:start start}",                                       # payout top left
        ".fc-stagewrap.dk-wide .fc-overlay:not(.bp){left:auto;right:max(12px",                      # chat: right
        ".fc-stagewrap.dk-wide .bp.fc-overlay{left:max(12px",                                        # bet: left
        ".fc-stagewrap.dk-wide .bp.fc-overlay:not(.is-collapsed){top:auto;bottom:calc(",            # ... at the bottom
        "var(--dk-pbs-h,0px)",                                                                       # ... clear of the payout card
        ".fc-stagewrap.dk-wide .bp .bp-scroll{grid-template-columns:1fr 1fr",                        # two columns: short slip
        ".fc-stagewrap.dk-wide .bp .sell-chips{grid-template-columns:repeat(3",
        ".fc-stagewrap.dk-wide .dk-feed{left:auto;right:",                                           # drifting chat lines move right
    ):
        assert needle in css, needle
    # everything for the wide layout is behind dk-wide: nothing changes for the existing layouts
    tail = css.split("WIDE PC LAYOUT")[1]
    assert all(line.startswith(".fc-stagewrap.dk-wide") or not line.strip() or line.startswith(("/*", " ", "*", "="))
               for line in tail.splitlines() if line.startswith(".")), "a wide-layout rule leaks outside dk-wide"

"""The motion pass: page transition, menu stagger, badge / empty-state easing, submit spinner.
These are source-level guards (the rules must stay behind the reduced-motion / minimal-mode switches); the look itself
is checked in a browser. Scroll reveal, the loading bar, ripples and count-up already exist in arena.js and must not be
duplicated."""
import re
from pathlib import Path

STATIC = Path("app/static")


def _motion_block():
    css = (STATIC / "css/theme.css").read_text("utf-8")
    start = css.index("MOTION PASS")
    return css[start:]


def _inside(block, anchor):
    """True when `anchor` appears inside a prefers-reduced-motion: no-preference media block."""
    for m in re.finditer(r"@media \(prefers-reduced-motion:no-preference\)\{", block):
        depth, i = 1, m.end()
        while depth and i < len(block):
            depth += {"{": 1, "}": -1}.get(block[i], 0)
            i += 1
        if anchor in block[m.end():i]:
            return True
    return False


def test_page_transition_is_opt_in_to_motion_and_skipped_in_minimal_mode():
    b = _motion_block()
    assert _inside(b, "@view-transition{navigation:auto}")
    for name in ("vt-sidebar", "vt-topbar", "vt-tabbar"):
        assert _inside(b, f"view-transition-name:{name}")
    assert _inside(b, "html.fx-min::view-transition-new(root){animation:none}")      # phones switch pages instantly
    assert "@keyframes vt-in" in b and "@keyframes vt-out" in b


def test_live_video_pages_opt_out_of_the_page_transition():
    assert (STATIC / "css/livestream.css").read_text("utf-8").lstrip().startswith("/*") and \
           "@view-transition{navigation:none}" in (STATIC / "css/livestream.css").read_text("utf-8")[:300]


def test_menu_items_stagger_only_with_motion_allowed_and_not_in_minimal_mode():
    b = _motion_block()
    assert _inside(b, "html:not(.fx-min) .menu[open] .menu-panel>*{animation:menu-item-in")
    assert ".account-item:hover" in b and "@media (hover:hover)" in b                # no hover effects on touch screens


def test_empty_states_fade_up_with_motion_allowed():
    b = _motion_block()
    assert _inside(b, "html:not(.fx-min) .empty,html:not(.fx-min) .empty-art{animation:empty-in")


def test_badges_ease_between_states():
    b = _motion_block()
    assert re.search(r"\.badge\{transition:[^}]*background-color[^}]*color[^}]*\}", b)


def test_submit_spinner_has_its_own_rotation_keyframes():
    """The page's existing `spin` keyframes animate an angle variable (a conic gradient), not a rotation."""
    b = _motion_block()
    assert "@keyframes btn-spin{to{transform:rotate(360deg)}}" in b
    assert "animation:btn-spin" in b
    app = (STATIC / "js/app.js").read_text("utf-8")
    assert "classList.add('is-working')" in app and "classList.remove('is-working')" in app


def test_nothing_that_already_exists_is_added_twice():
    arena = (STATIC / "js/arena.js").read_text("utf-8")
    assert "nav-progress" in arena and "rv-in" in arena                                # the loading bar and scroll reveal live here
    assert not (STATIC / "js/motion.js").exists()
    css = (STATIC / "css/theme.css").read_text("utf-8")
    assert css.count(".nav-progress{position:fixed") == 1 and css.count(".rv{opacity:0}") == 1

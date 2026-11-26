"""The unified game cards (.gcard) keep their artwork.

The one-look cards replaced the old `.games .game-tile` tiles, whose pictures were drawn by `.games .game-tile::before`. A refactor
of the markup once silently dropped every picture because nothing styled `.gcard` that way. These tests fail loudly if it happens again.
"""
import re
from pathlib import Path

from tests.test_web import login

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
THEME = (STATIC / "css" / "theme.css").read_text()


def _cards(path):
    c, r = login("admin")
    assert r.status_code == 303
    page = c.get(path)
    assert page.status_code == 200
    return set(re.findall(r'<article class="gcard[^"]*" data-game="([a-z_]+)"', page.text))


def _art_for(key):
    """The SVG path the stylesheet gives to the card with data-game=<key> (selector lists such as bola + number are handled)."""
    match = re.search(r'data-game="%s"\][^{}]*\{--gc-art:url\((/static/img/games/[a-z]+\.svg)\)\}' % re.escape(key), THEME)
    return match.group(1) if match else None


def test_every_game_card_on_the_dashboard_and_the_hub_has_its_picture():
    for path in ("/dashboard", "/games"):
        cards = _cards(path)
        assert {"sabong", "bola"} <= cards, (path, cards)                      # the two playable games
        for key in cards:
            art = _art_for(key)
            assert art, f"{path}: the {key} card has no artwork rule in theme.css"
            assert (STATIC / art.removeprefix("/static/")).is_file(), art      # ... and the file exists


def test_the_announced_games_keep_their_pictures_too():
    cards = _cards("/dashboard")
    for key in ("esports", "nba"):
        if key in cards:
            assert _art_for(key), key


def test_the_artwork_is_decoration_behind_the_content_and_respects_low_motion_modes():
    block = THEME[THEME.index("unified game cards (.gcard): the artwork"):]
    block = block[:block.index("/* ---------- Bola area cards")]
    assert "pointer-events:none" in block and "z-index:-1" in block            # never covers the buttons or the numbers
    assert ".fx-lite .gcard::before{animation:none}" in block                   # low-power mode: no looping motion
    assert "prefers-reduced-motion:reduce" in block                             # reduced-motion users: no motion either

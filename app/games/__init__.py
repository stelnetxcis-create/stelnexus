"""Game registry (Games Standard, plan 4.6).

Shared pages read from ``GAMES``; they never import a game's code directly. To add a game: write
``app/games/<key>.py`` with a ``GameAdapter`` subclass and add one line here.
"""
from __future__ import annotations

from app.games.base import RECORD_TABS, RIGHTS, TAB_LABELS, WORKSPACE_TABS, Filters, GameAdapter, can_see_money
from app.games.bola import BolaAdapter
from app.games.sabong import SabongAdapter

GAMES: dict[str, GameAdapter] = {a.key: a for a in (SabongAdapter(), BolaAdapter())}

__all__ = ["GAMES", "RECORD_TABS", "RIGHTS", "TAB_LABELS", "WORKSPACE_TABS", "Filters", "GameAdapter",
           "can_see_money", "get", "visible_games"]


def get(key: str) -> GameAdapter | None:
    return GAMES.get(key)


def visible_games(ctx) -> list[GameAdapter]:
    """Games this account may see at all, in registry order."""
    return [g for g in GAMES.values() if g.visible(ctx)]

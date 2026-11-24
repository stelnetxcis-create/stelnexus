"""Games hub (/games) and per-game workspace entry points (/games/{key}/{tab}).

Phase 1 of the Games Standard: workspace tabs resolve through the adapter to wherever the page lives
today, so old URLs keep working and a game that moves a page only changes its adapter.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app import games
from app.core.errors import NotFound
from app.games import WORKSPACE_TABS
from app.games.base import SCHEDULE_FILTERS
from app.web.deps import Ctx, render, require_ctx

router = APIRouter()

GAME_COOKIE = "sn_game"


def remember(response, key: str) -> None:
    """The last game switcher choice, per browser (cookie, not DB)."""
    response.set_cookie(GAME_COOKIE, key, max_age=60 * 60 * 24 * 365, samesite="lax", httponly=True)


def game_cards(ctx: Ctx) -> list[dict]:
    """The cards on the Games hub and on the Dashboard: one per game this account may see. Each has ONE way in
    (/games/<key>/live): playing and managing a game are the same place, and what you can do there follows your role."""
    cards = []
    for adapter in games.visible_games(ctx):
        cards.append({"game": adapter, "urls": adapter.workspace_urls(ctx), "summary": adapter.summary_today(ctx)})
    return cards


@router.get("/games")
async def games_hub(ctx: Ctx = Depends(require_ctx)):
    return render(ctx, "games_hub.html", active="games", cards=game_cards(ctx))


@router.get("/games/{key}")
async def game_home(key: str, ctx: Ctx = Depends(require_ctx)):
    return RedirectResponse(f"/games/{key}/live", status_code=303)


@router.get("/games/{key}/{tab}")
async def game_tab(key: str, tab: str, request: Request, ctx: Ctx = Depends(require_ctx)):
    adapter = games.get(key)
    if adapter is None or tab not in WORKSPACE_TABS or not adapter.visible(ctx):
        raise NotFound("That page does not exist.")
    target = adapter.workspace_urls(ctx).get(tab)
    if not target:
        raise NotFound("That page does not exist.")
    if tab == "schedule" and adapter.schedule_view == "cards" and target == f"/games/{key}/schedule":
        # Sabong: the events list (with Create event) IS the game's page, so the old "Manage" address lands there.
        query = request.url.query
        response = RedirectResponse(f"/games/{key}/live?pick=1" + (f"&{query}" if query else ""), status_code=303)
        remember(response, key)
        return response
    if tab == "schedule" and target == f"/games/{key}/schedule":
        # the shared page renders in place; `?create=1` (old link) still opens the create drawer via pages.js
        when = request.query_params.get("when")
        when = when if when in SCHEDULE_FILTERS else adapter.schedule_default
        if adapter.schedule_view == "cards":
            extra = adapter.schedule_context(ctx, when)
            rows = []
        else:
            extra, rows = {}, adapter.schedule(ctx, when)
        response = render(ctx, "games_schedule.html", active=adapter.nav_active[0] if adapter.nav_active else "games",
                          game=adapter, when=when, filters=SCHEDULE_FILTERS, rows=rows,
                          spec=adapter.create_spec(ctx), **extra)
        remember(response, key)
        return response
    if tab == "live" and target == f"/games/{key}/live":
        response = await adapter.live(request, ctx)          # may itself redirect (Sabong: into the one open event)
        remember(response, key)
        return response
    query = request.url.query
    if query:
        target += ("&" if "?" in target else "?") + query
    response = RedirectResponse(target, status_code=303)
    remember(response, key)
    return response

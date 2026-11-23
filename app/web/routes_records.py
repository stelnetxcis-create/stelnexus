"""Records (/records): Tickets, Cancelled, Commissions, History, Reports for every game on one page
(Games Standard plan 4.4). Rows come from the game adapters, already normalized."""
from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from app import games
from app.core.errors import Forbidden, NotFound
from app.games import RECORD_TABS, TAB_LABELS, Filters, can_see_money
from app.games.sabong import usernames  # noqa: F401  (re-exported for tests)
from app.services import bola
from app.web.deps import Ctx, render, require_ctx
from app.web.paging import make_pager, page_number
from app.web.routes_games import GAME_COOKIE, remember

router = APIRouter()
PER_PAGE = 100
ARCHIVE_PER_PAGE = 50
# right needed per tab; every game answers it through adapter.can(ctx, right)
TAB_RIGHT = {"tickets": "tickets", "cancelled": "cancel", "history": "view", "reports": "report"}


def _safe(value: str) -> str:
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _date(raw: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def legacy_redirect(request: Request, tab: str, game: str | None = None, rename: dict | None = None,
                    extra: dict | None = None):
    """Old page URL -> its Records tab, keeping the filters the old page understood (plan 4.8)."""
    from urllib.parse import urlencode

    from fastapi.responses import RedirectResponse
    query = {"tab": tab}
    if game:
        query["game"] = game
    for key, value in request.query_params.items():
        query[(rename or {}).get(key, key)] = value
    query.update(extra or {})
    return RedirectResponse("/records?" + urlencode(query), status_code=303)


def _today() -> dt.date:
    return bola.utcnow().astimezone(bola.MANILA).date()


def allowed_tabs(ctx: Ctx) -> list[str]:
    visible = games.visible_games(ctx)
    money = can_see_money(ctx)
    out = []
    for tab in RECORD_TABS:
        if tab == "commissions":
            ok = ctx.can("commissions.view") and money
        elif tab == "history":
            ok = bool(visible)
        else:
            ok = money and any(g.can(ctx, TAB_RIGHT[tab]) for g in visible)
        if ok:
            out.append(tab)
    return out


def games_for(ctx: Ctx, tab: str, wanted: str) -> list:
    right = TAB_RIGHT.get(tab, "view")
    pool = [g for g in games.visible_games(ctx) if g.can(ctx, right)]
    if wanted in games.GAMES:
        pool = [g for g in pool if g.key == wanted]
    return pool


def resolve(request: Request, ctx: Ctx) -> tuple[str, str, list[str]]:
    """(tab, game, tabs allowed). Game falls back to the cookie, then 'all'."""
    tabs = allowed_tabs(ctx)
    if not tabs:
        raise Forbidden("Your role cannot see records.")
    tab = request.query_params.get("tab") or tabs[0]
    if tab not in RECORD_TABS:
        raise NotFound("That tab does not exist.")
    if tab not in tabs:
        raise Forbidden("Your role cannot see this tab.")
    game = request.query_params.get("game") or request.cookies.get(GAME_COOKIE) or "all"
    return tab, (game if game in games.GAMES else "all"), tabs


def _merge(rows: list, offset: int, limit: int) -> list:
    rows.sort(key=lambda r: (r.at is not None, r.at), reverse=True)
    return rows[offset:offset + limit]


def rows_for(ctx: Ctx, tab: str, game: str, f: Filters) -> list:
    """One page (plus one look-ahead row) across the chosen games, newest first."""
    pool = games_for(ctx, tab, game)
    fetch = Filters(f.start, f.end, f.status, f.q, limit=f.offset + f.limit, offset=0) if len(pool) > 1 else f
    rows: list = []
    for g in pool:
        rows += getattr(g, tab)(ctx, fetch)
    return _merge(rows, f.offset if len(pool) > 1 else 0, f.limit) if len(pool) > 1 else rows


def build_filters(request: Request, per_page: int, *, csv_mode: bool = False) -> Filters:
    p = request.query_params
    page = 0 if csv_mode else page_number(request)
    return Filters(start=_date(p.get("start")), end=_date(p.get("end")),
                   status=p.get("status") or None, q=(p.get("q") or "")[:40] or None,
                   session_id=_int(p.get("session")), round_id=_int(p.get("round")),
                   limit=per_page + 1 if not csv_mode else 5000, offset=page * per_page)


def report_rows(ctx: Ctx, game: str, p) -> tuple[str, list[tuple[str, object]], dt.date | str | None]:
    """(view, [(game_label, ReportRow)], what was asked). View: daily | monthly | session."""
    pool = games_for(ctx, "reports", game)
    view = p.get("view") if p.get("view") in ("daily", "monthly", "session", "archive") else "daily"
    out: list[tuple[str, object]] = []
    asked: dt.date | str | None = None
    if view == "daily":
        day = _date(p.get("day")) or _today()
        asked = day
        for g in pool:
            out += [(g.label, r) for r in g.report_daily(ctx, day)]
    elif view == "monthly":
        raw = p.get("month") or _today().strftime("%Y-%m")
        try:
            year, month = (int(x) for x in raw.split("-"))
            dt.date(year, month, 1)
        except ValueError:
            year, month = _today().year, _today().month
        asked = f"{year:04d}-{month:02d}"
        for g in pool:
            out += [(g.label, r) for r in g.report_monthly(ctx, year, month)]
    elif view == "archive":
        f = Filters(limit=ARCHIVE_PER_PAGE + 1, offset=(_int(p.get("page")) or 0) * ARCHIVE_PER_PAGE)
        for g in pool:
            out += [(g.label, r) for r in g.report_archive(ctx, f)]
    else:
        if len(pool) != 1:
            raise NotFound("Pick one game to see a session.")
        try:
            sid = int(p.get("session", ""))
        except ValueError:
            raise NotFound("Pick a session.")
        asked = str(sid)
        out = [(pool[0].label, r) for r in pool[0].report_session(ctx, sid)]
    return view, out, asked


def totals_of(rows: list[tuple[str, object]]) -> dict:
    keys = ("staked", "paid", "house", "commission", "expenses", "net")
    return {k: sum(getattr(r, k) for _, r in rows) for k in keys}


@router.get("/records")
async def records(request: Request, ctx: Ctx = Depends(require_ctx)):
    tab, game, tabs = resolve(request, ctx)
    if tab == "commissions":
        from app.web.routes_money import commissions_page
        return await commissions_page(request, ctx)
    ctx_extra: dict = {}
    pager = None
    rows: list = []
    p = request.query_params
    if tab == "reports":
        view, rows, asked = report_rows(ctx, game, p)
        if view == "archive":
            has_next = len(rows) > ARCHIVE_PER_PAGE
            rows = rows[:ARCHIVE_PER_PAGE]
            pager = make_pager(request, ARCHIVE_PER_PAGE, page_number(request), len(rows), has_next)
        ctx_extra.update(view=view, asked=asked, totals=totals_of(rows),
                         has_result=any(r.result for _, r in rows),
                         has_tickets=any(r.tickets is not None for _, r in rows),
                         day=asked if view == "daily" else _today(),
                         month=asked if view == "monthly" else _today().strftime("%Y-%m"))
    else:
        f = build_filters(request, PER_PAGE)
        rows = rows_for(ctx, tab, game, f)
        has_next = len(rows) > PER_PAGE
        rows = rows[:PER_PAGE]
        pager = make_pager(request, PER_PAGE, page_number(request), len(rows), has_next)
        if tab in ("tickets", "cancelled"):
            live = [r for r in rows if r.status != "canceled" and r.stake is not None]
            ctx_extra["kpis"] = {"staked": sum(r.stake for r in live), "paid": sum(r.payout or 0 for r in live)} \
                if can_see_money(ctx) else None
    response = render(ctx, "records/index.html", active="records", tab=tab, game=game, tabs=tabs,
                      tab_labels=TAB_LABELS, rows=rows, pager=pager, can_see_money=can_see_money(ctx),
                      game_choices=[g for g in games.visible_games(ctx)],
                      filters={"start": p.get("start", ""), "end": p.get("end", ""),
                               "status": p.get("status", ""), "q": p.get("q", "")},
                      show_game=game == "all" and len(games.visible_games(ctx)) > 1, **ctx_extra)
    if p.get("game") in games.GAMES or p.get("game") == "all":
        remember(response, p["game"])
    return response


def _money(cents) -> str:
    return "" if cents is None else f"{cents / 100:.2f}"


@router.get("/records.csv")
async def records_csv(request: Request, ctx: Ctx = Depends(require_ctx)):
    tab, game, _tabs = resolve(request, ctx)
    if tab == "commissions":
        raise NotFound("Use /commissions.csv for statements.")
    out = io.StringIO()
    w = csv.writer(out)
    if tab == "reports":
        view, rows, asked = report_rows(ctx, game, request.query_params)
        w.writerow(["Game", "Label", "Staked", "Paid out", "House", "Commission", "Expenses", "Net"])
        for label, r in rows:
            w.writerow([label, _safe(r.label), _money(r.staked), _money(r.paid), _money(r.house),
                        _money(r.commission), _money(r.expenses), _money(r.net)])
        name = f"reports-{view}-{asked}"
    else:
        rows = rows_for(ctx, tab, game, build_filters(request, PER_PAGE, csv_mode=True))
        if tab == "history":
            w.writerow(["Game", "Session", "Round", "Result", "Time", "Detail"])
            for r in rows:
                w.writerow([r.game, _safe(r.session), r.round_label, r.result, r.at.isoformat() if r.at else "", r.detail])
        elif tab == "cancelled":
            w.writerow(["Time", "Code", "Game", "Round", "Stake", "Cancelled by", "Reason"])
            for r in rows:
                w.writerow([r.cancelled_at.isoformat() if r.cancelled_at else "", r.code, r.game,
                            _safe(r.round_label), _money(r.stake), _safe(r.cancelled_by), _safe(r.reason)])
        else:
            w.writerow(["Time", "Code", "Game", "Round", "Pick", "Stake", "Status", "Payout"])
            for r in rows:
                w.writerow([r.at.isoformat() if r.at else "", r.code, r.game, _safe(r.round_label),
                            r.pick, _money(r.stake), r.status, _money(r.payout)])
        name = f"{tab}-{game}"
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{name}.csv"', "Cache-Control": "no-store"})

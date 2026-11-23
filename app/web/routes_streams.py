"""Admin Panel -> Streams (paste a link, preview it, take it live) and the public payload the player polls."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core import ttlcache
from app.core.errors import NotFound, ValidationError
from app.core.rbac import Perm
from app.core.settings_registry import SettingsStore
from sqlalchemy import select

from app.db.models import Event, Fight
from app.services import streams
from app.web.deps import Ctx, flash_redirect, form_data, require_ctx
from app.web.routes_admin import panel
from app.web.routes_live import _reply

router = APIRouter()


def _bad_ids(request: Request) -> frozenset[int]:
    """Sources this viewer's player could not play (``?bad=3,7``). Only ever skips ahead in the chain for that viewer."""
    raw = request.query_params.get("bad", "")
    return frozenset(int(x) for x in raw.split(",")[:5] if x.strip().isdigit())


def _origin(request: Request) -> str:
    return "https://" + (request.headers.get("host") or request.url.netloc)


def _assignment_rows(db, rows, sources):
    """Plain labels for the assignments table (scope, chain by name, window in Philippine time)."""
    ev_ids = {a.scope_id for a in rows if a.scope_type == "event"}
    fi_ids = {a.scope_id for a in rows if a.scope_type == "fight"}
    fights = {f.id: f for f in db.scalars(select(Fight).where(Fight.id.in_(fi_ids)))} if fi_ids else {}
    ev_ids |= {f.event_id for f in fights.values()}
    evs = {e.id: e for e in db.scalars(select(Event).where(Event.id.in_(ev_ids)))} if ev_ids else {}
    names = {x.id: x.name for x in sources}
    out = []
    for a in rows:
        if a.scope_type == "global":
            scope = "Everything"
        elif a.scope_type == "event":
            e = evs.get(a.scope_id)
            scope = f"Event: {e.title}" if e else "Event (removed)"
        else:
            f = fights.get(a.scope_id)
            e = evs.get(f.event_id) if f else None
            scope = f"Fight #{f.number} of {e.title}" if f and e else "Game (removed)"
        win = " → ".join(x for x in (streams.fmt_local(a.starts_at) or "…", streams.fmt_local(a.ends_at) or "…") if x) \
            if (a.starts_at or a.ends_at) else "always"
        out.append({"id": a.id, "scope": scope, "mode": streams.MODE_LABEL[a.mode], "active": a.active, "window": win,
                    "chain": [names.get(i, "(removed)") for i in streams.chain_ids(a)], "pinned": bool(a.pinned_source_id),
                    "slate": a.slate_message})
    return out


@router.get("/admin/streams")
async def streams_page(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_VIEW)
    rows = streams.list_sources(ctx.db)
    st = SettingsStore(ctx.db)
    assigns = streams.list_assignments(ctx.db)
    events = ctx.db.execute(select(Event.id, Event.title, Event.event_date).order_by(Event.id.desc()).limit(60)).all()
    return panel(ctx, "streams", "admin_streams.html", sources=rows, live_src=next((r for r in rows if r.is_active), None),
                 kind_label=streams.KIND_LABEL, native=streams.NATIVE, stream_on=bool(st.get("stream.enabled")),
                 can_manage=ctx.can(Perm.STREAMS_MANAGE), assignments=_assignment_rows(ctx.db, assigns, rows),
                 events=events, modes=streams.MODE_LABEL, usable=[r for r in rows if r.enabled],
                 failovers=streams.failovers(ctx.db), fmt_time=streams.fmt_local)


@router.post("/admin/streams/detect")
async def streams_detect(request: Request, ctx: Ctx = Depends(require_ctx)):
    """Detect + check a pasted link (nothing is saved). Plain-language result for the form."""
    ctx.need(Perm.STREAMS_MANAGE)
    data = await form_data(request)
    try:
        det = streams.detect(data.get("url", ""), allowed_hosts=streams.allowed_host_set(SettingsStore(ctx.db)))
        checks = await run_in_threadpool(streams.probe, det, site_origin=_origin(request))
    except ValidationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})
    return JSONResponse({"ok": True, "kind": det.kind, "kind_label": streams.KIND_LABEL[det.kind], "label": det.label,
                         "native": det.kind in streams.NATIVE, **checks})


@router.post("/admin/streams/create")
async def streams_create(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    data = await form_data(request)
    try:
        start = int(data["start"]) if data.get("start", "").strip() else None
    except ValueError as exc:
        raise ValidationError("Start time must be a whole number of seconds.") from exc
    row = await run_in_threadpool(
        streams.create_source, ctx.db, ctx.user, name=data.get("name", ""), raw_url=data.get("url", ""),
        default_mode=data.get("mode", "auto"), loop=data.get("loop") == "1", start_at=start,
        settings=SettingsStore(ctx.db))
    return flash_redirect("/admin/streams", f"'{row.name}' saved as {streams.KIND_LABEL[row.kind]}. "
                                            "Preview it, then take it live.")


@router.post("/admin/streams/{source_id}/take")
async def streams_take(source_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    row = streams.take(ctx.db, ctx.user, source_id)
    return flash_redirect("/admin/streams", f"'{row.name}' is live for everyone. Open viewers switch within seconds.")


@router.post("/admin/streams/stop")
async def streams_stop(ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    streams.stop(ctx.db, ctx.user)
    return flash_redirect("/admin/streams", "Stopped. Viewers see the sample feed again.")


@router.post("/admin/streams/{source_id}/toggle")
async def streams_toggle(source_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    data = await form_data(request)
    row = streams.set_enabled(ctx.db, ctx.user, source_id, data.get("on") == "1")
    return flash_redirect("/admin/streams", f"'{row.name}' is now {'on' if row.enabled else 'off'}.")


@router.post("/admin/streams/{source_id}/delete")
async def streams_delete(source_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    streams.delete(ctx.db, ctx.user, source_id)
    return flash_redirect("/admin/streams", "Source removed.")


@router.get("/_live/stream")
async def live_stream(request: Request, ctx: Ctx = Depends(require_ctx)):
    """What the player should play now. ETag / 304 like every other live endpoint: an idle poll costs a header."""
    bad = _bad_ids(request)
    data = ttlcache.cached(("live", "stream", tuple(sorted(bad))), 5, lambda: streams.payload(ctx.db, exclude=bad))
    return _reply(request, dict(data))


def _assign_args(ctx: Ctx, data) -> dict:
    """Read the "What plays where" form (shared by Save and the impact warning, so they can never disagree)."""
    scope = data.get("scope", "global")
    scope_id = None
    if scope in ("event", "fight"):
        if not data.get("event_id", "").isdigit():
            raise ValidationError("Choose the event.")
        scope_id = int(data["event_id"])
        if scope == "fight":
            num = data.get("game_number", "").strip()
            if not num.isdigit():
                raise ValidationError("Enter the game number.")
            fight = ctx.db.scalar(select(Fight).where(Fight.event_id == scope_id, Fight.number == int(num)))
            if fight is None:
                raise ValidationError(f"Fight #{num} does not exist in that event.")
            scope_id = fight.id
    chain = [int(data[k]) for k in ("s1", "s2", "s3") if data.get(k, "").isdigit()]
    return dict(scope_type=scope, scope_id=scope_id, mode=data.get("mode", "live"), chain=chain,
                starts_at=streams.parse_local(data.get("starts")), ends_at=streams.parse_local(data.get("ends")))


@router.post("/admin/streams/assign/impact")
async def streams_assign_impact(request: Request, ctx: Ctx = Depends(require_ctx)):
    """The warning shown before saving: who this changes and what it replaces. Saves nothing."""
    ctx.need(Perm.STREAMS_MANAGE)
    data = await form_data(request)
    try:
        return JSONResponse({"ok": True, **streams.impact(ctx.db, **_assign_args(ctx, data))})
    except ValidationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)})


@router.post("/admin/streams/assign")
async def streams_assign(request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    data = await form_data(request)
    row = streams.create_assignment(
        ctx.db, ctx.user, **_assign_args(ctx, data), pin_first=data.get("pin") == "1",
        slate_message=data.get("slate", ""))
    return flash_redirect("/admin/streams", f"Saved: {streams.MODE_LABEL[row.mode]}. Open viewers switch within seconds. "
                                            "Not what you wanted? Use Undo in the list below.")


@router.post("/admin/streams/assignments/{assignment_id}/undo")
async def streams_assignment_undo(assignment_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    return flash_redirect("/admin/streams", streams.undo_assignment(ctx.db, ctx.user, assignment_id))


@router.post("/admin/streams/{source_id}/check")
async def streams_check_now(source_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    row = await run_in_threadpool(streams.check_now, ctx.db, ctx.user, source_id)
    return flash_redirect("/admin/streams", f"'{row.name}': {row.health.upper()}. {row.health_detail or ''}")


@router.post("/admin/streams/assignments/{assignment_id}/toggle")
async def streams_assignment_toggle(assignment_id: int, request: Request, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    data = await form_data(request)
    row = streams.set_assignment_active(ctx.db, ctx.user, assignment_id, data.get("on") == "1")
    return flash_redirect("/admin/streams", "Setting resumed." if row.active else "Setting paused. It no longer applies.")


@router.post("/admin/streams/assignments/{assignment_id}/delete")
async def streams_assignment_delete(assignment_id: int, ctx: Ctx = Depends(require_ctx)):
    ctx.need(Perm.STREAMS_MANAGE)
    streams.delete_assignment(ctx.db, ctx.user, assignment_id)
    return flash_redirect("/admin/streams", "Setting removed.")


@router.get("/_live/stream/{scope}")
async def live_stream_scoped(scope: str, request: Request, ctx: Ctx = Depends(require_ctx)):
    """Per event (``e12``) or per game (``f345``). Same ETag / 304 and shared cache as the global one."""
    m = re.fullmatch(r"([ef])(\d{1,9})", scope)
    if not m:
        raise NotFound("Unknown stream scope.")
    event_id = fight_id = None
    if m.group(1) == "f":
        fight = ctx.db.get(Fight, int(m.group(2)))
        if fight is None:
            raise NotFound("Unknown game.")
        fight_id, event_id = fight.id, fight.event_id
    else:
        event_id = int(m.group(2))
    bad = _bad_ids(request)
    data = ttlcache.cached(("live", "stream", event_id, fight_id, tuple(sorted(bad))), 5,
                           lambda: streams.payload(ctx.db, event_id=event_id, fight_id=fight_id, exclude=bad))
    return _reply(request, dict(data))

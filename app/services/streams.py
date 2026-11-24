"""Admin-configurable livestream / video sources (Admin Panel -> Streams).

The admin pastes a link; the server works out what it is, checks it, builds the safe embed itself and stores
it as a ``StreamSource``. Admin HTML is never stored or rendered: an ``<iframe ...>`` paste is reduced to its
``src`` and everything else is thrown away. Detection is a short list of adapters, so a new provider is one
more branch in :func:`detect`, not a redesign.

Security notes (all enforced here, not in the browser):
  * https only; no credentials in the URL; no localhost / private / link-local / metadata addresses;
  * probing resolves DNS, rejects non-public addresses, then connects to the *validated IP* (no second lookup),
    follows at most 3 redirects (each re-checked), 5 s timeout, 1 MB cap, no cookies;
  * the Content-Security-Policy only ever gains the hosts of enabled sources (:func:`extend_csp`), never ``*``.
"""
from __future__ import annotations

import datetime as dt
import html
import http.client
import ipaddress
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urljoin, urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import audit, ttlcache
from app.core.errors import NotFound, ValidationError
from app.core.settings_registry import SettingsStore
from app.db.models import Event, Fight, StreamAssignment, StreamSource, User
from app.db.session import session_scope

KINDS = ("hls", "file", "youtube", "facebook", "embed")
KIND_LABEL = {"hls": "HLS live stream", "file": "Video file", "youtube": "YouTube", "facebook": "Facebook",
              "embed": "Embedded page"}
NATIVE = ("hls", "file")                      # played by our own <video>: Battle Pass rules can be enforced
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
            "youtube-nocookie.com", "www.youtube-nocookie.com"}
FB_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.watch"}
FILE_EXT = (".mp4", ".webm", ".m4v", ".ogv")
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_UA = "StelNexus-StreamCheck/1.0"
RTMP_HELP = ("Browsers cannot play RTMP. Send it to a media server that turns it into HLS (a link ending in .m3u8) "
             "and paste that link, or paste a YouTube / Facebook live link.")


@dataclass
class Detected:
    kind: str
    url: str
    embed_ref: str | None
    host: str                                  # host[:port] without credentials
    label: str
    start: int | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- link parsing

def extract_url(raw: str) -> str:
    """The link inside what the admin pasted. An iframe paste keeps only its ``src``; the rest is discarded."""
    text = (raw or "").strip()
    if not text:
        raise ValidationError("Paste a link first.")
    if "<" in text:
        m = re.search(r"""\bsrc\s*=\s*["']([^"']+)["']""", text, re.I)
        if not m:
            raise ValidationError("That looks like code, not a link. Paste the video link itself.")
        text = html.unescape(m.group(1)).strip()
    if re.search(r"\s", text):
        raise ValidationError("A link cannot contain spaces. Paste just the link.")
    if len(text) > 1000:
        raise ValidationError("That link is too long.")
    return text


def _start_seconds(value: str) -> int | None:
    value = (value or "").strip().lower()
    if not value:
        return None
    if value.isdigit():
        return int(value) or None
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if not m or not any(m.groups()):
        return None
    h, mi, s = (int(g or 0) for g in m.groups())
    return (h * 3600 + mi * 60 + s) or None


def _literal_block(host: str) -> None:
    """Reject obviously internal names and IP literals without touching the network."""
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        raise ValidationError("That address is on a private network. Use a public https link.")
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return
    if not _is_public(addr):
        raise ValidationError("That address is on a private network. Use a public https link.")


def _is_public(addr) -> bool:
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return bool(addr.is_global) and not (addr.is_multicast or addr.is_unspecified)


def parse_https(url: str):
    low = url.lower()
    if low.startswith(("rtmp://", "rtmps://", "rtsp://")):
        raise ValidationError(RTMP_HELP)
    if low.startswith("http://"):
        raise ValidationError("The link must start with https:// (a plain http:// video is blocked by browsers).")
    if not low.startswith("https://"):
        raise ValidationError("Only https:// links are allowed.")
    sp = urlsplit(url)
    if sp.username or sp.password or "@" in sp.netloc:
        raise ValidationError("Links with a username or password in them are not allowed.")
    try:
        sp.port  # noqa: B018  (raises on a bad port)
    except ValueError as exc:
        raise ValidationError("That link has an invalid port number.") from exc
    host = (sp.hostname or "").lower().rstrip(".")
    if not host or "." not in host and ":" not in host:
        raise ValidationError("That link has no valid website address.")
    _literal_block(host)
    return sp, host


def allowed_host_set(settings: SettingsStore | None) -> set[str]:
    raw = str(settings.get("stream.allowed_hosts") or "") if settings is not None else ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def detect(raw: str, *, allowed_hosts: set[str] | frozenset[str] = frozenset()) -> Detected:
    """What kind of link is this, and what is the safe way to play it? Raises ValidationError with plain help."""
    url = extract_url(raw)
    sp, host = parse_https(url)
    netloc = sp.netloc.lower()
    path = sp.path or ""

    if host in YT_HOSTS:
        q = parse_qs(sp.query)
        if host == "youtu.be":
            vid = path.strip("/").split("/")[0]
        elif path == "/watch":
            vid = (q.get("v") or [""])[0]
        else:
            m = re.match(r"^/(?:live|embed|shorts|v)/([^/?#]+)", path)
            vid = m.group(1) if m else ""
        if not _ID.match(vid):
            raise ValidationError("That YouTube link has no video. Open the video and copy its address "
                                  "(youtube.com/watch?v=… or youtu.be/…).")
        start = _start_seconds((q.get("t") or q.get("start") or [""])[0])
        embed = f"https://www.youtube-nocookie.com/embed/{vid}?rel=0&modestbranding=1&playsinline=1"
        if start:
            embed += f"&start={start}"
        return Detected("youtube", url, embed, "www.youtube-nocookie.com", f"YouTube video {vid}", start,
                        ["YouTube plays in its own player: Battle Pass rewind rules are limited on embeds."])

    if host in FB_HOSTS:
        if host != "fb.watch" and not re.search(r"/(videos|watch|reel|reels)(/|\?|$)", path + "?" + sp.query):
            raise ValidationError("That Facebook link is not a video. Open the video and copy its address "
                                  "(facebook.com/…/videos/… or fb.watch/…).")
        embed = "https://www.facebook.com/plugins/video.php?show_text=false&href=" + quote(url, safe="")
        return Detected("facebook", url, embed, "www.facebook.com", "Facebook video", None,
                        ["Facebook plays in its own player: Battle Pass rewind rules are limited on embeds."])

    if path.lower().endswith(".m3u8"):
        return Detected("hls", url, None, netloc, "HLS stream (.m3u8)")
    if path.lower().endswith(FILE_EXT):
        return Detected("file", url, None, netloc, "Video file")

    if host in allowed_hosts or netloc in allowed_hosts:
        return Detected("embed", url, url, netloc, "Embedded page",
                        notes=["A generic page is shown in a sandboxed frame; Battle Pass rules do not apply."])
    raise ValidationError(
        "I do not recognise that link. Use a YouTube or Facebook video, a link ending in .m3u8 (live stream) or "
        ".mp4 / .webm (video file). To embed another site, add its host under Variables -> Streaming -> Extra embed hosts.")


# --------------------------------------------------------------------------- network safety + probe

def assert_public_host(host: str, port: int = 443) -> list[str]:
    """Resolve ``host`` and refuse anything that is not a public address. Returns the validated IPs."""
    _literal_block(host)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ValidationError("I could not find that website address. Check the link for typos.") from exc
    ips = sorted({i[4][0] for i in infos})
    if not ips:
        raise ValidationError("I could not find that website address. Check the link for typos.")
    for ip in ips:
        if not _is_public(ipaddress.ip_address(ip)):
            raise ValidationError("That address is on a private network. Use a public https link.")
    return ips


def _fetch(url: str, *, headers: dict | None = None, max_bytes: int = 1_000_000, hops: int = 3, timeout: float = 5.0):
    """GET with the SSRF safeguards. Returns (status, lowercase-headers, body, final_url)."""
    for _ in range(hops + 1):
        sp = urlsplit(url)
        if sp.scheme != "https" or not sp.hostname:
            raise ValidationError("The link redirected to an address that is not https.")
        port = sp.port or 443
        ips = assert_public_host(sp.hostname, port)
        sock = socket.create_connection((ips[0], port), timeout=timeout)       # the IP we just validated
        conn = http.client.HTTPSConnection(sp.hostname, port, timeout=timeout)
        conn.sock = ssl.create_default_context().wrap_socket(sock, server_hostname=sp.hostname)
        target = (sp.path or "/") + (("?" + sp.query) if sp.query else "")
        try:
            conn.request("GET", target, headers={"User-Agent": _UA, "Accept": "*/*", **(headers or {})})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308) and resp.getheader("Location"):
                url = urljoin(url, resp.getheader("Location"))
                continue
            body = resp.read(max_bytes + 1)[:max_bytes]
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body, url
        finally:
            conn.close()
    raise ValidationError("The link redirects too many times.")


def probe(det: Detected, *, site_origin: str | None = None) -> dict:
    """Plain-language checks for the admin form. Never raises: problems become ``notes``."""
    out: dict = {"reachable": None, "playable": None, "live": None, "embeddable": None, "cors": None,
                 "notes": list(det.notes)}
    try:
        if det.kind == "hls":
            status, hdr, body, _ = _fetch(det.url, headers={"Origin": site_origin} if site_origin else None)
            out["reachable"] = status == 200
            text = body.decode("utf-8", "replace")
            out["playable"] = out["reachable"] and text.lstrip().startswith("#EXTM3U")
            if out["playable"]:
                if "#EXT-X-STREAM-INF" in text:
                    out["notes"].append("This is a master playlist: whether it is live or a replay is detected while it plays.")
                else:
                    out["live"] = "#EXT-X-ENDLIST" not in text
            elif out["reachable"]:
                out["notes"].append("The address answers, but it is not an HLS playlist.")
            else:
                out["notes"].append(f"The host answered with an error ({status}).")
            if site_origin and out["reachable"]:
                acao = (hdr.get("access-control-allow-origin") or "").strip()
                same = urlsplit(det.url).netloc.lower() == urlsplit(site_origin).netloc.lower()
                out["cors"] = same or acao in ("*", site_origin)
                if not out["cors"]:
                    out["notes"].append("The stream host does not allow this website to play it. Ask them to send the header "
                                        f"Access-Control-Allow-Origin: {site_origin}")
        elif det.kind == "file":
            status, hdr, _, _ = _fetch(det.url, headers={"Range": "bytes=0-1023"}, max_bytes=2048)
            out["reachable"] = status in (200, 206)
            ctype = (hdr.get("content-type") or "").split(";")[0].strip().lower()
            out["playable"] = out["reachable"] and (ctype.startswith("video/") or ctype == "application/octet-stream")
            if out["reachable"] and status != 206 and "bytes" not in (hdr.get("accept-ranges") or ""):
                out["notes"].append("The host does not support seeking (Range requests): the bar will not jump around.")
            if not out["reachable"]:
                out["notes"].append(f"The host answered with an error ({status}).")
            elif not out["playable"]:
                out["notes"].append(f"The file is served as '{ctype or 'unknown'}', not as a video.")
        elif det.kind == "youtube":
            status, _, _, _ = _fetch("https://www.youtube.com/oembed?format=json&url=" + quote(det.url, safe=""),
                                     max_bytes=20_000)
            out["reachable"] = True
            out["embeddable"] = status == 200
            if status in (401, 403):
                out["notes"].append("The owner has switched off embedding for this video.")
            elif status == 404:
                out["notes"].append("YouTube does not know this video (private, deleted or a typo).")
            elif status != 200:
                out["notes"].append(f"YouTube answered {status}; try again in a moment.")
            else:
                out["notes"].append("YouTube cannot tell us from outside whether the video is live right now.")
        else:
            out["notes"].append("This kind of link cannot be checked from the server. Use Preview after saving.")
    except ValidationError as exc:
        out["notes"].append(str(exc))
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        out["reachable"] = False
        out["notes"].append(f"Could not reach that host ({type(exc).__name__}).")
    return out


# --------------------------------------------------------------------------- managing sources

def list_sources(db: Session) -> list[StreamSource]:
    return list(db.scalars(select(StreamSource).order_by(StreamSource.is_active.desc(), StreamSource.id.desc())))


def _get(db: Session, source_id: int) -> StreamSource:
    row = db.get(StreamSource, int(source_id))
    if row is None:
        raise NotFound("That stream source no longer exists.")
    return row


def create_source(db: Session, actor: User, *, name: str, raw_url: str, default_mode: str = "auto",
                  loop: bool = False, start_at: int | None = None, settings: SettingsStore,
                  check_dns: bool = True) -> StreamSource:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise ValidationError("Give the source a name (up to 80 characters).")
    det = detect(raw_url, allowed_hosts=allowed_host_set(settings))
    if check_dns:
        sp = urlsplit(det.url)
        assert_public_host(sp.hostname or "", sp.port or 443)
    mode = default_mode if default_mode in ("auto", "live", "replay") else "auto"
    if det.kind == "file":
        mode = "replay"
    elif det.kind != "hls" and mode == "auto":
        mode = "live"
    row = StreamSource(name=name, kind=det.kind, url=det.url, embed_ref=det.embed_ref, default_mode=mode,
                       loop=bool(loop), start_at=start_at if start_at else det.start, enabled=True, is_active=False,
                       health="unknown", created_by_id=actor.id)
    db.add(row)
    db.flush()
    audit.record(db, actor, "stream.create", entity_type="stream_source", entity_id=row.id,
                 after={"name": row.name, "kind": row.kind, "url": row.url})
    invalidate()
    return row


def set_enabled(db: Session, actor: User, source_id: int, on: bool) -> StreamSource:
    row = _get(db, source_id)
    before = {"enabled": row.enabled, "is_active": row.is_active}
    row.enabled = bool(on)
    if not on:
        row.is_active = False                        # a disabled source can never be what viewers watch
    db.flush()
    audit.record(db, actor, "stream.enable" if on else "stream.disable", entity_type="stream_source",
                 entity_id=row.id, before=before, after={"enabled": row.enabled, "is_active": row.is_active})
    invalidate()
    return row


def take(db: Session, actor: User, source_id: int) -> StreamSource:
    """Make this the one source everybody watches (open viewers pick it up within a few seconds)."""
    row = _get(db, source_id)
    if not row.enabled:
        raise ValidationError("Turn the source on before taking it live.")
    prev = db.scalar(select(StreamSource.id).where(StreamSource.is_active.is_(True)))
    db.execute(update(StreamSource).values(is_active=False))
    row.is_active = True
    db.flush()
    ttlcache.invalidate_after_commit(db, "live")
    audit.record(db, actor, "stream.take", entity_type="stream_source", entity_id=row.id,
                 before={"active_source_id": prev}, after={"active_source_id": row.id, "name": row.name})
    invalidate()
    return row


def stop(db: Session, actor: User) -> None:
    prev = db.scalar(select(StreamSource.id).where(StreamSource.is_active.is_(True)))
    db.execute(update(StreamSource).values(is_active=False))
    db.flush()
    ttlcache.invalidate_after_commit(db, "live")      # a bulk UPDATE touches no ORM object, so the listener would not fire
    audit.record(db, actor, "stream.stop", entity_type="stream_source", entity_id=prev,
                 before={"active_source_id": prev}, after={"active_source_id": None})
    invalidate()


def delete(db: Session, actor: User, source_id: int) -> None:
    row = _get(db, source_id)
    audit.record(db, actor, "stream.delete", entity_type="stream_source", entity_id=row.id,
                 before={"name": row.name, "kind": row.kind, "url": row.url, "was_active": row.is_active})
    db.delete(row)
    db.flush()
    invalidate()


# --------------------------------------------------------------------------- assignments (what plays where)

SCOPES = ("global", "event", "fight")
MODES = ("live", "replay", "standby", "off")
MODE_LABEL = {"live": "Live", "replay": "Replay", "standby": "Standby (looping clip)", "off": "Off (message only)"}
_RANK = {"global": 1, "event": 2, "fight": 3}
MAX_CHAIN = 5


def _manila() -> dt.tzinfo:
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("Asia/Manila")
    except Exception:  # noqa: BLE001 - a host without tzdata must not break scheduling (the Philippines has no DST)
        return dt.timezone(dt.timedelta(hours=8), "PHT")


def parse_local(value: str | None) -> dt.datetime | None:
    """A ``datetime-local`` form value (Philippine time) -> aware UTC. Empty = no limit."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        naive = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("That date and time is not valid.") from exc
    return naive.replace(tzinfo=_manila()).astimezone(dt.timezone.utc) if naive.tzinfo is None else naive.astimezone(dt.timezone.utc)


def fmt_local(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(_manila()).strftime("%b %d, %I:%M %p")


def chain_ids(a) -> list[int]:
    return [int(x) for x in (a.chain or "").split(",") if x.strip().isdigit()]


def _aware(value: dt.datetime | None) -> dt.datetime | None:
    return value.replace(tzinfo=dt.timezone.utc) if value is not None and value.tzinfo is None else value


def in_window(a, now: dt.datetime) -> bool:
    start, end = _aware(a.starts_at), _aware(a.ends_at)
    return (start is None or start <= now) and (end is None or now < end)


def pick_assignment(rows, event_id: int | None, fight_id: int | None, now: dt.datetime):
    """The rules, in one pure function: active + in window + matching scope; most specific scope wins, then a
    windowed row over a standing one, then the newest."""
    best, best_key = None, None
    for a in rows:
        if not a.active:
            continue
        if a.scope_type == "fight" and (fight_id is None or a.scope_id != fight_id):
            continue
        if a.scope_type == "event" and (event_id is None or a.scope_id != event_id):
            continue
        if not in_window(a, now):
            continue
        key = (_RANK[a.scope_type], 1 if (a.starts_at or a.ends_at) else 0, a.id or 0)
        if best_key is None or key > best_key:
            best, best_key = a, key
    return best


def list_assignments(db: Session) -> list[StreamAssignment]:
    rows = list(db.scalars(select(StreamAssignment)))
    rows.sort(key=lambda a: (-_RANK[a.scope_type], -(a.id or 0)))
    return rows


def create_assignment(db: Session, actor: User, *, scope_type: str, scope_id: int | None, mode: str,
                      chain: list[int], starts_at: dt.datetime | None = None, ends_at: dt.datetime | None = None,
                      pin_first: bool = False, slate_message: str = "") -> StreamAssignment:
    if scope_type not in SCOPES:
        raise ValidationError("Choose what this applies to.")
    if mode not in MODES:
        raise ValidationError("Choose how it plays.")
    if scope_type == "event" and db.get(Event, int(scope_id or 0)) is None:
        raise ValidationError("That event does not exist.")
    if scope_type == "fight" and db.get(Fight, int(scope_id or 0)) is None:
        raise ValidationError("That game does not exist.")
    if scope_type == "global":
        scope_id = None
    ids: list[int] = []
    for sid in chain:
        if int(sid) not in ids:
            ids.append(int(sid))
    if len(ids) > MAX_CHAIN:
        raise ValidationError(f"Use at most {MAX_CHAIN} sources in a chain.")
    if mode != "off":
        if not ids:
            raise ValidationError("Pick at least one source (the Primary).")
        found = {s.id: s for s in db.scalars(select(StreamSource).where(StreamSource.id.in_(ids)))}
        for sid in ids:
            src = found.get(sid)
            if src is None:
                raise ValidationError("One of the chosen sources no longer exists.")
            if not src.enabled:
                raise ValidationError(f"'{src.name}' is switched off. Turn it on first.")
    else:
        ids = []
    if starts_at and ends_at and ends_at <= starts_at:
        raise ValidationError("The end time must be after the start time.")
    slate = (slate_message or "").strip()[:500] or None
    previous = None
    if not (starts_at or ends_at):                   # one standing setting per scope: this one replaces it
        for old in db.scalars(select(StreamAssignment).where(
                StreamAssignment.scope_type == scope_type,
                StreamAssignment.scope_id.is_(None) if scope_id is None else StreamAssignment.scope_id == scope_id)):
            if not (old.starts_at or old.ends_at):
                previous = _snapshot(old)
                audit.record(db, actor, "stream.assign.replace", entity_type="stream_assignment", entity_id=old.id,
                             before=previous)
                db.delete(old)
        db.flush()
    row = StreamAssignment(scope_type=scope_type, scope_id=scope_id, mode=mode, chain=",".join(map(str, ids)),
                           starts_at=starts_at, ends_at=ends_at, pinned_source_id=ids[0] if (pin_first and ids) else None,
                           active=True, slate_message=slate, previous=previous, updated_by_id=actor.id)
    db.add(row)
    db.flush()
    audit.record(db, actor, "stream.assign", entity_type="stream_assignment", entity_id=row.id,
                 after={"scope": scope_type, "scope_id": scope_id, "mode": mode, "chain": row.chain,
                        "starts_at": starts_at, "ends_at": ends_at, "pinned": bool(row.pinned_source_id)})
    ttlcache.invalidate_after_commit(db, "live")
    return row


def _snapshot(a: StreamAssignment) -> dict:
    """Everything needed to put a standing setting back exactly as it was."""
    return {"scope_type": a.scope_type, "scope_id": a.scope_id, "mode": a.mode, "chain": a.chain,
            "pinned_source_id": a.pinned_source_id, "active": bool(a.active), "slate_message": a.slate_message}


def undo_assignment(db: Session, actor: User, assignment_id: int) -> str:
    """Take back a setting: remove it and re-apply the standing one it replaced (if any). Returns a plain sentence."""
    row = db.get(StreamAssignment, int(assignment_id))
    if row is None:
        raise NotFound("That setting no longer exists.")
    prev = row.previous or None
    audit.record(db, actor, "stream.assign.undo", entity_type="stream_assignment", entity_id=row.id,
                 before=_snapshot(row), after=prev)
    db.delete(row)
    db.flush()
    ttlcache.invalidate_after_commit(db, "live")
    if not prev:
        return "Setting removed. Viewers it applied to fall back to the next one."
    ids = [i for i in (prev.get("chain") or "").split(",") if i.strip().isdigit()]
    alive = {x for x in db.scalars(select(StreamSource.id).where(StreamSource.id.in_([int(i) for i in ids])))} if ids else set()
    kept = [int(i) for i in ids if int(i) in alive]
    pinned = prev.get("pinned_source_id")
    db.add(StreamAssignment(scope_type=prev["scope_type"], scope_id=prev.get("scope_id"), mode=prev["mode"],
                            chain=",".join(map(str, kept)), pinned_source_id=pinned if pinned in kept else None,
                            active=bool(prev.get("active", True)), slate_message=prev.get("slate_message"),
                            updated_by_id=actor.id))
    db.flush()
    return "Undone. The previous setting is back in force."


def scope_label(db: Session, scope_type: str, scope_id: int | None) -> str:
    if scope_type == "global":
        return "everyone"
    if scope_type == "event":
        e = db.get(Event, int(scope_id or 0))
        return f"everyone on {e.title}" if e else "an event that no longer exists"
    f = db.get(Fight, int(scope_id or 0))
    if f is None:
        return "a game that no longer exists"
    e = db.get(Event, f.event_id)
    return f"everyone watching fight #{f.number} of {e.title if e else 'its event'}"


def impact(db: Session, *, scope_type: str, scope_id: int | None, mode: str, chain: list[int],
           starts_at: dt.datetime | None = None, ends_at: dt.datetime | None = None) -> dict:
    """The plain-language warning shown before a setting is saved: who it changes, what it replaces, what happens
    right now (a setting with a future window changes nothing yet)."""
    if scope_type not in SCOPES or mode not in MODES:
        raise ValidationError("Choose what this applies to and how it plays.")
    who = scope_label(db, scope_type, scope_id)
    names = {s.id: s for s in db.scalars(select(StreamSource).where(StreamSource.id.in_(chain)))} if chain else {}
    first = next((names[i] for i in chain if i in names), None)
    now = dt.datetime.now(dt.timezone.utc)
    lines = []
    future = bool(starts_at and _aware(starts_at) > now)
    if mode == "off":
        lines.append(f"This changes what {who} see: a message instead of video.")
    else:
        lines.append(f"This changes what {who} see: {MODE_LABEL[mode]} from '{first.name if first else '?'}'"
                     f"{'' if len(chain) < 2 else f' (with {len(chain) - 1} backup' + ('s' if len(chain) > 2 else '') + ')'}.")
    if future:
        lines.append(f"It starts {fmt_local(starts_at)} (Philippine time). Nothing changes until then.")
    elif ends_at:
        lines.append(f"It stops applying {fmt_local(ends_at)}.")
    if not (starts_at or ends_at):
        old = db.scalar(select(StreamAssignment).where(
            StreamAssignment.scope_type == scope_type,
            StreamAssignment.scope_id.is_(None) if scope_id is None else StreamAssignment.scope_id == scope_id,
            StreamAssignment.starts_at.is_(None), StreamAssignment.ends_at.is_(None)))
        if old is not None:
            lines.append(f"It replaces the current standing setting ({MODE_LABEL[old.mode]}). You can undo this afterwards.")
    if first is not None and first.health == "down" and mode != "off":
        lines.append(f"Heads up: the primary '{first.name}' is marked Down ({first.health_detail or 'check failed'}).")
    return {"text": " ".join(lines), "lines": lines}


def set_assignment_active(db: Session, actor: User, assignment_id: int, on: bool) -> StreamAssignment:
    row = db.get(StreamAssignment, int(assignment_id))
    if row is None:
        raise NotFound("That setting no longer exists.")
    before = row.active
    row.active = bool(on)
    row.updated_by_id = actor.id
    db.flush()
    audit.record(db, actor, "stream.assign.enable" if on else "stream.assign.pause", entity_type="stream_assignment",
                 entity_id=row.id, before={"active": before}, after={"active": row.active})
    return row


def delete_assignment(db: Session, actor: User, assignment_id: int) -> None:
    row = db.get(StreamAssignment, int(assignment_id))
    if row is None:
        raise NotFound("That setting no longer exists.")
    audit.record(db, actor, "stream.assign.delete", entity_type="stream_assignment", entity_id=row.id,
                 before={"scope": row.scope_type, "scope_id": row.scope_id, "mode": row.mode, "chain": row.chain})
    db.delete(row)
    db.flush()


# --------------------------------------------------------------------------- what viewers get

def _first_usable(db: Session, a: StreamAssignment, exclude: frozenset[int] = frozenset()):
    """The first source in the chain that is on and not Down (a pinned one skips the health check). ``exclude`` is
    what one viewer's player says it could not play: it moves that viewer to the next source, but if nothing else is
    left the viewer keeps the normal answer (so their player keeps retrying instead of showing a dead end)."""
    ids = chain_ids(a)
    found = {s.id: s for s in db.scalars(select(StreamSource).where(StreamSource.id.in_(ids)))} if ids else {}
    fallback = (None, None)
    for pos, sid in enumerate(ids):
        src = found.get(sid)
        if src is None or not src.enabled:
            continue
        if src.health == "down" and a.pinned_source_id != sid:
            continue
        if sid in exclude:
            if fallback[0] is None:
                fallback = (src, pos)
            continue
        return src, pos
    return fallback


def payload(db: Session, *, event_id: int | None = None, fight_id: int | None = None,
            now: dt.datetime | None = None, exclude: frozenset[int] = frozenset()) -> dict:
    """What to play for this event / game right now. ``status``: sample (nothing configured: the sample feed),
    playing, slate (everything in the chain is unusable) or off (the admin chose message-only)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    st = SettingsStore(db)
    dvr = int(st.get("stream.dvr_window_seconds") or 120)
    out = {"status": "sample", "scope": "default", "kind": None, "mode": None, "src": None, "embed": None, "loop": False,
           "start": None, "message": None, "chain_position": None, "name": None, "source_id": None, "dvr_window": dvr,
           "autoplay_muted": bool(st.get("stream.autoplay_muted"))}
    if not st.get("stream.enabled"):
        return out

    def fill(src: StreamSource, mode: str, pos: int, scope: str) -> dict:
        if mode == "live" and src.kind == "hls" and src.default_mode == "auto":
            mode = "auto"                            # an HLS stream that ends turns into a replay by itself
        out.update(status="playing", scope=scope, kind=src.kind, mode=mode, name=src.name, chain_position=pos, source_id=src.id,
                   loop=bool(src.loop) or mode == "standby", start=src.start_at)
        out["src" if src.kind in NATIVE else "embed"] = src.url if src.kind in NATIVE else src.embed_ref
        return out

    a = pick_assignment(db.scalars(select(StreamAssignment).where(StreamAssignment.active.is_(True))), event_id, fight_id, now)
    if a is not None:
        slate = (a.slate_message or "").strip() or str(st.get("stream.default_slate_text") or "")
        if a.mode == "off":
            out.update(status="off", scope=a.scope_type, mode="off", message=slate)
            return out
        src, pos = _first_usable(db, a, exclude)
        if src is None:
            out.update(status="slate", scope=a.scope_type, mode=a.mode, message=slate)
            return out
        return fill(src, a.mode, pos, a.scope_type)
    legacy = db.scalar(select(StreamSource).where(StreamSource.enabled.is_(True), StreamSource.is_active.is_(True)).limit(1))
    if legacy is not None:                           # "Take live" from the sources list: the standing default for everything
        return fill(legacy, legacy.default_mode, 0, "default")
    return out


# --------------------------------------------------------------------------- health checks (Phase 3)

STALL_SECONDS = 45          # an HLS playlist whose media sequence has not moved for this long is stalled
DOWN_AFTER = 2              # consecutive failed checks before a source is marked Down (one blip is not an outage)
_SEQ = re.compile(r"#EXT-X-MEDIA-SEQUENCE:\s*(\d+)")


def parse_playlist(text: str) -> dict:
    """What matters in an HLS playlist: is it one at all, is it a master, has it ended, its media sequence."""
    text = text or ""
    ok = text.lstrip().startswith("#EXTM3U")
    master = ok and "#EXT-X-STREAM-INF" in text
    variant = None
    if master:
        lines = text.splitlines()
        for i, ln in enumerate(lines):
            if ln.startswith("#EXT-X-STREAM-INF"):
                for nxt in lines[i + 1:]:
                    if nxt.strip() and not nxt.startswith("#"):
                        variant = nxt.strip()
                        break
                break
    m = _SEQ.search(text)
    return {"ok": ok, "master": master, "variant": variant, "ended": "#EXT-X-ENDLIST" in text,
            "seq": int(m.group(1)) if m else None}


def judge(prev_health: str | None, fails: int, ok: bool | None, *, down_after: int = DOWN_AFTER) -> tuple[str, int]:
    """Pure rule: ok=True -> Up at once and the failure count resets; ok=False -> Down only on the Nth failure in a
    row; ok=None (cannot tell: a 5xx from a third party, an embed) -> nothing changes."""
    if ok is None:
        return prev_health or "unknown", fails
    if ok:
        return "up", 0
    fails += 1
    return ("down" if fails >= down_after else (prev_health or "unknown")), fails


def check_source(src: StreamSource, now: dt.datetime) -> tuple[bool | None, str, int | None]:
    """Look at one source from the server. Returns (ok, plain detail, media sequence). Never raises."""
    try:
        if src.kind == "hls":
            status, _, body, final = _fetch(src.url)
            if status != 200:
                return False, f"The host answered {status}.", None
            info = parse_playlist(body.decode("utf-8", "replace"))
            if not info["ok"]:
                return False, "The address answers, but it is not an HLS playlist.", None
            if info["master"]:                              # follow the first rendition: that is the one that moves
                if not info["variant"]:
                    return False, "The master playlist lists no stream.", None
                status, _, body, _ = _fetch(urljoin(final, info["variant"]))
                if status != 200:
                    return False, f"The stream playlist answered {status}.", None
                info = parse_playlist(body.decode("utf-8", "replace"))
                if not info["ok"] or info["master"]:
                    return False, "The stream playlist is not valid.", None
            if info["ended"]:
                return True, "Finished video (playlist has ended).", info["seq"]
            seq = info["seq"]
            if seq is not None and src.media_seq is not None and seq == src.media_seq and src.seq_changed_at is not None:
                idle = (now - _aware(src.seq_changed_at)).total_seconds()
                if idle > STALL_SECONDS:
                    return False, f"The stream stopped moving {int(idle)} s ago.", seq
            return True, "Live and moving.", seq
        if src.kind == "file":
            status, hdr, _, _ = _fetch(src.url, headers={"Range": "bytes=0-1023"}, max_bytes=2048)
            ctype = (hdr.get("content-type") or "").split(";")[0].strip().lower()
            if status not in (200, 206):
                return False, f"The host answered {status}.", None
            if not (ctype.startswith("video/") or ctype == "application/octet-stream"):
                return False, f"Served as '{ctype or 'unknown'}', not a video.", None
            return True, "File is reachable.", None
        if src.kind == "youtube":
            status, _, _, _ = _fetch("https://www.youtube.com/oembed?format=json&url=" + quote(src.url, safe=""), max_bytes=20_000)
            if status == 200:
                return True, "YouTube knows this video and allows embedding.", None
            if status in (401, 403, 404):
                return False, {404: "YouTube no longer finds this video.", 401: "Embedding is switched off.",
                               403: "Embedding is switched off."}[status], None
            return None, f"YouTube answered {status}; not counted.", None
        return None, "This kind of link cannot be checked from the server.", None
    except ValidationError as exc:
        return False, str(exc), None
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        return False, f"Could not reach the host ({type(exc).__name__}).", None


def apply_check(db: Session, src: StreamSource, ok: bool | None, detail: str, seq: int | None,
                now: dt.datetime) -> str | None:
    """Store one result. Returns "down" / "up" when the health CHANGED (that also clears the viewers' cache and writes
    the audit entry, so a failover is always explained)."""
    before = src.health or "unknown"
    if seq is not None and seq != src.media_seq:
        src.media_seq, src.seq_changed_at = seq, now
    elif seq is not None and src.seq_changed_at is None:
        src.seq_changed_at = now
    after, fails = judge(before, int(src.fail_count or 0), ok)
    src.fail_count = fails
    src.health = after
    src.health_detail = (detail or "")[:255]
    src.last_checked_at = now
    db.flush()
    if after == before:
        return None
    ttlcache.invalidate_after_commit(db, "live")
    audit.record(db, None, "stream.health", entity_type="stream_source", entity_id=src.id,
                 before={"health": before}, after={"health": after, "name": src.name, "why": src.health_detail})
    return "down" if after == "down" else ("up" if before == "down" else None)


def in_use_ids(db: Session, now: dt.datetime | None = None) -> set[int]:
    """Enabled sources worth watching: the "Take live" one and every source in an active setting's chain."""
    ids = set(db.scalars(select(StreamSource.id).where(StreamSource.enabled.is_(True), StreamSource.is_active.is_(True))))
    for a in db.scalars(select(StreamAssignment).where(StreamAssignment.active.is_(True))):
        if a.mode != "off":
            ids.update(chain_ids(a))
    on = set(db.scalars(select(StreamSource.id).where(StreamSource.enabled.is_(True), StreamSource.id.in_(ids)))) if ids else set()
    return on


def check_all(db: Session, *, only: set[int] | None = None, checker=check_source) -> list[dict]:
    """The timer's job. Checks every source in use (or ``only``), stores the result, returns what changed."""
    now = dt.datetime.now(dt.timezone.utc)
    ids = only if only is not None else in_use_ids(db, now)
    changes = []
    for src in db.scalars(select(StreamSource).where(StreamSource.id.in_(ids))) if ids else []:
        ok, detail, seq = checker(src, now)
        moved = apply_check(db, src, ok, detail, seq, now)
        if moved:
            changes.append({"id": src.id, "name": src.name, "now": src.health, "why": src.health_detail})
    return changes


def check_now(db: Session, actor: User, source_id: int) -> StreamSource:
    """The admin's "Check now" button."""
    src = _get(db, source_id)
    ok, detail, seq = check_source(src, dt.datetime.now(dt.timezone.utc))
    apply_check(db, src, ok, detail, seq, dt.datetime.now(dt.timezone.utc))
    audit.record(db, actor, "stream.check", entity_type="stream_source", entity_id=src.id,
                 after={"health": src.health, "why": src.health_detail})
    return src


def failovers(db: Session, now: dt.datetime | None = None) -> list[dict]:
    """For the admin page: every setting in force whose primary is not what viewers get, and why."""
    now = now or dt.datetime.now(dt.timezone.utc)
    out = []
    for a in db.scalars(select(StreamAssignment).where(StreamAssignment.active.is_(True))):
        if a.mode == "off" or not in_window(a, now):
            continue
        ids = chain_ids(a)
        if not ids:
            continue
        found = {s.id: s for s in db.scalars(select(StreamSource).where(StreamSource.id.in_(ids)))}
        src, pos = _first_usable(db, a)
        if pos == 0 and src is not None:
            continue
        primary = found.get(ids[0])
        why = ("it is switched off" if primary is not None and not primary.enabled else
               (primary.health_detail or "its check failed") if primary is not None else "it was removed")
        out.append({"assignment_id": a.id, "scope": scope_label(db, a.scope_type, a.scope_id),
                    "primary": primary.name if primary else "(removed)", "why": why,
                    "playing": src.name if src is not None else None})
    return out


# --------------------------------------------------------------------------- Content-Security-Policy

_csp_lock = threading.Lock()
_csp: dict = {"at": 0.0, "enabled": {"media": [], "frame": []}, "all": {"media": [], "frame": []}}
CSP_TTL = 10.0


def invalidate() -> None:
    with _csp_lock:
        _csp["at"] = 0.0


def snapshot_stale() -> bool:
    return time.monotonic() - _csp["at"] > CSP_TTL


def _origin(url: str | None) -> str | None:
    if not url:
        return None
    sp = urlsplit(url)
    return f"https://{sp.netloc.lower()}" if sp.scheme == "https" and sp.netloc and "@" not in sp.netloc else None


def refresh() -> None:
    enabled = {"media": set(), "frame": set()}
    every = {"media": set(), "frame": set()}
    try:
        with session_scope() as db:
            rows = db.execute(select(StreamSource.kind, StreamSource.url, StreamSource.embed_ref,
                                     StreamSource.enabled)).all()
        for kind, url, embed, on in rows:
            bucket, origin = ("media", _origin(url)) if kind in NATIVE else ("frame", _origin(embed))
            if origin:
                every[bucket].add(origin)
                if on:
                    enabled[bucket].add(origin)
    except Exception:                                  # the table may not exist yet: base policy only
        pass
    with _csp_lock:
        _csp["enabled"] = {k: sorted(v) for k, v in enabled.items()}
        _csp["all"] = {k: sorted(v) for k, v in every.items()}
        _csp["at"] = time.monotonic()


def csp_extras(path: str) -> dict:
    """Hosts the policy may add for this request. The Streams admin page sees every saved source (so Preview works
    before a source is switched on); every other page only sees enabled ones."""
    return _csp["all" if path.startswith("/staff/stream") else "enabled"]


def extend_csp(csp: str, extras: dict) -> str:
    """Add exactly the listed https origins to a base policy. Never a wildcard, never http:."""
    media, frame = extras.get("media") or [], extras.get("frame") or []
    marker = "object-src 'none'; "
    if media:
        hosts = " ".join(media)
        csp = csp.replace("connect-src 'self'; ", f"connect-src 'self' {hosts}; ")
        csp = csp.replace(marker, f"media-src 'self' blob: {hosts}; worker-src 'self' blob:; {marker}")
    if frame:
        csp = csp.replace(marker, f"frame-src {' '.join(frame)}; {marker}")
    return csp

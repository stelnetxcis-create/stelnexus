"""Template helpers for the built static assets (see tools/build_static.mjs).

    {{ css('site') }}   ->  <link rel="stylesheet" href="/static/dist/site.3fa9c1d2.css">
    {{ js('site') }}    ->  <script src="/static/dist/site.9b21e0aa.js" defer></script>

Bundles are defined once in app/asset_bundles.json. `npm run build` turns them into content-hashed
files plus app/build/manifest.json. If there is no usable build the helpers fall back to the plain
source files with a `?v=<mtime>` cache-buster, so the app always works unbuilt.

In dev a build that no longer matches the sources (you edited a .css/.js and did not rebuild) is
ignored in favour of the sources, so an edit is never silently hidden behind old output. In prod the
build is trusted as-is, and a stale one is logged once as a warning.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

from markupsafe import Markup

from app.core.config import get_settings

log = logging.getLogger("stelnexus.assets")

APP_DIR = Path(__file__).resolve().parents[1]
STATIC_DIR = APP_DIR / "static"
BUNDLES_FILE = APP_DIR / "asset_bundles.json"
MANIFEST_FILE = APP_DIR / "build" / "manifest.json"
SW_BUILT_FILE = APP_DIR / "build" / "sw.js"
SW_SOURCE_FILE = STATIC_DIR / "sw.js"

_RECHECK_SECONDS = 2.0
_state: dict = {"checked": 0.0, "mtime": None, "manifest": None, "fresh": False, "warned": False}


def _bundles() -> dict:
    return json.loads(BUNDLES_FILE.read_text("utf-8"))


def source_fingerprint() -> str:
    """Must stay byte-for-byte identical to inputsHash() in tools/build_static.mjs."""
    bundles = _bundles()
    files = {"sw.js"}
    for kind in ("css", "js"):
        for listed in bundles[kind].values():
            files.update(listed)
    for folder in ("img", "icons", "fonts"):
        base = STATIC_DIR / folder
        if base.is_dir():
            files.update(p.relative_to(STATIC_DIR).as_posix() for p in base.rglob("*") if p.is_file())
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode())
        h.update(b"\0")
        h.update((STATIC_DIR / rel).read_bytes())
        h.update(b"\0")
    h.update(b"bundles\0")
    h.update(BUNDLES_FILE.read_bytes())
    return h.hexdigest()


def _load() -> tuple[dict | None, bool]:
    """(manifest or None, usable). Re-reads when the manifest file changes; re-checks freshness every 2 s."""
    now = time.monotonic()
    st = _state
    try:
        mtime = MANIFEST_FILE.stat().st_mtime
    except OSError:
        st.update(manifest=None, mtime=None, fresh=False, checked=now)
        return None, False
    if st["mtime"] != mtime:
        try:
            st.update(manifest=json.loads(MANIFEST_FILE.read_text("utf-8")), mtime=mtime, checked=0.0, warned=False)
        except (OSError, ValueError):
            log.exception("could not read %s", MANIFEST_FILE)
            st.update(manifest=None, mtime=mtime, fresh=False, checked=now)
            return None, False
    manifest = st["manifest"]
    if now - st["checked"] >= _RECHECK_SECONDS:
        try:
            st["fresh"] = manifest.get("inputs") == source_fingerprint()
        except OSError:
            st["fresh"] = False
        st["checked"] = now
        if not st["fresh"] and not st["warned"]:
            st["warned"] = True
            log.warning("static build is out of date with app/static sources — run `npm run build`%s",
                        "" if get_settings().ENV == "prod" else " (dev: serving the sources instead)")
    usable = st["fresh"] or get_settings().ENV == "prod"
    return manifest, usable


def _source_tags(kind: str, name: str, defer: bool) -> str:
    out = []
    for rel in _bundles()[kind][name]:
        try:
            version = int((STATIC_DIR / rel).stat().st_mtime)
        except OSError:
            version = 0
        href = f"/static/{rel}?v={version}"
        out.append(f'<link rel="stylesheet" href="{href}">' if kind == "css"
                   else f'<script src="{href}"{" defer" if defer else ""}></script>')
    return "\n".join(out)


def asset_urls(kind: str, name: str) -> list[str]:
    """The URLs a page loads for this bundle (one when built, one per source file when not)."""
    manifest, usable = _load()
    if manifest and usable and name in manifest.get(kind, {}):
        return [manifest[kind][name]]
    return [f"/static/{rel}" for rel in _bundles()[kind][name]]


def _tag(kind: str, name: str, defer: bool = True) -> Markup:
    manifest, usable = _load()
    if manifest and usable and name in manifest.get(kind, {}):
        href = manifest[kind][name]
        if kind == "css":
            return Markup(f'<link rel="stylesheet" href="{href}">')
        return Markup(f'<script src="{href}"{" defer" if defer else ""}></script>')
    return Markup(_source_tags(kind, name, defer))


def css(name: str) -> Markup:
    return _tag("css", name)


def js(name: str, defer: bool = True) -> Markup:
    return _tag("js", name, defer)


def preload_fonts() -> Markup:
    """<link rel=preload> for the fonts the first paint needs, so text does not flash in a fallback face.
    Same hashed URLs the CSS uses (a preload for a different URL would download the font twice).
    `crossorigin` is required for font preloads even on the same origin."""
    manifest, usable = _load()
    if manifest and usable:
        urls = manifest.get("preload", [])
    else:
        urls = [f"/static/{rel}" for rel in _bundles().get("preload", [])]
    return Markup("\n".join(f'<link rel="preload" href="{u}" as="font" type="font/woff2" crossorigin>' for u in urls))


def service_worker_file() -> Path:
    """The generated worker (hashed shell + build id) when a usable build exists, else the plain source."""
    manifest, usable = _load()
    return SW_BUILT_FILE if manifest and usable and SW_BUILT_FILE.is_file() else SW_SOURCE_FILE

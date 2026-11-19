"""Conditional responses (ETag / If-None-Match) and page fragments.

Two small tools shared by the polling endpoints and the partial page refresh:

* ``json_reply`` / ``not_modified`` -- hash the body, send it as an ``ETag``, and answer ``304`` with no
  body when the caller already holds that exact state. An idle poll costs a header, not a payload.
* ``extract_region`` -- pull one ``<!--region:NAME-->...<!--/region:NAME-->`` slice out of a rendered page
  (see ``ui.region`` in ``_macros.html``), so the browser can refresh one box instead of reloading.

Why the client sends ``If-None-Match`` itself: the poll requests use ``cache: "no-store"`` (the responses
hold balances and chat), so the browser keeps no copy and never revalidates on its own. When script sets the
header by hand the browser hands the 304 straight to script, which is exactly what we want.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from fastapi import Request, Response

# A reverse proxy that compresses (Caddy `encode`) may rewrite a strong ETag to `"abc-gzip"` on the way out,
# so the value we get back is not always the one we sent. Strip those suffixes before comparing.
_ENCODING_SUFFIX = re.compile(r"-(?:gzip|zstd|br|deflate)$")
_NO_STORE = {"Cache-Control": "no-store"}


def make_etag(body: bytes | str) -> str:
    """A strong validator for ``body``: quoted, short, stable across processes."""
    raw = body.encode() if isinstance(body, str) else body
    return '"' + hashlib.sha1(raw).hexdigest()[:20] + '"'


def _tags(header: str) -> list[str]:
    out = []
    for part in header.split(","):
        tag = part.strip()
        if tag.startswith("W/"):
            tag = tag[2:]
        tag = _ENCODING_SUFFIX.sub("", tag.strip('"')) if tag.startswith('"') else tag
        if tag:
            out.append(tag)
    return out


def etag_matches(request: Request, etag: str) -> bool:
    """RFC 9110 weak comparison of ``If-None-Match`` against ``etag`` (``*`` matches anything)."""
    header = request.headers.get("if-none-match")
    if not header:
        return False
    if header.strip() == "*":
        return True
    return etag.strip('"') in _tags(header)


def not_modified(etag: str, extra: dict[str, str] | None = None) -> Response:
    headers = {"ETag": etag, **_NO_STORE, **(extra or {})}
    return Response(status_code=304, headers=headers)


def json_reply(request: Request, payload: Any, *, headers: dict[str, str] | None = None) -> Response:
    """``payload`` as JSON with an ETag; ``304`` if the caller already has this exact body.

    Keys are sorted so the same state always hashes the same.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    etag = make_etag(body)
    if etag_matches(request, etag):
        return not_modified(etag, headers)
    return Response(body, media_type="application/json", headers={"ETag": etag, **_NO_STORE, **(headers or {})})


# ---------------------------------------------------------------------------------------------- fragments

_REGION_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def wants_fragment(request: Request) -> str | None:
    """The region name the caller asked for (``X-Fragment: name``), or None for a normal page."""
    name = (request.headers.get("x-fragment") or "").strip().lower()
    return name if _REGION_NAME.match(name) else None


def extract_region(html: str, name: str) -> str | None:
    """The HTML between the markers of region ``name``, or None when the page has no such region.

    Plain string search on purpose: the markers are comments the template macro writes, so there is no
    HTML parsing to get wrong. Regions do not nest under the same name.
    """
    start = f"<!--region:{name}-->"
    end = f"<!--/region:{name}-->"
    a = html.find(start)
    if a < 0:
        return None
    a += len(start)
    b = html.find(end, a)
    if b < 0:
        return None
    return html[a:b]


def fragment_response(request: Request, response: Response, name: str) -> Response:
    """Turn a rendered full-page ``response`` into just region ``name`` (with ETag / 304).

    Anything unexpected (redirects, errors, a page without that region) is passed through untouched, so
    the client can fall back to a normal navigation: a real fragment always carries an ``X-Fragment`` header.
    """
    if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
        return response
    html = bytes(response.body).decode("utf-8", "replace")
    part = extract_region(html, name)
    if part is None:
        return response
    etag = make_etag(part)
    marks = {"Vary": "X-Fragment", "X-Fragment": name}
    if etag_matches(request, etag):
        return not_modified(etag, marks)
    return Response(part, media_type="text/html", headers={"ETag": etag, **_NO_STORE, **marks})

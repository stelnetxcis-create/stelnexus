"""Static asset pipeline: hashed bundles, dev fallback, service worker, cache headers."""
import json
import re
import shutil
import subprocess

import pytest

from app.core import assets
from tests.test_web import login

BUILT = assets.MANIFEST_FILE.is_file()


def _manifest():
    return json.loads(assets.MANIFEST_FILE.read_text("utf-8"))


def test_every_bundle_source_exists():
    bundles = json.loads(assets.BUNDLES_FILE.read_text("utf-8"))
    for kind in ("css", "js"):
        for name, files in bundles[kind].items():
            for rel in files:
                assert (assets.STATIC_DIR / rel).is_file(), f"{kind}:{name} lists missing {rel}"
    for kind, name in bundles["shell"]:
        assert name in bundles[kind]


def test_unbuilt_fallback_emits_source_files_with_a_cache_buster(monkeypatch):
    monkeypatch.setattr(assets, "MANIFEST_FILE", assets.APP_DIR / "build" / "does-not-exist.json")
    assets._state.update(mtime=None, manifest=None, fresh=False, checked=0.0)
    try:
        tags = str(assets.css("site"))
        bundles = json.loads(assets.BUNDLES_FILE.read_text("utf-8"))
        assert tags.count("<link") == len(bundles["css"]["site"]) and re.search(r"/static/css/theme\.css\?v=\d+", tags)
        assert 'src="/static/js/nav-init.js?v=' in str(assets.js("nav-init", defer=False))
        assert " defer" not in str(assets.js("nav-init", defer=False)) and " defer" in str(assets.js("site"))
        assert assets.service_worker_file() == assets.SW_SOURCE_FILE
    finally:
        assets._state.update(mtime=None, manifest=None, fresh=False, checked=0.0)


@pytest.mark.skipif(not BUILT, reason="run `npm run build` first")
def test_build_is_current_with_the_sources():
    """Python and Node must agree on the input fingerprint, otherwise the dev stale-check is meaningless."""
    assert _manifest()["inputs"] == assets.source_fingerprint(), "sources changed: run `npm run build`"


@pytest.mark.skipif(not BUILT, reason="run `npm run build` first")
def test_built_pages_load_three_files_not_eleven():
    c, _ = login("admin")
    html = c.get("/dashboard").text
    m = _manifest()
    for url in (m["css"]["site"], m["js"]["site"], m["js"]["nav-init"]):
        assert url in html
    assert not re.search(r'(href|src)="/static/(css|js)/', html)        # no loose source files
    assert re.search(r"/site\.[0-9a-f]{8}\.css$", m["css"]["site"])


@pytest.mark.skipif(not BUILT, reason="run `npm run build` first")
def test_hashed_files_are_served_immutable_and_sw_is_not():
    c, _ = login("admin")
    m = _manifest()
    r = c.get(m["css"]["site"])
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"] and "max-age=31536000" in r.headers["cache-control"]
    assert "url(/static/dist/fonts/" in r.text or "url(/static/dist/img/" in r.text      # CSS points at hashed media too
    assert not re.search(r"url\(/static/(fonts|img)/", r.text)
    for url in re.findall(r"url\((/static/dist/[^)]+)\)", r.text)[:5]:
        assert c.get(url).status_code == 200, url
    sw = c.get("/sw.js")
    assert sw.status_code == 200 and "immutable" not in sw.headers["cache-control"]
    assert "max-age=31536000" not in sw.headers["cache-control"]


@pytest.mark.skipif(not BUILT, reason="run `npm run build` first")
def test_served_service_worker_carries_the_build_id_and_hashed_shell():
    c, _ = login("admin")
    m = _manifest()
    sw = c.get("/sw.js").text
    assert f"/*@build*/'{m['build']}'" in sw
    for url in m["shell"]:
        assert url in sw
    assert "/static/css/theme.css" not in sw            # nothing hand-listed that could go stale


@pytest.mark.skipif(not (BUILT and shutil.which("node") and (assets.APP_DIR.parent / "node_modules").is_dir()),
                    reason="needs node, `npm ci` and a build")
def test_node_check_agrees_the_build_is_current():
    root = assets.APP_DIR.parent
    done = subprocess.run(["node", "tools/build_static.mjs", "--check"], cwd=root, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(not BUILT, reason="run `npm run build` first")
def test_dev_ignores_a_stale_build_so_edits_are_never_hidden(monkeypatch):
    monkeypatch.setattr(assets, "source_fingerprint", lambda: "edited-since-build")
    assets._state.update(checked=0.0, fresh=False, warned=True)
    try:
        assert assets.get_settings().ENV == "dev"
        tags = str(assets.css("site"))
        assert "/static/dist/" not in tags and "/static/css/theme.css?v=" in tags
        assert assets.service_worker_file() == assets.SW_SOURCE_FILE
    finally:
        assets._state.update(checked=0.0, fresh=False, warned=False)


@pytest.mark.skipif(not BUILT, reason="run `npm run build` first")
def test_prod_trusts_the_build_even_when_sources_differ(monkeypatch):
    class Prod:
        ENV = "prod"
    monkeypatch.setattr(assets, "source_fingerprint", lambda: "edited-since-build")
    monkeypatch.setattr(assets, "get_settings", lambda: Prod())
    assets._state.update(checked=0.0, fresh=False, warned=True)
    try:
        assert "/static/dist/site." in str(assets.css("site"))
        assert assets.service_worker_file() == assets.SW_BUILT_FILE
    finally:
        assets._state.update(checked=0.0, fresh=False, warned=False)


@pytest.mark.skipif(not BUILT, reason="run `npm run build` first")
def test_fonts_are_preloaded_with_the_same_urls_the_css_uses():
    c, _ = login("admin")
    html = c.get("/dashboard").text
    m = _manifest()
    assert len(m["preload"]) == 2
    css = c.get(m["css"]["site"]).text
    for url in m["preload"]:
        assert f'<link rel="preload" href="{url}" as="font" type="font/woff2" crossorigin>' in html
        assert url in css                       # else the browser would download the font twice
        assert c.get(url).status_code == 200
    assert "manrope" in "".join(m["preload"]) and "cormorant" in "".join(m["preload"])


def test_login_page_has_no_font_preload_and_gets_the_login_extras():
    from fastapi.testclient import TestClient
    from app.main import app
    html = TestClient(app).get("/login").text
    assert "rel=\"preload\"" not in html                 # auth.css declares no web fonts
    bundles = json.loads(assets.BUNDLES_FILE.read_text("utf-8"))["js"]
    assert "js/login-extras.js" in bundles["auth"] and "js/login-extras.js" not in bundles["site"]
    assert "js/anim-pause.js" in bundles["site"]
    js = (assets.STATIC_DIR / "js" / "luxe.js").read_text("utf-8")
    assert "LUXE login extras" not in js


def test_offscreen_pause_rule_exists_and_reduced_motion_stays_covered():
    css = (assets.STATIC_DIR / "css" / "theme.css").read_text("utf-8")
    assert ".is-offscreen" in css and "animation-play-state:paused" in css
    luxe = (assets.STATIC_DIR / "css" / "base.css").read_text("utf-8")
    assert "prefers-reduced-motion:reduce" in luxe and "*::before,*::after{animation:none!important" in luxe

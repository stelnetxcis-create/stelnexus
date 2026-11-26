"""One player, one set of controls, one full screen: YouTube is driven by our controls, other embeds run passively."""
import pathlib
import shutil
import subprocess

import pytest

from app.services import streams

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8")


def test_player_markup_has_volume_and_sound_chip():
    html = _read("app/web/templates/_player.html")
    assert "data-vp-vol" in html and "data-vp-sound" in html
    assert "data-vp-big" in html and "data-vp-mute" in html and "data-vp-full" in html


def test_youtube_is_driven_by_our_controls_and_never_gets_its_own_full_screen():
    js = _read("app/static/js/stream.js")
    assert "function ytShim" in js and "enablejsapi" in js
    assert "q.set('fs', '0')" in js and "'controls', ownControls ? '1' : '0'" in js      # YouTube's UI and full screen button off
    assert "f.allowFullscreen = false" in js
    assert "f.allow = 'autoplay'" in js                                                   # no 'fullscreen', no pop-out (picture-in-picture) for any embed
    assert "picture-in-picture" not in js
    assert "allowFullscreen = true" not in js
    assert "mountPassive" in js and "embedFail" in js                                       # fallback + failover paths exist


def test_css_makes_the_frame_click_proof_and_hides_dead_controls_on_passive_embeds():
    css = _read("app/static/css/store.css")
    assert ".vp.is-yt .vp-media iframe{pointer-events:none}" in css
    assert ".vp.is-embed .pbs.is-min ~ .vp-big" in css          # the minimized payout card used to bring the dead play button back
    assert ".vp-sound{" in css and ".vp-vol{" in css


def test_admin_notes_describe_the_new_behaviour():
    yt = streams.detect("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert any("controlled by our player" in n for n in yt.notes)
    fb = streams.detect("https://www.facebook.com/somepage/videos/1234567890/")
    assert any("cannot be controlled" in n for n in fb.notes)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_youtube_controller_logic():
    """Queued commands, origin checks, state reading, clamped seeks, and no seek when there is no rewind window."""
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "ytshim_check.js")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout + out.stderr


def test_phone_fixes_are_wired():
    js = _read("app/static/js/stream.js")
    css = _read("app/static/css/store.css")
    # the browser's own video overlay is asked to stay away, and the element is click-proof
    for attr in ("x5-playsinline", "webkit-playsinline", "disablePictureInPicture", "disableRemotePlayback", "nofullscreen"):
        assert attr in js, attr
    assert ".vp-media video{pointer-events:none}" in css
    # the control row can never push Full screen off a phone
    assert ".vp-row{flex-wrap:wrap" in css and ".vp.is-behind .vp-time{display:none}" in css
    # a YouTube that will not start from our button becomes tappable
    assert "function needTap" in js and "function armTap" in js
    assert ".vp.is-yt-tap .vp-media iframe{pointer-events:auto}" in css
    # the progress bar is the last 10 minutes
    assert "const BAR_SPAN = 600" in js and "Math.min(DVR(), BAR_SPAN)" in js


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_time_format_and_bar_span():
    out = subprocess.run(["node", str(ROOT / "tests" / "js" / "vp_mobile_check.js")], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr

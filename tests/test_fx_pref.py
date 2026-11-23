from tests.test_web import login


def test_settings_has_the_animation_picker():
    c, r = login("admin")
    assert r.status_code == 303
    page = c.get("/preferences")
    assert page.status_code == 200
    assert "data-fx-pref" in page.text and 'value="min"' in page.text and 'value="full"' in page.text


def test_settings_holds_sounds_and_profile_no_longer_has_animations():
    c, _ = login("admin")
    page = c.get("/preferences").text
    assert "data-sound-toggle" in page and 'id="sounds"' in page
    assert "data-fx-pref" not in c.get("/profile").text
    nav = c.get("/dashboard").text
    assert 'href="/preferences"' in nav and 'class="icon-btn snd-btn"' not in nav

from tests.test_web import login


def test_profile_has_the_animation_picker():
    c, r = login("admin")
    assert r.status_code == 303
    page = c.get("/profile")
    assert page.status_code == 200
    assert "data-fx-pref" in page.text and 'value="min"' in page.text and 'value="full"' in page.text

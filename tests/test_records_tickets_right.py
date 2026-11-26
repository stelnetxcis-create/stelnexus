"""Records asks every game for the 'tickets' right. A game that did not answer it crashed /records with a 500."""
from types import SimpleNamespace

import pytest

from app import games
from app.games import RIGHTS


@pytest.mark.parametrize("key", sorted(games.GAMES))
def test_every_game_answers_the_tickets_right(key):
    everything = SimpleNamespace(can=lambda perm: True)
    nothing = SimpleNamespace(can=lambda perm: False)
    adapter = games.GAMES[key]
    assert adapter.can(everything, "tickets") is True
    assert adapter.can(nothing, "tickets") is False


def test_tickets_is_not_one_of_the_six_and_nonsense_is_still_rejected():
    assert "tickets" not in RIGHTS
    with pytest.raises(ValueError):
        games.GAMES["sabong"].can(SimpleNamespace(can=lambda p: True), "nonsense")

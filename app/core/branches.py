"""The three branches of accounts, and the tier of each account inside its branch.

    Users   player -> sub agent -> agent -> master      the people who play and recruit. A higher tier has more
                                                        authority and earns commission; a player earns none.
    Staff   declarator, teller                          the people who run the desk.
    Dev     pawn -> bishop -> admin                     the people who run the platform.

Every account signs in the same way. The branch and tier decide what a person is called and (through
``app.core.rbac``) what they may see. This module is plain data: it reads no database and imports nothing
from the app, so it can be used anywhere.
"""
from __future__ import annotations

USERS, STAFF, DEV = "users", "staff", "dev"

# Lowest tier first. "player" is listed for the Users branch even where a player is still stored as a Player
# record: the label and tier are the same either way.
LADDERS: dict[str, tuple[str, ...]] = {
    USERS: ("player", "sub_agent", "agent", "master"),
    STAFF: ("declarator", "teller"),
    DEV: ("pawn", "bishop", "admin"),
}
BRANCH_LABELS = {USERS: "Users", STAFF: "Staff", DEV: "Dev"}
BRANCH_TONES = {USERS: "green", STAFF: "blue", DEV: "purple"}      # badge colours used by the People page
ROLE_LABELS = {
    "player": "Player", "sub_agent": "Sub Agent", "agent": "Agent", "master": "Master Agent",
    "declarator": "Declarator", "teller": "Teller", "pawn": "Pawn", "bishop": "Admin", "admin": "Admin",
}
_BRANCH_OF = {role: branch for branch, ladder in LADDERS.items() for role in ladder}


def branch_of(role: str) -> str:
    """'users', 'staff' or 'dev' for a role value such as 'agent'. Unknown roles read as 'staff' (the safe, narrow one)."""
    return _BRANCH_OF.get(str(getattr(role, "value", role)), STAFF)


def tier_of(role: str) -> int:
    """1 = lowest tier of its branch (a player, a declarator, a pawn)."""
    role = str(getattr(role, "value", role))
    ladder = LADDERS[branch_of(role)]
    return ladder.index(role) + 1 if role in ladder else 1


def describe(role: str) -> dict:
    """What the People page shows next to a person: branch, tier and the ladder position, e.g. Users · tier 3 of 4."""
    role = str(getattr(role, "value", role))
    branch = branch_of(role)
    return {"branch": branch, "branch_label": BRANCH_LABELS[branch], "tone": BRANCH_TONES[branch],
            "tier": tier_of(role), "of": len(LADDERS[branch]), "label": ROLE_LABELS.get(role, role.title())}


def commission_eligible(role: str) -> bool:
    """Only the Users branch above a player earns commission."""
    role = str(getattr(role, "value", role))
    return branch_of(role) == USERS and role != "player"

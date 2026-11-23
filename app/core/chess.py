"""The chess names: who sees what an account is called.

    King    the one true admin (stored as role ``admin``)          ♔
    Bishop  a deputy the King appoints (stored as role ``bishop``)  ♗
    Pawn    a helper with rights set per account                    ♙

Only the King ever sees the real names. Everyone else is kept from learning that a King exists: a Bishop reads as
plain "Admin" (with the crown) to itself and to Pawns, and the King account itself is never shown to them at all.
"""
from __future__ import annotations

from app.db.models import Role, User

KING, BISHOP, PAWN = "♔", "♗", "♙"
SYMBOL = {"admin": KING, "bishop": BISHOP, "pawn": PAWN}


def piece(role: Role | str, viewer: User | None) -> tuple[str, str]:
    """(label, symbol) for an account of ``role`` as ``viewer`` should see it. The symbol is '' for roles with none."""
    role = Role(role)
    if viewer is not None and viewer.role == Role.ADMIN:
        label = {"admin": "King", "bishop": "Bishop"}.get(role.value, role.label)
        return label, SYMBOL.get(role.value, "")
    if role in (Role.ADMIN, Role.BISHOP):
        return "Admin", KING                  # a Bishop believes it is the top; so does everyone who can see one
    return role.label, SYMBOL.get(role.value, "")


def role_name(role: Role | str, viewer: User | None) -> str:
    label, symbol = piece(role, viewer)
    return f"{symbol} {label}".strip()

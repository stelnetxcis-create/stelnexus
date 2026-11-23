"""How accounts are grouped in Admin Panel -> Access.

Three trees, each holding account *kinds*. A kind is either a built-in role (or ``player``) or a custom
account type the Admin made on top of one of those roles (see app.db.models.AccountType).

    Dev     the Admin (developer) and Pawns (view-only helpers the Admin appoints)
    Staff   Declarators and Tellers (back-office)
    Users   the selling network (Master Agent, Agent, Sub Agent) and Players
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tree:
    key: str
    label: str
    tagline: str
    kinds: tuple[str, ...]
    accent: str          # a CSS colour (theme variable or hex)
    tagline_other: str = ""   # what a Bishop reads instead (it must not learn that a King or Bishops exist)


TREES: tuple[Tree, ...] = (
    Tree("dev", "Dev", "The King, the Bishops it appoints, and the Pawns who help.", ("admin", "bishop", "pawn"), "var(--purple)",
         "The helper accounts you appoint (Pawns) and what each may do."),
    Tree("staff", "Staff", "Back-office accounts that run results and the wallet desk.", ("declarator", "teller"), "#7fbcff"),
    Tree("users", "Users", "The selling network and the people who play.", ("master", "agent", "sub_agent", "player"), "var(--green)"),
)
TREE_BY_KEY: dict[str, Tree] = {t.key: t for t in TREES}

KIND_LABEL: dict[str, str] = {
    "admin": "King", "bishop": "Bishop", "pawn": "Pawn", "declarator": "Declarator", "teller": "Teller",
    "master": "Master Agent", "agent": "Agent", "sub_agent": "Sub Agent", "player": "Player",
}
KIND_BLURB: dict[str, str] = {
    "admin": "The one true admin: hidden from everyone, holds every right, cannot be limited. Nobody below is told it exists.",
    "bishop": "A deputy admin you appoint. Holds every right except what you seal, can create Pawns and set their rights, and believes it is the top admin.",
    "pawn": "Helpers. Nothing is on by default; an admin switches on exactly what each one may see or change, and how.",
    "declarator": "Enters fight and draw results. Never sees stakes, pools or payouts.",
    "teller": "The wallet desk: approves load and cash-out requests. Holds no balance.",
    "master": "Top of the visible network. Runs operations and keeps the remainder of every commission pool.",
    "agent": "Recruits Sub Agents and players and earns the Volume % of the commission pool.",
    "sub_agent": "Recruits players and earns the SOP % taken from its Agent's cut.",
    "player": "Plays only: sees the games, places its own bets, loads and cashes out its own credit.",
}

# Built-in kinds that a custom account type may be based on (a type inherits the hierarchy, wallet and
# network rules of its base role; only the rights differ). The Admin and players cannot be a base.
TYPE_BASES: tuple[str, ...] = ("pawn", "declarator", "teller", "master", "agent", "sub_agent")


KIND_ICON: dict[str, str] = {"admin": "♔", "bishop": "♗", "pawn": "♙"}
# Kinds only the King may see or open. A Bishop gets "not found" for them, so it cannot tell they exist.
KING_ONLY_KINDS: tuple[str, ...] = ("admin", "bishop")


def kinds_visible_to(tree: Tree, viewer) -> tuple[str, ...]:
    if viewer is not None and viewer.role.value == "admin":
        return tree.kinds
    return tuple(k for k in tree.kinds if k not in KING_ONLY_KINDS)


def tree_of(kind_key: str) -> Tree:
    for tree in TREES:
        if kind_key in tree.kinds:
            return tree
    raise KeyError(kind_key)

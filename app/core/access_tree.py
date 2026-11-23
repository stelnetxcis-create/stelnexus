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


TREES: tuple[Tree, ...] = (
    Tree("dev", "Dev", "The developer account and the view-only helpers the Admin appoints.", ("admin", "pawn"), "var(--purple)"),
    Tree("staff", "Staff", "Back-office accounts that run results and the wallet desk.", ("declarator", "teller"), "#7fbcff"),
    Tree("users", "Users", "The selling network and the people who play.", ("master", "agent", "sub_agent", "player"), "var(--green)"),
)
TREE_BY_KEY: dict[str, Tree] = {t.key: t for t in TREES}

KIND_LABEL: dict[str, str] = {
    "admin": "Dev", "pawn": "Pawn", "declarator": "Declarator", "teller": "Teller",
    "master": "Master Agent", "agent": "Agent", "sub_agent": "Sub Agent", "player": "Player",
}
KIND_BLURB: dict[str, str] = {
    "admin": "The one hidden developer account. Always holds every right; this cannot be changed.",
    "pawn": "View-only helpers. Nothing is on by default; you switch on exactly what each one may see or change.",
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


def tree_of(kind_key: str) -> Tree:
    for tree in TREES:
        if kind_key in tree.kinds:
            return tree
    raise KeyError(kind_key)

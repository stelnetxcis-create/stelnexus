"""Every tunable business variable, editable by Admin in the dashboard.

No rate, cut, limit or threshold is hard-coded in business logic. Each entry is
typed, range-checked, audited on change, and cached per request.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ValidationError
from app.db.models import AppSetting

Kind = Literal["int", "money", "bps", "bool", "str", "choice"]


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    kind: Kind
    default: Any
    group: str
    help: str = ""
    minimum: int | None = None
    maximum: int | None = None
    choices: tuple[str, ...] = ()

    def parse(self, raw: str) -> Any:
        try:
            if self.kind == "bool":
                if str(raw).lower() not in ("true", "false", "1", "0", "on", "off", "yes", "no"):
                    raise ValueError
                return str(raw).lower() in ("true", "1", "on", "yes")
            if self.kind in ("int", "money", "bps"):
                val = int(str(raw).replace(",", "").strip())
                if self.minimum is not None and val < self.minimum:
                    raise ValueError(f"must be at least {self.minimum}")
                if self.maximum is not None and val > self.maximum:
                    raise ValueError(f"must be at most {self.maximum}")
                return val
            if self.kind == "choice":
                if raw not in self.choices:
                    raise ValueError(f"must be one of {', '.join(self.choices)}")
                return raw
            val = str(raw).strip()
            if self.maximum is not None and len(val) > self.maximum:
                raise ValueError("too long")
            return val
        except (ValueError, TypeError) as exc:
            raise ValidationError(f"{self.label}: {exc or 'invalid value'}") from exc

    def serialize(self, value: Any) -> str:
        if self.kind == "bool":
            return "true" if value else "false"
        return str(value)


DEFS: tuple[SettingDef, ...] = (
    # ---- payout -----------------------------------------------------------
    SettingDef("payout.model", "Default payout model", "choice", "pool", "Payout",
               "Pool = winners split the net pool pro-rata (pari-mutuel). Fixed = admin-set multiplier.",
               choices=("pool", "fixed")),
    SettingDef("payout.house_cut_bps", "Tong (house cut)", "bps", 500, "Payout",
               "Basis points of the total pool. 500 = 5.00%. Events may override.", 0, 10000),
    SettingDef("payout.no_winner_policy", "If nobody bet the winning side", "choice", "house", "Payout",
               "house = net pool goes to the house; refund = return every stake.",
               choices=("house", "refund")),
    SettingDef("payout.rounding_remainder", "Rounding remainder goes to", "choice", "house", "Payout",
               "Integer centavo division leaves 0-2 centavos; this account absorbs them.",
               choices=("house", "winners")),
    SettingDef("payout.min_stake_cents", "Minimum bet", "money", 2000, "Payout", "In centavos (2000 = ₱20).", 100, 10**9),
    SettingDef("payout.max_stake_cents", "Maximum bet", "money", 5000000, "Payout", "In centavos (5000000 = ₱50,000).", 100, 10**11),
    SettingDef("payout.max_payout_cents", "Maximum payout per ticket", "money", 0, "Payout", "0 = no cap.", 0, 10**11),
    SettingDef("payout.draw_odds_bps", "Draw payout multiplier", "bps", 80000, "Payout",
               "Draw is its own side bet, always paid at this fixed multiplier. 80000 = 8.00x.", 10000, 1000000),
    SettingDef("payout.min_odds_bps", "Minimum odds (pool model)", "bps", 14000, "Payout",
               "If the winning side would pay less than this multiple, the fight is cancelled and every "
               "stake refunded. 14000 = 1.40x. 0 = rule off.", 0, 100000),
    SettingDef("fight.draw_bets_enabled", "Allow Draw bets", "bool", True, "Events",
               "Off hides the Draw side everywhere."),
    SettingDef("fight.pawn_proof_required", "A Pawn must upload a picture to declare a result", "bool", True,
               "Events", "Admin and Master may attach one too, but it is optional for them. The picture "
               "is shown on the game and in Game History."),
    SettingDef("payout.default_fixed_odds_bps", "Default fixed odds", "bps", 19000, "Payout",
               "Used by fixed-odds events. 19000 = 1.90x.", 10000, 1000000),
    # ---- commissions ------------------------------------------------------
    # Waterfall: pool -> Cavo cut (Volume %) -> Kubrador cut (SOP %, taken out of the Cavo's cut).
    # Whatever the Cavo does not keep is the Master's ("Management"), so the lines always sum
    # to the pool exactly. Master and Admin can override Volume % / SOP % per Cavo / Kubrador
    # on the Cuts page; these are the defaults and the guard rails.
    SettingDef("commission.enabled", "Commissions enabled", "bool", True, "Commissions"),
    SettingDef("commission.basis", "Commission basis", "choice", "netwin", "Commissions",
               "netwin = pool is a share of the house's win on the game (tong etc.), split pro-rata by stake; "
               "turnover = pool is a share of every settled stake.",
               choices=("turnover", "netwin")),
    SettingDef("commission.pool_bps", "Pool: share of the basis that is split", "bps", 10000, "Commissions",
               "How much of the basis goes into the Master / Cavo / Kubrador split. 10000 = 100%. "
               "With basis = turnover use a small number (e.g. 150 = 1.50% of every stake).", 0, 10000),
    SettingDef("commission.cavo_cut_bps", "Default Cavo cut (Volume %)", "bps", 3000, "Commissions",
               "Share of the pool kept by the Cavo's side. 3000 = 30%, so the Master gets 70%.", 0, 10000),
    SettingDef("commission.kubrador_cut_bps", "Default Kubrador cut (SOP %)", "bps", 2000, "Commissions",
               "Share of the Cavo's cut that goes to the Kubrador who brought the bet. 2000 = 20% of the Cavo's 30%.",
               0, 10000),
    SettingDef("commission.max_cavo_cut_bps", "Highest Cavo cut a Master may set", "bps", 5000, "Commissions",
               "Guard rail. Admin is not limited. 5000 = 50%.", 0, 10000),
    SettingDef("commission.max_kubrador_cut_bps", "Highest Kubrador cut a Master may set", "bps", 5000, "Commissions",
               "Guard rail, as a share of the Cavo's cut. Admin is not limited.", 0, 10000),
    SettingDef("commission.cap_of_house_cut_bps", "Cap: share of house win", "bps", 10000, "Commissions",
               "Total commission per game can never exceed this share of the house win. 10000 = 100%.", 0, 10000),
    SettingDef("commission.pay_on_refund", "Pay commission on refunded bets", "bool", False, "Commissions"),
    # ---- bola: the two-number draw game (Tumbok-Sahod) ---------------------
    # Ships DARK. Only switch bola.enabled on once the operator has confirmed it is authorised
    # to run a numbers game (PCSO / PAGCOR / a licensed operator). Nothing here implies any
    # government affiliation: branding comes from brand.name / ticket.header.
    SettingDef("bola.enabled", "Number game (Bola) enabled", "bool", False, "Bola",
               "Master switch. Off hides the game everywhere and refuses every bet. Turn it on only "
               "after confirming you are licensed/authorised to run a numbers game."),
    SettingDef("bola.max_number", "Highest ball", "int", 40, "Bola",
               "Numbers run from 1 to this. The legacy game used 40.", 2, 90),
    SettingDef("bola.allow_duplicate_balls", "The two balls may be the same number", "bool", False, "Bola",
               "Off (default) = the two balls are distinct, so there are 40 x 39 = 1,560 ordered "
               "outcomes and a bet like 5,5 is refused. On changes the true odds."),
    SettingDef("bola.payout_multiplier_x100", "Payout multiplier", "int", 80000, "Bola",
               "80000 = 800.00x on both tumbok and sahod. At 40 distinct numbers the return to "
               "player is 800/1560 = 51.3%; disclose it to players.", 100, 500000),
    SettingDef("bola.min_line_stake_cents", "Minimum stake per line side", "money", 100, "Bola",
               "Each of tumbok / sahod is either 0 or at least this. 100 = PHP 1.", 1, 10**9),
    SettingDef("bola.limit_per_line_cents", "Limit per combination", "money", 150000, "Bola",
               "Sum of all stakes on the same ordered pair in one draw (the legacy rule, now actually "
               "enforced). 150000 = PHP 1,500. 0 = off.", 0, 10**11),
    SettingDef("bola.max_liability_per_outcome_cents", "Liability cap per outcome", "money", 0, "Bola",
               "Most one drawn outcome may owe: multiplier x (tumbok on (x,y) + sahod on (y,x)). "
               "A bet that would push an outcome past this is refused. 0 = off.", 0, 10**13),
    SettingDef("bola.max_liability_per_draw_cents", "Liability cap per draw", "money", 0, "Bola",
               "Same cap applied to the worst outcome of the whole draw. 0 = off.", 0, 10**13),
    SettingDef("bola.cutoff_minutes", "Betting cut-off (minutes before draw)", "int", 10, "Bola",
               "Bets are refused from this many minutes before the draw time. Enforced by timestamp "
               "inside the bet transaction, so it does not depend on the scheduler.", 0, 1440),
    SettingDef("bola.open_hours_before", "Open betting this long before the draw", "int", 12, "Bola",
               "Used by bola-tick / Generate draws to flip a scheduled draw to OPEN.", 0, 168),
    SettingDef("bola.cancel_window_seconds", "Bola ticket cancel window (seconds)", "int", 300, "Bola",
               "Agents may cancel within this window and only before the cut-off. Master/Admin any "
               "time before the cut-off.", 0, 86400),
    SettingDef("bola.commission.basis", "Bola commission basis", "choice", "turnover", "Bola",
               "turnover = a share of every settled stake (recommended for a fixed-multiplier game). "
               "netwin = a share of the house's net take on the draw, apportioned by stake.",
               choices=("turnover", "netwin")),
    SettingDef("bola.commission.pool_bps", "Bola pool: share of the basis", "bps", 3000, "Bola",
               "How much of each stake is set aside for the network. 3000 = 30%. The Cavo's Volume % "
               "and the Kubrador's SOP % are then taken out of this pool (Cuts page).", 0, 10000),
    SettingDef("bola.commission.pay_master", "Pay the Master a Bola commission line", "bool", False, "Bola",
               "Plan decision D4. Off (default): the Master's remainder of the pool stays with the "
               "house. On: it is paid to the Master as a commission line, as in sabong, so the "
               "whole pool is paid out."),
    SettingDef("bola.show_odds_to_players", "Show the multiplier and odds on the bet slip", "bool", True, "Bola",
               "Responsible-play disclosure: the payout multiplier and the chance per ordered pair."),
    # ---- wallet / transfers ----------------------------------------------
    SettingDef("wallet.min_transfer_cents", "Minimum credit transfer", "money", 1000, "Credit", "Centavos.", 100, 10**9),
    SettingDef("wallet.max_transfer_cents", "Maximum credit transfer", "money", 50000000, "Credit", "Centavos.", 100, 10**11),
    SettingDef("wallet.min_cash_cents", "Minimum online load / cash-out", "money", 1000, "Credit", "Centavos.", 100, 10**9),
    SettingDef("wallet.max_cash_cents", "Maximum online load / cash-out", "money", 50000000, "Credit", "Centavos.", 100, 10**11),
    SettingDef("wallet.withdrawals_enabled", "Withdrawals / cash-outs enabled", "bool", True, "Credit",
               "Emergency switch. Off blocks every cash-out, agent withdrawal and approval of one."),
    SettingDef("wallet.transfer_scope", "Who can receive a credit transfer", "choice", "anyone", "Credit",
               "anyone = any active account that holds credit (found by username). network = only your "
               "own upline and downline. Declarators and Pawns never hold credit.",
               choices=("anyone", "network")),
    SettingDef("wallet.allow_agent_overdraft", "Allow agent credit to go negative", "bool", False, "Credit",
               "Off is strongly recommended: credit must be pre-funded down the tree."),
    # ---- players / responsible gaming ------------------------------------
    SettingDef("player.min_age", "Minimum player age", "int", 21, "Players", "Enforced at registration.", 18, 99),
    SettingDef("player.daily_bet_limit_cents", "Default daily bet limit", "money", 0, "Players",
               "0 = unlimited. Per-player limits override this.", 0, 10**11),
    SettingDef("player.require_kyc_for_online", "Require KYC for online players", "bool", True, "Players"),
    SettingDef("player.self_exclusion_days", "Self-exclusion period (days)", "int", 30, "Players", "", 1, 3650),
    # ---- events / fights --------------------------------------------------
    SettingDef("event.default_fight_count", "Default games per event", "int", 20, "Events", "", 1, 200),
    SettingDef("event.require_location", "Require a location on every new event", "bool", True, "Events",
               "Only applies while at least one active location is registered (Admin Panel → Locations)."),
    SettingDef("event.auto_lock_on_declare", "Auto-lock fight before declaring", "bool", True, "Events"),
    SettingDef("fight.redeclare_requires_approval", "Redeclare needs a second approver", "bool", True, "Events",
               "Maker-checker: the requester cannot approve their own correction."),
    SettingDef("fight.cancel_window_seconds", "Ticket cancel window (seconds)", "int", 300, "Events",
               "Agents may cancel a ticket within this window, and only before the fight locks.", 0, 86400),
    SettingDef("live.board_enabled", "Public live board (/live)", "bool", True, "Events",
               "Shows fight number, betting open/closed, totals and odds to anyone with the link."),
    SettingDef("live.show_bola", "Show the number game on the live board", "bool", True, "Events",
               "Adds a second panel: last result per area and the next draw time. Never any stakes."),
    # ---- security ---------------------------------------------------------
    SettingDef("security.session_idle_minutes", "Idle session timeout (minutes)", "int", 30, "Security", "", 5, 480),
    SettingDef("security.max_login_attempts", "Failed logins before lockout", "int", 5, "Security", "", 3, 20),
    SettingDef("security.lockout_minutes", "Lockout duration (minutes)", "int", 15, "Security", "", 1, 1440),
    SettingDef("referral.default_ttl_minutes", "Referral code default lifetime (minutes)", "int", 1440, "Security", "Codes are always single-use; this is only the default timer.", 1, 43200),
    SettingDef("presence.window_minutes", "Count an account as online for (minutes)", "int", 5, "Security",
               "An account is 'online' if it did something within this many minutes.", 1, 60),
    SettingDef("security.password_min_length", "Minimum password length", "int", 12, "Security", "", 8, 128),
    # ---- branding / ticket ------------------------------------------------
    SettingDef("brand.name", "Brand name", "str", "StelNexus", "Branding", "", maximum=40),
    SettingDef("brand.tagline", "Tagline", "str", "Every fight, settled to the centavo", "Branding", "", maximum=80),
    SettingDef("ticket.header", "Ticket header", "str", "StelNexus", "Branding", "", maximum=40),
    SettingDef("ticket.footer", "Ticket footer", "str", "Winnings are credited to the player automatically. Not transferable.", "Branding", "", maximum=120),
    SettingDef("ticket.currency_symbol", "Currency symbol", "str", "₱", "Branding", "", maximum=4),
)

BY_KEY: dict[str, SettingDef] = {d.key: d for d in DEFS}
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(d.group for d in DEFS))


class SettingsStore:
    """Read-through cache of DB-backed settings, scoped to one request/session."""

    def __init__(self, db: Session) -> None:
        self._db = db
        self._cache: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cache is None:
            values = {d.key: d.default for d in DEFS}
            for row in self._db.scalars(select(AppSetting)).all():
                definition = BY_KEY.get(row.key)
                if definition is None:
                    continue
                try:
                    values[row.key] = definition.parse(row.value)
                except ValidationError:
                    values[row.key] = definition.default
            self._cache = values
        return self._cache

    def get(self, key: str) -> Any:
        if key not in BY_KEY:
            raise KeyError(key)
        return self._load()[key]

    def all(self) -> dict[str, Any]:
        return dict(self._load())

    def invalidate(self) -> None:
        self._cache = None

    def set(self, key: str, raw: str, actor_id: int | None) -> tuple[Any, Any]:
        definition = BY_KEY.get(key)
        if definition is None:
            raise ValidationError("Unknown setting.")
        new_value = definition.parse(raw)
        old_value = self.get(key)
        row = self._db.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=definition.serialize(new_value), updated_by_id=actor_id)
            self._db.add(row)
        else:
            row.value = definition.serialize(new_value)
            row.updated_by_id = actor_id
        self._db.flush()
        self.invalidate()
        return old_value, new_value


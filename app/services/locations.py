"""Locations: the venues events are held at.

The Admin controls the registry (Admin Panel -> Locations): name, address, free-form custom fields
and per-venue defaults that pre-fill a new event. A Master (or Admin) can also register a new
venue on the spot while creating an event. Archived locations disappear from the event form but
stay on the events that used them; a location with events can be archived, never deleted.
"""
from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, Forbidden, NotFound, ValidationError
from app.core.rbac import Perm, has_perm
from app.db.models import Event, Location, PayoutModel, User

# The venue pictures that ship with the app (file name -> label). Only these can be chosen, so a
# location can never point at an arbitrary path.
VENUE_IMAGES: dict[str, str] = {
    "lipa.svg": "Lipa dusk",
    "batangas-city.svg": "Batangas City port",
    "tanauan.svg": "Tanauan, Taal lake",
    "sto-tomas.svg": "Sto. Tomas hills",
    "nasugbu.svg": "Nasugbu beach",
}
# Sample venues for a dev database: (name, city, address, picture).
SAMPLE_LOCATIONS = (
    ("Lipa Arena", "Lipa City, Batangas", "Lipa City, Batangas", "lipa.svg"),
    ("Batangas City Coliseum", "Batangas City, Batangas", "Batangas City, Batangas", "batangas-city.svg"),
    ("Tanauan Arena", "Tanauan City, Batangas", "Tanauan City, Batangas", "tanauan.svg"),
    ("Sto. Tomas Pavilion", "Sto. Tomas, Batangas", "Sto. Tomas, Batangas", "sto-tomas.svg"),
    ("Nasugbu Beach Arena", "Nasugbu, Batangas", "Nasugbu, Batangas", "nasugbu.svg"),
)
_UNSET = object()

NAME_RE = re.compile(r"^[\w\s.,'&/#()-]{2,80}$", re.UNICODE)
MAX_CUSTOM_FIELDS = 12


def _need(actor: User, perm: Perm) -> None:
    if not has_perm(actor, perm):
        raise Forbidden("Your role is not allowed to do that with locations.")


def _clean(value: str | None, limit: int) -> str | None:
    value = " ".join((value or "").split())
    if len(value) > limit:
        raise ValidationError(f"Too long (max {limit} characters).")
    return value or None


def parse_custom_fields(labels, values) -> list[dict]:
    """Pair up the label/value rows from the form. Blank labels are dropped."""
    out: list[dict] = []
    seen: set[str] = set()
    for label, value in zip(labels or [], values or []):
        label = " ".join(str(label or "").split())
        value = " ".join(str(value or "").split())
        if not label:
            continue
        if len(label) > 40 or len(value) > 200:
            raise ValidationError("Custom field labels are max 40 characters, values max 200.")
        if label.lower() in seen:
            raise ValidationError(f"Custom field '{label}' is listed twice.")
        seen.add(label.lower())
        out.append({"label": label, "value": value})
    if len(out) > MAX_CUSTOM_FIELDS:
        raise ValidationError(f"At most {MAX_CUSTOM_FIELDS} custom fields per location.")
    return out


def _image(value) -> str | None:
    value = (value or "").strip() if isinstance(value, str) or value is None else ""
    if value and value not in VENUE_IMAGES:
        raise ValidationError("Choose one of the listed venue pictures.")
    return value or None


def _defaults(fight_count, payout_model, house_cut_bps) -> tuple[int | None, str | None, int | None]:
    def to_int(raw, label):
        if raw in (None, ""):
            return None
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{label} must be a whole number.") from exc
    count = to_int(fight_count, "Default games")
    cut = to_int(house_cut_bps, "Default tong")
    if count is not None and not 1 <= count <= 200:
        raise ValidationError("Default games must be between 1 and 200.")
    if cut is not None and not 0 <= cut <= 10000:
        raise ValidationError("Default tong must be between 0 and 10000 basis points.")
    model = (payout_model or "").strip() or None
    if model is not None and model not in (PayoutModel.POOL.value, PayoutModel.FIXED.value):
        raise ValidationError("Default payout model must be pool or fixed.")
    return count, model, cut


def _name_free(db: Session, name: str, *, except_id: int | None = None) -> None:
    stmt = select(Location.id).where(func.lower(Location.name) == name.lower())
    if except_id:
        stmt = stmt.where(Location.id != except_id)
    if db.scalar(stmt):
        raise Conflict("A location with that name already exists.")


def list_locations(db: Session, *, active_only: bool = False) -> list[Location]:
    stmt = select(Location)
    if active_only:
        stmt = stmt.where(Location.is_active.is_(True))
    return list(db.scalars(stmt.order_by(Location.is_active.desc(), Location.name)).all())


def get_location(db: Session, location_id: int) -> Location:
    row = db.get(Location, location_id)
    if row is None:
        raise NotFound("Location not found.")
    return row


def event_counts(db: Session) -> dict[int, int]:
    rows = db.execute(select(Event.location_id, func.count(Event.id))
                      .where(Event.location_id.is_not(None)).group_by(Event.location_id)).all()
    return {int(loc): int(n) for loc, n in rows}


def create_location(
    db: Session, actor: User, *, name: str, address: str | None = None, city: str | None = None,
    notes: str | None = None, custom_fields: list[dict] | None = None,
    default_fight_count=None, default_payout_model: str | None = None, default_house_cut_bps=None,
    image: str | None = None,
) -> Location:
    _need(actor, Perm.LOCATIONS_CREATE)
    name = " ".join((name or "").split())
    if not NAME_RE.match(name):
        raise ValidationError("Location name: 2-80 characters (letters, numbers, spaces and . , ' & / # ( ) -).")
    _name_free(db, name)
    count, model, cut = _defaults(default_fight_count, default_payout_model, default_house_cut_bps)
    row = Location(
        name=name, address=_clean(address, 200), city=_clean(city, 80), notes=_clean(notes, 500),
        image=_image(image),
        custom_fields=custom_fields or None, default_fight_count=count,
        default_payout_model=model, default_house_cut_bps=cut, is_active=True, created_by_id=actor.id,
    )
    db.add(row)
    db.flush()
    audit.record(db, actor, "location.create", entity_type="location", entity_id=row.id,
                 after={"name": row.name, "city": row.city})
    return row


def update_location(
    db: Session, actor: User, location_id: int, *, name: str, address: str | None, city: str | None,
    notes: str | None, custom_fields: list[dict] | None, default_fight_count, default_payout_model,
    default_house_cut_bps, image=_UNSET,
) -> Location:
    _need(actor, Perm.LOCATIONS_MANAGE)
    row = get_location(db, location_id)
    name = " ".join((name or "").split())
    if not NAME_RE.match(name):
        raise ValidationError("Location name: 2-80 characters (letters, numbers, spaces and . , ' & / # ( ) -).")
    _name_free(db, name, except_id=row.id)
    count, model, cut = _defaults(default_fight_count, default_payout_model, default_house_cut_bps)
    before = {"name": row.name, "address": row.address, "city": row.city, "notes": row.notes,
              "custom_fields": row.custom_fields, "fights": row.default_fight_count,
              "model": row.default_payout_model, "cut": row.default_house_cut_bps}
    row.name, row.address, row.city = name, _clean(address, 200), _clean(city, 80)
    row.notes = _clean(notes, 500)
    if image is not _UNSET:               # callers that do not know about pictures keep the current one
        row.image = _image(image)
    row.custom_fields = custom_fields or None
    row.default_fight_count, row.default_payout_model, row.default_house_cut_bps = count, model, cut
    db.flush()
    audit.record(db, actor, "location.update", entity_type="location", entity_id=row.id, before=before,
                 after={"name": row.name, "address": row.address, "city": row.city, "notes": row.notes,
                        "custom_fields": row.custom_fields, "fights": count, "model": model, "cut": cut})
    return row


def seed_samples(db: Session, actor: User, *, attach_events: bool = True) -> dict:
    """Dev helper: register the five sample Batangas venues (with pictures) and, optionally, give
    every event that has no location one of them. Safe to run again: nothing is duplicated."""
    made, pictured = [], []
    rows: list[Location] = []
    for name, city, address, picture in SAMPLE_LOCATIONS:
        row = db.scalar(select(Location).where(func.lower(Location.name) == name.lower()))
        if row is None:
            row = create_location(db, actor, name=name, city=city, address=address, image=picture)
            made.append(name)
        elif not row.image:
            row.image = picture
            pictured.append(name)
        rows.append(row)
    attached = 0
    if attach_events:
        loose = db.scalars(select(Event).where(Event.location_id.is_(None)).order_by(Event.id)).all()
        for i, event in enumerate(loose):
            event.location_id = rows[i % len(rows)].id
            attached += 1
    db.flush()
    return {"created": made, "pictured": pictured, "events_attached": attached}


def set_active(db: Session, actor: User, location_id: int, active: bool) -> Location:
    _need(actor, Perm.LOCATIONS_MANAGE)
    row = get_location(db, location_id)
    row.is_active = bool(active)
    db.flush()
    audit.record(db, actor, "location.archive" if not active else "location.restore",
                 entity_type="location", entity_id=row.id, after={"name": row.name})
    return row


def delete_location(db: Session, actor: User, location_id: int) -> None:
    _need(actor, Perm.LOCATIONS_MANAGE)
    row = get_location(db, location_id)
    if event_counts(db).get(row.id):
        raise Conflict("This location has events. Archive it instead; its events keep the name.")
    audit.record(db, actor, "location.delete", entity_type="location", entity_id=row.id,
                 before={"name": row.name})
    db.delete(row)
    db.flush()

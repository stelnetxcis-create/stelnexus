"""Booklets: batches of pre-numbered ticket pages.

The original generated booklets with a page counter but nothing showed the codes
being consumed atomically. Here a page is claimed with row-level locking, so two
tellers can never print the same ticket code.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import audit
from app.core.errors import Conflict, NotFound, ValidationError
from app.core.security import random_code
from app.core.settings_registry import SettingsStore
from app.db.models import Booklet, BookletPage, Status, User


def generate(db: Session, actor: User, *, pages: int | None, settings: SettingsStore) -> Booklet:
    count = int(pages or settings.get("booklet.default_pages"))
    if not 1 <= count <= 10000:
        raise ValidationError("Pages must be between 1 and 10000.")
    code = _unique_code(db, prefix="BK")
    booklet = Booklet(code=code, pages_total=count, pages_available=count,
                      status=Status.ACTIVE, created_by_id=actor.id)
    db.add(booklet)
    db.flush()
    for number in range(1, count + 1):
        db.add(BookletPage(booklet_id=booklet.id, page_no=number,
                           code=_unique_code(db, prefix=code[-4:])))
    db.flush()
    audit.record(db, actor, "booklet.generate", entity_type="booklet", entity_id=booklet.id,
                 after={"code": code, "pages": count})
    return booklet


def _unique_code(db: Session, prefix: str = "") -> str:
    for _ in range(20):
        code = f"{prefix}{random_code(8)}"[:24]
        exists = db.scalar(select(Booklet.id).where(Booklet.code == code)) or db.scalar(
            select(BookletPage.id).where(BookletPage.code == code))
        if not exists:
            return code
    raise Conflict("Could not allocate a unique code, please retry.")


def take_page(db: Session) -> BookletPage | None:
    """Atomically claim one unused page. Returns None when booklets are exhausted."""
    page = db.scalar(
        select(BookletPage)
        .where(BookletPage.is_used.is_(False))
        .order_by(BookletPage.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if page is None:
        return None
    booklet = db.get(Booklet, page.booklet_id)
    if booklet is None or booklet.pages_available <= 0:
        return None
    page.is_used = True
    booklet.pages_available = max(0, booklet.pages_available - 1)
    if booklet.pages_available == 0:
        booklet.status = Status.CLOSED
    from app.db.base import utcnow
    page.used_at = utcnow()
    db.flush()
    return page


def release_page(db: Session, page_id: int) -> None:
    page = db.get(BookletPage, page_id)
    if page is None or not page.is_used:
        return
    page.is_used = False
    page.used_at = None
    booklet = db.get(Booklet, page.booklet_id)
    if booklet is not None:
        booklet.pages_available = min(booklet.pages_total, booklet.pages_available + 1)
        if booklet.status == Status.CLOSED:
            booklet.status = Status.ACTIVE
    db.flush()


def list_booklets(db: Session, limit: int = 100) -> list[Booklet]:
    return list(db.scalars(select(Booklet).order_by(Booklet.id.desc()).limit(min(limit, 500))).all())


def get_booklet(db: Session, booklet_id: int) -> Booklet:
    booklet = db.get(Booklet, booklet_id)
    if booklet is None:
        raise NotFound("Booklet not found.")
    return booklet


def stats(db: Session) -> dict:
    return {
        "booklets": int(db.scalar(select(func.count(Booklet.id))) or 0),
        "pages_available": int(db.scalar(select(func.coalesce(func.sum(Booklet.pages_available), 0))) or 0),
    }

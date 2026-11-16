"""Result pictures: the photo a Pawn (or anyone allowed) attaches when declaring a game.

* The file type is decided from the file's own first bytes (JPEG / PNG / WebP), never from the
  filename or the browser's content-type. SVG and anything else is refused, so an upload can never
  carry script.
* Files live in ``UPLOAD_DIR/proofs`` (outside ``/static``) under a random name and are only served
  through ``/fights/{id}/proof`` to signed-in staff.
* Pawns must attach one when ``fight.pawn_proof_required`` is on; Admin and Declarator may, but need not.
  (Master cannot declare at all.)
"""
from __future__ import annotations

import datetime as dt
import secrets
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.core import audit
from app.core.config import get_settings
from app.core.errors import ValidationError
from app.core.settings_registry import SettingsStore
from app.db.models import Fight, Role, User

_cfg = get_settings()

_TYPES = (
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
)


@dataclass(frozen=True)
class Proof:
    data: bytes
    mime: str
    ext: str


def required_for(actor: User, settings: SettingsStore) -> bool:
    """A Pawn has to show the picture; Admin and Declarator may."""
    return actor.role == Role.PAWN and bool(settings.get("fight.pawn_proof_required"))


def _detect(data: bytes) -> tuple[str, str] | None:
    for magic, mime, ext in _TYPES:
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def check_upload(data: bytes | None) -> Proof | None:
    """Return the validated picture, or None when nothing was uploaded."""
    if not data:
        return None
    if len(data) > _cfg.MAX_UPLOAD_BYTES:
        raise ValidationError(f"The picture is too large (max {_cfg.MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")
    kind = _detect(data)
    if kind is None:
        raise ValidationError("Upload a JPG, PNG or WebP picture.")
    return Proof(data=data, mime=kind[0], ext=kind[1])


def _dir() -> Path:
    path = Path(_cfg.UPLOAD_DIR) / "proofs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def attach(db: Session, actor: User, fight: Fight, proof: Proof) -> None:
    """Store the picture and point the game at it. A replaced picture stays on disk as evidence."""
    name = f"g{fight.id}-{secrets.token_hex(12)}.{proof.ext}"
    (_dir() / name).write_bytes(proof.data)
    before = fight.proof_file
    fight.proof_file = name
    fight.proof_mime = proof.mime
    fight.proof_by_id = actor.id
    fight.proof_at = dt.datetime.now(dt.timezone.utc)
    db.flush()
    audit.record(db, actor, "fight.proof", entity_type="fight", entity_id=fight.id,
                 before={"file": before}, after={"file": name, "bytes": len(proof.data)})


def path_of(fight: Fight) -> Path | None:
    """Where the game's picture is on disk (None if it has none or the file is gone)."""
    if not fight.proof_file:
        return None
    path = (_dir() / Path(fight.proof_file).name)      # .name: a stored value can never escape the folder
    return path if path.is_file() else None

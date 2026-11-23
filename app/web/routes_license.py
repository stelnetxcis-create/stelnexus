"""The public License page: who licenses us, the license number, and where to verify it."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.errors import NotFound
from app.services.license_info import license_info
from app.web.deps import Ctx, optional_ctx, render

router = APIRouter()


@router.get("/license")
async def license_page(ctx: Ctx = Depends(optional_ctx)):
    info = license_info(ctx.store)
    if not info["published"]:
        raise NotFound("License details have not been published.")
    return render(ctx, "license.html", lic=info)

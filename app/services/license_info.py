"""The operator's license details (Variables -> License), prepared for pages. Nothing is invented: the License page and the
clickable logo exist only once the Admin has typed a license number; the verify link is shown only if it is https://."""
from __future__ import annotations


def license_info(store) -> dict:
    def get(key: str) -> str:
        return str(store.get(key) or "").strip()

    verify = get("license.verify_url")
    if not (verify.lower().startswith("https://") and " " not in verify):
        verify = ""
    info = {"issuer": get("license.issuer"), "holder": get("license.holder"), "number": get("license.number"),
            "valid_until": get("license.valid_until"), "verify_url": verify, "statement": get("license.statement"),
            "notice": get("license.notice")}
    info["published"] = bool(info["number"])
    info["short"] = (f"Licensed by {info['issuer']}" if info["issuer"] else "Licensed") + \
                    (f" · License No. {info['number']}" if info["number"] else "")
    return info

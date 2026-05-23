"""aafiya (tpa.aafiya.ae) eligibility check.

Flow: log in -> open the Member System Info page -> pick "EMIRATES ID" as the
selection type -> type the EID -> Search. A modal ("Member System Info") pops
up with the member's details when the member is found; we read it and decide
eligibility from the "Member Status/Date" field (ACTIVE => eligible).
"""
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

from . import format_eid, headless, load_config, LAUNCH_ARGS

PORTAL_NAME = "aafiya"
OUT = Path(__file__).parent.parent / "exploration" / PORTAL_NAME
OUT.mkdir(parents=True, exist_ok=True)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# label text shown in the Member System Info modal -> our detail key.
# keys match static/index.html FIELD_META so the UI renders Arabic labels.
_LABEL_MAP = {
    "MEMBER NUMBER": "member_id",
    "MEMBER NAME": "member_name",
    "EMIRATES ID": "emirates_id",
    "EXPIRY DATE": "expiry_date",
    "ENROLLMENT DATE": "effective_date",
    "EMPLOYEE NUMBER": "employee_number",
    "PAYER": "payer",
    "GEOGRAPHIC COVERAGE": "visa_emirate",
    "MEMBER STATUS/DATE": "status_raw",
    "GENDER": "gender",
    "NETWORK CLASSIFICATION": "network",
    "DATE OF BIRTH": "dob",
}


def _parse_modal(modal) -> dict:
    """Walk the two info tables in the modal, mapping label -> value."""
    out: dict = {}
    for row in modal.query_selector_all("table.infotable tr"):
        tds = row.query_selector_all("td")
        if len(tds) < 2:
            continue
        label = (tds[0].inner_text() or "").strip().rstrip(":").strip().upper()
        value = " ".join((tds[1].inner_text() or "").split()).strip()
        if label and label in _LABEL_MAP and value:
            out[_LABEL_MAP[label]] = value
    return out


def _search(page, value: str, timeout: int) -> bool:
    """Run one member search; return True if the result modal appears."""
    page.select_option("#SelectionType", value="2")  # EMIRATES ID
    page.wait_for_timeout(300)
    page.fill("#SearchParam", "")
    page.fill("#SearchParam", value)
    page.click("#FindMember")
    try:
        page.locator("#MemberSysteminfoDetailsID").wait_for(
            state="visible", timeout=timeout
        )
        page.wait_for_timeout(1200)  # let Angular bindings settle
        return True
    except Exception:
        return False


def check(emirates_id: str, **_) -> dict:
    cfg = load_config(PORTAL_NAME)
    eid = format_eid(emirates_id)
    digits = "".join(c for c in emirates_id if c.isdigit())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless(), args=LAUNCH_ARGS)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 850},
            user_agent=_UA,
        )
        page = ctx.new_page()
        try:
            # ---- login ----
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=40000)
            page.fill("#UserName", cfg["username"])
            page.fill("#Password", cfg["password"])
            page.click("#LoginSubmit")
            page.wait_for_timeout(3500)
            if page.query_selector("#LoginSubmit"):
                return {
                    "portal": PORTAL_NAME,
                    "status": "ERROR",
                    "message": "login failed (check credentials)",
                    "details": {},
                }

            # ---- open member info page ----
            page.goto(cfg["member_url"], wait_until="domcontentloaded", timeout=40000)
            try:
                page.wait_for_load_state("networkidle", timeout=12000)
            except Exception:
                pass
            page.wait_for_selector("#SelectionType", timeout=15000)
            page.wait_for_timeout(800)

            # ---- search (dashed EID first, plain digits as fallback) ----
            found = _search(page, eid, timeout=18000)
            if not found and digits and digits != eid:
                found = _search(page, digits, timeout=10000)

            page.screenshot(path=str(OUT / "02_result.png"), full_page=True)

            if not found:
                return {
                    "portal": PORTAL_NAME,
                    "status": "NOT_ELIGIBLE",
                    "message": "member not found",
                    "details": {},
                }

            modal = page.query_selector("#MemberSysteminfoDetailsID")
            details = _parse_modal(modal) if modal else {}
            status_raw = details.get("status_raw", "")

            if "ACTIVE" in status_raw.upper():
                status = "ELIGIBLE"
            elif details.get("member_name"):
                # member exists but not active (expired / suspended)
                status = "NOT_ELIGIBLE"
            else:
                status = "UNKNOWN"

            return {
                "portal": PORTAL_NAME,
                "status": status,
                "message": details.get("network", "") or status_raw,
                "details": details,
            }
        except Exception as e:
            msg = str(e)
            if "ERR_CONNECTION_TIMED_OUT" in msg or "ERR_NAME_NOT_RESOLVED" in msg or "ECONNREFUSED" in msg:
                msg = "لا يمكن الوصول لبوابة عافية من هذا الاتصال. يرجى التشغيل من شبكة الكلينيك."
            return {
                "portal": PORTAL_NAME,
                "status": "ERROR",
                "message": msg,
                "details": {},
            }
        finally:
            browser.close()

"""globalmed (genix.globemedgulf.com) eligibility check.

GlobeMed runs a GeneXus "CareGate" provider portal with a 2-step login
(username -> Next -> password -> Log in). The eligibility page has the
Provider and "Patient Identifier" (National ID / Emirates ID) pre-selected,
so we just type the Emirates ID and click "Check Eligibility".
"""
from __future__ import annotations

import re
from pathlib import Path
from playwright.sync_api import sync_playwright

from . import format_eid, headless, load_config

PORTAL_NAME = "globalmed"
OUT = Path(__file__).parent.parent / "exploration" / PORTAL_NAME
OUT.mkdir(parents=True, exist_ok=True)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]

_NEGATIVE = (
    "NOT ELIGIBLE", "NOT ACTIVE", "INACTIVE", "NOT FOUND", "NO RECORD",
    "INVALID", "NOT COVERED", "EXPIRED", "NOT VALID", "NO DATA",
)
_POSITIVE = ("ELIGIBLE", "ACTIVE", "COVERED", "VALID")


def _login(page, cfg) -> bool:
    """Two-step GeneXus login. Returns True if we end up on EligibilityCheck."""
    page.wait_for_selector(
        "input[placeholder*='USERNAME' i], #b3-INP_Username", timeout=25000
    )
    user = (page.query_selector("input[placeholder*='USERNAME' i]")
            or page.query_selector("#b3-INP_Username"))
    user.fill(cfg["username"])
    page.wait_for_timeout(400)
    try:
        page.click("button:has-text('Next'), a:has-text('Next'), input[type=submit][value*='Next' i]")
    except Exception:
        user.press("Enter")

    page.wait_for_selector("input[type=password]", timeout=15000)
    page.wait_for_timeout(400)
    pwd = page.query_selector("input[type=password]")
    pwd.fill(cfg["password"])
    page.wait_for_timeout(300)
    try:
        page.click("button:has-text('Log in'), button:has-text('Login'), button:has-text('Sign In'), input[type=submit]")
    except Exception:
        pwd.press("Enter")

    try:
        page.wait_for_load_state("networkidle", timeout=25000)
    except Exception:
        pass
    page.wait_for_timeout(3000)
    return "login" not in page.url.lower()


def _parse_result(page) -> tuple[str, dict]:
    body = page.locator("body").inner_text() or ""
    up = body.upper()

    # GlobeMed shows explicit messages: a banner ("Adherent not found") and a
    # field note ("The member is (not) eligible").
    if "ADHERENT NOT FOUND" in up or "NOT FOUND" in up:
        return "NOT_ELIGIBLE", {"status_raw": "Adherent not found"}

    m = re.search(r"the member is (not )?eligible", body, re.IGNORECASE)
    if m:
        msg = " ".join(m.group(0).split())
        if m.group(1):  # "not eligible"
            return "NOT_ELIGIBLE", {"status_raw": msg}
        return "ELIGIBLE", {"status_raw": msg}

    # fallback keyword scan (whole-word ELIGIBLE to avoid menu noise)
    if any(n in up for n in _NEGATIVE):
        return "NOT_ELIGIBLE", {}
    if re.search(r"\bELIGIBLE\b", up) and "NOT ELIGIBLE" not in up:
        return "ELIGIBLE", {}
    return "UNKNOWN", {}


def check(emirates_id: str, captcha_solver=None, **_) -> dict:
    cfg = load_config(PORTAL_NAME)
    eid = format_eid(emirates_id)
    digits = "".join(c for c in emirates_id if c.isdigit())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless(), args=_LAUNCH_ARGS)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 850},
            user_agent=_UA,
            locale="en-US",
        )
        page = ctx.new_page()
        try:
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=45000)

            body_text = (page.locator("body").inner_text() or "").lower()
            if "prohibited" in body_text or not body_text.strip():
                return {
                    "portal": PORTAL_NAME,
                    "status": "ERROR",
                    "message": "بوابة Global Med محجوبة من هذا الاتصال. يرجى التشغيل من شبكة الكلينيك.",
                    "details": {},
                }

            if not _login(page, cfg):
                return {
                    "portal": PORTAL_NAME,
                    "status": "ERROR",
                    "message": "login failed (check credentials)",
                    "details": {},
                }

            # ensure we're on the eligibility page
            if "eligibilitycheck" not in page.url.lower():
                page.goto(cfg["url"], wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector(
                "input[placeholder*='Iqama' i], input[placeholder*='Nationality' i], #b4-b1-Input_L_IQAMANBR2",
                timeout=20000,
            )
            page.wait_for_timeout(1000)

            id_field = (page.query_selector("input[placeholder*='Iqama' i]")
                        or page.query_selector("input[placeholder*='Nationality' i]")
                        or page.query_selector("#b4-b1-Input_L_IQAMANBR2"))
            id_field.fill(eid)
            page.wait_for_timeout(400)
            # "Check Eligibility" appears more than once — click the visible one
            clicked = False
            for el in page.query_selector_all("button, a, input[type=submit], [role=button]"):
                txt = (el.inner_text() or el.get_attribute("value") or "").strip().lower()
                if "check eligibility" in txt:
                    try:
                        if el.is_visible():
                            el.click()
                            clicked = True
                            break
                    except Exception:
                        continue
            if not clicked:
                page.locator(
                    "button:has-text('Check Eligibility'), a:has-text('Check Eligibility')"
                ).first.click()

            # wait for the result to render
            for _ in range(28):
                page.wait_for_timeout(700)
                up = (page.locator("body").inner_text() or "").upper()
                if any(k in up for k in _NEGATIVE) or any(
                    re.search(r"\b" + re.escape(k) + r"\b", up) for k in _POSITIVE
                ):
                    break

            page.screenshot(path=str(OUT / "20_result.png"), full_page=True)
            (OUT / "20_result.html").write_text(page.content(), encoding="utf-8")

            status, details = _parse_result(page)
            return {
                "portal": PORTAL_NAME,
                "status": status,
                "message": details.get("network", "") or details.get("payer", ""),
                "details": details,
            }
        except Exception as e:
            msg = str(e)
            if "ERR_CONNECTION_TIMED_OUT" in msg or "ERR_NAME_NOT_RESOLVED" in msg or "ECONNREFUSED" in msg:
                msg = "لا يمكن الوصول لبوابة Global Med من هذا الاتصال. يرجى التشغيل من شبكة الكلينيك."
            return {
                "portal": PORTAL_NAME,
                "status": "ERROR",
                "message": msg,
                "details": {},
            }
        finally:
            browser.close()

"""GIG Gulf (partners.gig-gulf.com) eligibility check.

The login form has an image CAPTCHA (5-6 alphanumeric chars with strikethrough).
Strategy:
  1. Reuse a saved session if it's fresh (< 8 hours old)
  2. Otherwise try OCR up to 3 times
  3. If OCR fails, pause and let the user solve the CAPTCHA manually
  4. Save the session for the rest of the day
"""
from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

import pytesseract
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

from . import format_eid, headless, load_config, LAUNCH_ARGS

_TESSERACT_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(_TESSERACT_DEFAULT).exists():
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_DEFAULT

PORTAL_NAME = "gig_axa"
ROOT = Path(__file__).parent.parent
OUT = ROOT / "exploration" / PORTAL_NAME
OUT.mkdir(parents=True, exist_ok=True)
STATE_FILE = ROOT / ".sessions" / f"{PORTAL_NAME}.json"
STATE_FILE.parent.mkdir(exist_ok=True)
SESSION_MAX_AGE_HOURS = 8

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _session_is_fresh() -> bool:
    if not STATE_FILE.exists():
        return False
    age_h = (time.time() - STATE_FILE.stat().st_mtime) / 3600
    return age_h < SESSION_MAX_AGE_HOURS


def _preprocess(image_path: Path) -> Image.Image:
    img = Image.open(image_path).convert("L")
    img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = img.point(lambda v: 0 if v < 130 else 255, mode="L")
    return img


def _solve_text_captcha(image_path: Path) -> str | None:
    """OCR a text CAPTCHA (5-6 alphanumeric characters)."""
    candidates = []
    pre = _preprocess(image_path)
    pre.save(image_path.with_suffix(".pre.png"))

    chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    for psm in (7, 8, 6, 13):
        for source in (pre, Image.open(image_path)):
            cfg = f"--oem 3 --psm {psm} -c tessedit_char_whitelist={chars}"
            raw = pytesseract.image_to_string(source, config=cfg).strip()
            cleaned = re.sub(r"[^A-Z0-9]", "", raw.upper())
            if 4 <= len(cleaned) <= 8:
                candidates.append(cleaned)

    if not candidates:
        return None
    counts = Counter(candidates)
    best, _ = counts.most_common(1)[0]
    return best


def _wait_for_manual_login(page) -> bool:
    """Pause until the URL leaves /ProviderLogin/. 3-minute timeout."""
    print("\n  >>> CAPTCHA detected. Please solve it in the open browser. <<<")
    print("  The script will continue automatically once you're logged in.\n")
    for _ in range(180):
        page.wait_for_timeout(1000)
        if "/ProviderLogin/" not in page.url and "login.aspx" not in page.url.lower():
            return True
    return False


def _logged_in(page) -> bool:
    url = page.url.lower()
    return "/providerlogin/" not in url and "login.aspx" not in url


# result label (as shown on the card page) -> our detail key. Keys match
# static/index.html FIELD_META so the UI renders Arabic labels.
_RESULT_LABEL_MAP = {
    "MEMBER NAME": "member_name",
    "NAME": "member_name",
    "MEMBERSHIP NO": "member_id",
    "MEMBERSHIP NUMBER": "member_id",
    "MEMBER NO": "member_id",
    "CARD NO": "card",
    "CARD NUMBER": "card",
    "EMIRATES ID": "emirates_id",
    "DATE OF BIRTH": "dob",
    "DOB": "dob",
    "GENDER": "gender",
    "NETWORK": "network",
    "PLAN": "network",
    "PAYER": "payer",
    "EFFECTIVE DATE": "effective_date",
    "START DATE": "effective_date",
    "EXPIRY DATE": "expiry_date",
    "EXPIRY": "expiry_date",
    "VALID UPTO": "expiry_date",
    "VALID TILL": "expiry_date",
    "END DATE": "expiry_date",
    "STATUS": "status_raw",
    "MEMBER STATUS": "status_raw",
}

_NEGATIVE_MARKERS = (
    "NO RECORD", "NOT FOUND", "NO MEMBER", "INVALID", "DOES NOT EXIST",
    "NOT A VALID", "NO DATA", "RECORD NOT", "NOT ELIGIBLE", "NO ACTIVE",
)


def _extract_pairs(el) -> dict:
    """Pull label:value pairs out of a result container (ASP.NET table rows)."""
    out: dict = {}
    if not el:
        return out
    for row in el.query_selector_all("tr"):
        cells = [" ".join((c.inner_text() or "").split())
                 for c in row.query_selector_all("td, th")]
        cells = [c for c in cells if c]
        for i in range(len(cells) - 1):
            label = cells[i].rstrip(":").strip().upper()
            if label in _RESULT_LABEL_MAP:
                value = cells[i + 1].strip()
                if value and value.rstrip(":").strip().upper() not in _RESULT_LABEL_MAP:
                    out.setdefault(_RESULT_LABEL_MAP[label], value)
    return out


def _parse_result(page, alerts=None):
    """Return (status, details) from the member-card result page."""
    err_el = page.query_selector("#ctl00_pagecontent_lblError")
    err = (err_el.inner_text().strip() if err_el else "")
    jc_el = page.query_selector(".jconfirm-content, .jconfirm-content-pane")
    jc = (jc_el.inner_text().strip() if jc_el else "")
    alert_txt = " ".join(alerts or []).strip()
    res_el = page.query_selector("#ctl00_pagecontent_updResult")

    details = _extract_pairs(res_el)
    body_up = (page.locator("body").inner_text() or "").upper()
    notice = " ".join(t for t in (err, jc, alert_txt) if t).strip()
    notice_up = notice.upper()
    status_raw = (details.get("status_raw") or "").upper()

    if details.get("member_name"):
        # a member record was rendered
        if any(b in status_raw for b in ("INACTIVE", "EXPIRED", "NOT")):
            status = "NOT_ELIGIBLE"
        else:
            status = "ELIGIBLE"
    elif notice and any(n in notice_up for n in _NEGATIVE_MARKERS):
        status = "NOT_ELIGIBLE"
        details["status_raw"] = notice
    elif any(n in body_up for n in _NEGATIVE_MARKERS):
        status = "NOT_ELIGIBLE"
    else:
        # no member data and no clear message — GIG re-renders the empty form
        # for non-members. Treat as NOT_ELIGIBLE (patient not on GIG books).
        status = "NOT_ELIGIBLE"
        details["status_raw"] = notice or "Member not found on GIG portal"
    return status, details


def check(emirates_id: str, captcha_solver=None, **_) -> dict:
    cfg = load_config(PORTAL_NAME)
    eid = format_eid(emirates_id)
    eid_digits = "".join(c for c in emirates_id if c.isdigit())

    have_session = _session_is_fresh()
    # The CAPTCHA is normally solved via the web UI (captcha_solver) or OCR, so
    # no visible browser is needed. We only fall back to a visible browser for
    # manual solving when there's no UI solver and no fresh session.
    need_manual_browser = (captcha_solver is None) and (not have_session)
    is_headless = headless() and not need_manual_browser

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=is_headless, args=LAUNCH_ARGS)
        ctx_kwargs = {
            "viewport": {"width": 1366, "height": 850},
            "user_agent": _UA,
        }
        if have_session:
            ctx_kwargs["storage_state"] = str(STATE_FILE)
            print(f"  reusing saved session")
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        # GIG signals "no record found" via a native alert() — capture its text
        # (and accept it) instead of letting Playwright silently dismiss it.
        alerts: list = []
        page.on("dialog", lambda d: (alerts.append(d.message), d.accept()))
        try:
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            if not _logged_in(page):
                # fill credentials
                page.fill("#txtUserName", cfg["username"])
                page.fill("#txtPassword", cfg["password"])

                solved = False
                for attempt in range(1, 4):
                    try:
                        cap_img = page.locator(
                            "img[id*='Captcha' i], img[src*='Captcha' i]"
                        ).first
                        cap_img.wait_for(state="visible", timeout=5000)
                    except Exception:
                        # no CAPTCHA shown — maybe the credentials alone passed
                        if _logged_in(page):
                            solved = True
                        break

                    img_bytes = cap_img.screenshot()
                    (OUT / f"captcha_attempt{attempt}.png").write_bytes(img_bytes)

                    answer = None
                    if captcha_solver is not None:
                        # surface the CAPTCHA to the web UI for the user to type
                        answer = captcha_solver(
                            PORTAL_NAME,
                            img_bytes,
                            "اكتب رمز التحقق الظاهر في الصورة",
                        )
                    else:
                        # no UI solver — try OCR (guarded: tesseract may be absent)
                        try:
                            answer = _solve_text_captcha(
                                OUT / f"captcha_attempt{attempt}.png"
                            )
                        except Exception as oce:
                            print(f"  OCR unavailable: {oce}")
                            answer = None
                    print(f"  captcha attempt {attempt}: {answer!r}")

                    if not answer:
                        page.reload()
                        page.wait_for_timeout(1500)
                        page.fill("#txtUserName", cfg["username"])
                        page.fill("#txtPassword", cfg["password"])
                        continue

                    page.fill("input[name='CaptchaControl']", answer)
                    page.click("#btnSubmit")
                    page.wait_for_timeout(3000)
                    if _logged_in(page):
                        solved = True
                        break
                    # likely wrong CAPTCHA — reload and retry
                    page.reload()
                    page.wait_for_timeout(1500)
                    page.fill("#txtUserName", cfg["username"])
                    page.fill("#txtPassword", cfg["password"])

                if not solved:
                    # last resort: visible browser for manual solving (CLI only)
                    if need_manual_browser and _wait_for_manual_login(page):
                        solved = True
                    if not solved:
                        return {
                            "portal": PORTAL_NAME,
                            "status": "ERROR",
                            "message": "login failed — CAPTCHA not solved",
                            "details": {},
                        }

                ctx.storage_state(path=str(STATE_FILE))
                print(f"  session saved -> {STATE_FILE.name}")

            page.screenshot(path=str(OUT / "02_after_login.png"), full_page=True)
            (OUT / "02_after_login.html").write_text(page.content(), encoding="utf-8")

            # ---- member lookup on the Card Authentication page ----
            try:
                page.wait_for_selector(
                    "#ctl00_pagecontent_txtEmiratesId", timeout=15000
                )
            except Exception:
                return {
                    "portal": PORTAL_NAME,
                    "status": "ERROR",
                    "message": "member lookup form not found after login",
                    "details": {"url": page.url},
                }

            page.fill("#ctl00_pagecontent_txtEmiratesId", eid)
            page.eval_on_selector(
                "#ctl00_pagecontent_txtEmiratesId", "el => el.blur()"
            )
            # Native click on the ASP.NET submit button (this keeps the framework's
            # __EVENTVALIDATION intact — forcing form.submit() triggers a runtime
            # error on the server).
            page.click("#ctl00_pagecontent_btnCheckValid")

            # For a found member GIG asks "Is the member at your facility at the
            # moment?" (jquery-confirm Yes/No). Answer "Yes" so the card details
            # render. (Harmless for an eligibility view.)
            try:
                page.wait_for_selector(".jconfirm-buttons button", timeout=8000)
                btns = page.query_selector_all(".jconfirm-buttons button")
                target = next(
                    (b for b in btns if "yes" in (b.inner_text() or "").lower()),
                    btns[0] if btns else None,
                )
                if target:
                    target.click()
            except Exception:
                pass

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            # wait for a result, an error, or an alert to arrive
            for _ in range(24):
                page.wait_for_timeout(700)
                res = page.query_selector("#ctl00_pagecontent_updResult")
                err = page.query_selector("#ctl00_pagecontent_lblError")
                if alerts or (res and res.inner_text().strip()) \
                        or (err and err.inner_text().strip()):
                    break
            page.wait_for_timeout(800)

            page.screenshot(path=str(OUT / "03_member_result.png"), full_page=True)
            (OUT / "03_member_result.html").write_text(page.content(), encoding="utf-8")

            try:
                _res = page.query_selector("#ctl00_pagecontent_updResult")
                _resn = len((_res.inner_text() or "").strip()) if _res else -1
                print(f"  [gig] url={page.url} alerts={alerts!r} updResult_len={_resn}", flush=True)
            except Exception:
                pass

            status, details = _parse_result(page, alerts)
            return {
                "portal": PORTAL_NAME,
                "status": status,
                "message": details.get("network", "") or details.get("status_raw", ""),
                "details": details,
            }
        except Exception as e:
            return {
                "portal": PORTAL_NAME,
                "status": "ERROR",
                "message": str(e),
                "details": {},
            }
        finally:
            page.wait_for_timeout(1500)
            browser.close()

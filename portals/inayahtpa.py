"""inayahtpa (provider.inayahtpa.com) eligibility check.

Quirks:
- "Welcome" terms modal blocks the form on first load
- Login form has a DNTCaptcha math-image (e.g. "19 + 56 = ?"). Tesseract
  struggles with the stencil font, so the strategy is:
    1) Try OCR with preprocessing (3 attempts)
    2) If OCR fails, open the browser and let the user solve once
    3) Save cookies + sessionStorage on success
    4) Reuse the saved session on subsequent runs (much faster + no CAPTCHA)
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytesseract
from PIL import Image, ImageFilter
from playwright.sync_api import sync_playwright

from . import format_eid, load_config, LAUNCH_ARGS

# tesseract is installed by winget at this fixed path on Windows
_TESSERACT_DEFAULT = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if Path(_TESSERACT_DEFAULT).exists():
    pytesseract.pytesseract.tesseract_cmd = _TESSERACT_DEFAULT

PORTAL_NAME = "inayahtpa"
ROOT = Path(__file__).parent.parent
OUT = ROOT / "exploration" / PORTAL_NAME
OUT.mkdir(parents=True, exist_ok=True)
STATE_FILE = ROOT / ".sessions" / f"{PORTAL_NAME}.json"
STATE_FILE.parent.mkdir(exist_ok=True)
# treat a saved session as fresh for this many hours
SESSION_MAX_AGE_HOURS = 8


def _preprocess(image_path: Path) -> Image.Image:
    """Up-scale + grayscale + threshold to give tesseract clean black-on-white."""
    img = Image.open(image_path).convert("L")  # grayscale
    img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
    img = img.filter(ImageFilter.MedianFilter(size=3))  # denoise
    # binarise — pixels darker than 130 become black, rest white
    img = img.point(lambda v: 0 if v < 130 else 255, mode="L")
    # invert if needed (we want dark text on light bg for tesseract)
    return img


def _solve_math_captcha(image_path: Path) -> int | None:
    """OCR the captcha image and evaluate the arithmetic expression.

    The portal generates two-operand expressions like:
        19 + 56     7 * 3     12 - 4
    Returns the integer result, or None if we can't parse.
    """
    candidates = []
    pre = _preprocess(image_path)
    pre.save(image_path.with_suffix(".pre.png"))  # for debugging

    # try several PSM modes — different ones win on different captcha layouts
    for psm in (7, 8, 6, 13):
        for source in (pre, Image.open(image_path)):
            cfg = f"--oem 3 --psm {psm} -c tessedit_char_whitelist=0123456789+-x*/=Xx"
            raw = pytesseract.image_to_string(source, config=cfg).strip()
            cleaned = re.sub(r"\s+", "", raw).rstrip("=")
            cleaned = cleaned.replace("x", "*").replace("X", "*").replace("o", "0").replace("O", "0")
            m = re.match(r"^(\d+)([+\-*/])(\d+)$", cleaned)
            if m:
                a, op, b = int(m.group(1)), m.group(2), int(m.group(3))
                candidates.append((raw, cleaned, a, op, b))

    if not candidates:
        return None
    # if multiple PSMs agree on the same expression, pick that; otherwise first
    from collections import Counter
    expr_counts = Counter((a, op, b) for _, _, a, op, b in candidates)
    (a, op, b), _ = expr_counts.most_common(1)[0]
    print(f"  captcha parsed: {a} {op} {b}  (candidates: {len(candidates)})")
    return {
        "+": a + b,
        "-": a - b,
        "*": a * b,
        "/": a // b if b else 0,
    }[op]


def _session_is_fresh() -> bool:
    if not STATE_FILE.exists():
        return False
    age_h = (time.time() - STATE_FILE.stat().st_mtime) / 3600
    return age_h < SESSION_MAX_AGE_HOURS


def _attempt_ocr_login(page, cfg) -> bool:
    """Fill creds + try to auto-solve the CAPTCHA. Returns True on success."""
    page.wait_for_selector("#inputEmailAddress", timeout=10000)
    page.fill("#inputEmailAddress", cfg["username"])
    page.fill("#inputChoosePassword", cfg["password"])
    for attempt in range(1, 4):
        cap_img = page.locator("app-captcha img").first
        cap_img.wait_for(state="visible", timeout=8000)
        cap_path = OUT / f"captcha_attempt{attempt}.png"
        cap_img.screenshot(path=str(cap_path))
        answer = _solve_math_captcha(cap_path)
        print(f"  OCR attempt {attempt}: answer={answer}")
        if answer is None:
            page.click("app-captcha button[title*='Refresh' i]")
            page.wait_for_timeout(700)
            continue
        page.fill("#captcha", str(answer))
        page.wait_for_timeout(250)
        page.click("button:has-text('Login')")
        page.wait_for_timeout(2500)
        if "/login" not in page.url:
            return True
        # likely wrong captcha — refresh and retry
        page.click("app-captcha button[title*='Refresh' i]")
        page.wait_for_timeout(700)
    return False


def _wait_for_manual_login(page) -> bool:
    """Pause until the user finishes login by hand (URL leaves /login).
    Returns True on success, False on timeout."""
    print("\n  >>> OCR failed. Please solve the CAPTCHA in the open browser. <<<")
    print("  The script will continue automatically once you're logged in.\n")
    for _ in range(180):  # up to 3 minutes
        page.wait_for_timeout(1000)
        if "/login" not in page.url:
            return True
    return False


def check(emirates_id: str, **_) -> dict:
    cfg = load_config(PORTAL_NAME)
    eid = format_eid(emirates_id)
    eid_digits = "".join(c for c in emirates_id if c.isdigit())

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=LAUNCH_ARGS)
        # reuse a recent session if we have one
        ctx_kwargs = {"viewport": {"width": 1366, "height": 850}}
        if _session_is_fresh():
            print(f"  reusing saved session ({STATE_FILE.name})")
            ctx_kwargs["storage_state"] = str(STATE_FILE)
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        try:
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)

            # if we landed on /login despite a "fresh" session, the session
            # was actually expired — fall through to the login flow.
            if "/login" in page.url:
                # dismiss welcome modal
                try:
                    btn = page.wait_for_selector(".ok-btn", timeout=6000, state="visible")
                    if btn:
                        btn.click()
                        page.wait_for_selector(".ok-btn", state="hidden", timeout=3000)
                except Exception:
                    pass

                if not _attempt_ocr_login(page, cfg):
                    if not _wait_for_manual_login(page):
                        return {
                            "portal": PORTAL_NAME,
                            "status": "ERROR",
                            "message": "login timed out (manual CAPTCHA never completed)",
                            "details": {},
                        }
                # save session for next run
                ctx.storage_state(path=str(STATE_FILE))
                print(f"  session saved to {STATE_FILE.name}")

            page.screenshot(path=str(OUT / "02_after_login.png"), full_page=True)
            (OUT / "02_after_login.html").write_text(page.content(), encoding="utf-8")

            body = page.locator("body").inner_text()
            return {
                "portal": PORTAL_NAME,
                "status": "UNKNOWN",
                "message": "logged in; eligibility flow not yet implemented",
                "details": {
                    "url": page.url,
                    "title": page.title(),
                    "preview": body[:600].replace("\n", " | "),
                    "screenshot": str(OUT / "02_after_login.png"),
                },
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

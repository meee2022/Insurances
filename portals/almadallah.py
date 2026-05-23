"""almadallah (provider-almadallah.axs.health) eligibility check."""
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

from . import format_eid, headless, load_config

PORTAL_NAME = "almadallah"
OUT = Path(__file__).parent.parent / "exploration" / PORTAL_NAME
OUT.mkdir(parents=True, exist_ok=True)

# realistic Chrome 120 UA so headless Chromium isn't trivially flagged as a bot
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


def _extract_details(body_text: str) -> dict:
    """Pull labelled fields out of the result page text.

    The portal renders each detail as two lines: a LABEL line followed by its
    value. We walk the lines and map the labels we care about.
    """
    labels = {
        "MEMBER NAME": "member_name",
        "PAYER": "payer",
        "MEMBER POLICY ORIGIN": "policy_origin",
        "POLICY END DATE": "policy_end",
        "CARD#": "card",
        "GATE KEEPER FACILITY?": "gate_keeper",
    }
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    out: dict = {}
    for i, line in enumerate(lines):
        if line in labels and i + 1 < len(lines):
            out[labels[line]] = lines[i + 1]

    m = re.search(r"Network:\s*(.+)", body_text)
    if m:
        out["network"] = m.group(1).strip()
    return out


def check(emirates_id: str, fob: str = "OutPatient", **_) -> dict:
    cfg = load_config(PORTAL_NAME)
    eid = format_eid(emirates_id)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless(), args=_LAUNCH_ARGS)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 850},
            user_agent=_UA,
        )
        page = ctx.new_page()
        try:
            page.goto(cfg["url"], wait_until="domcontentloaded")
            page.fill("#emailInput", cfg["username"])
            page.fill("#passwordInput", cfg["password"])
            page.click("button:has-text('Sign In')")
            page.wait_for_url("**/eligibility/**", timeout=20000)
            page.wait_for_load_state("networkidle", timeout=15000)

            page.wait_for_selector("input[name='member-eligibility'][id='Eid']", timeout=10000)
            page.click("label[for='Eid']")
            page.wait_for_selector("input[name='emirates-id']", timeout=5000)
            page.fill("input[name='emirates-id']", eid)
            page.select_option("select[name='fob']", label=fob)
            page.screenshot(path=str(OUT / "01_filled.png"), full_page=True)
            page.click("button:has-text('Submit')")

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            page.screenshot(path=str(OUT / "02_result.png"), full_page=True)
            (OUT / "02_result.html").write_text(page.content(), encoding="utf-8")

            body = page.locator("body").inner_text()
            status = "ELIGIBLE" if "ELIGIBLE" in body.upper() and "NOT" not in body.upper().split("ELIGIBLE")[0][-10:] else (
                "NOT_ELIGIBLE" if "NOT ELIGIBLE" in body.upper() or "NOT FOUND" in body.upper() else "UNKNOWN"
            )
            details = _extract_details(body) if status == "ELIGIBLE" else {}
            return {
                "portal": PORTAL_NAME,
                "status": status,
                "message": details.get("network", ""),
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
            browser.close()

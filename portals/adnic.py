"""ADNIC (online.adnic.ae/eportal) eligibility check.

Quirks:
- Three user-type segments (Customer / Partner / Employee). "Partner" (value 2)
  is the medical-provider segment.
- The visible select is hidden via CSS; the page wires it to a custom dropdown.
  We set the value via JS and dispatch a change event so the page's listeners
  pick it up, then call the inline ``loginSubmit()`` directly.
"""
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

from . import format_eid, headless, load_config

PORTAL_NAME = "adnic"
OUT = Path(__file__).parent.parent / "exploration" / PORTAL_NAME
OUT.mkdir(parents=True, exist_ok=True)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]


# the result page renders each field as "Label  :  Value" on its own row
_RESULT_LABELS = {
    "Member Name": "member_name",
    "VISA Emirate": "visa_emirate",
    "Date Of Birth": "dob",
    "Member ID": "member_id",
    "Effective Date": "effective_date",
    "Gender": "gender",
    "Expiry Date": "expiry_date",
    "Network Type": "network",
    "Emirates ID": "emirates_id",
    "Status": "status_raw",
    "Room Type": "room_type",
    "Dependency": "dependency",
    "Marital": "marital",
    "Client Name": "client",
}


def _parse_result(body_text: str) -> dict:
    """Pull labelled fields out of the eligibility result page.

    The portal renders fields across two lines:
        Member Name
         :  ZAID MOHAMMED ZAID ALSHEHHI
    so we walk the lines and treat any line that exactly matches a known
    label as a hook into "the next line's value".
    """
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    out: dict = {}
    for i, line in enumerate(lines):
        if line in _RESULT_LABELS and i + 1 < len(lines):
            val = lines[i + 1].lstrip(":").strip()
            if val and val != ":":
                out[_RESULT_LABELS[line]] = val

    # deductions block — single-line "Label : value" rows
    ded = {}
    for line in lines:
        m = re.match(
            r"^(Inpatient|Consultation|Pharmacy|Lab[^:]*|Dental|Psychiatric)\s*:\s*(.+)",
            line,
        )
        if m:
            ded[m.group(1).strip()] = m.group(2).strip()
    if ded:
        out["deductions"] = ded
    return out


def check(emirates_id: str, **_) -> dict:
    cfg = load_config(PORTAL_NAME)
    eid_digits = "".join(c for c in emirates_id if c.isdigit())  # most portals want digits

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless(), args=_LAUNCH_ARGS)
        ctx = browser.new_context(
            viewport={"width": 1366, "height": 850},
            user_agent=_UA,
        )
        page = ctx.new_page()
        try:
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#userIdEcommerce", timeout=10000)

            page.fill("#userIdEcommerce", cfg["username"])
            page.fill("#pwdEcommerce", cfg["password"])

            # set userType=2 (Partner) and fire change so any custom dropdown wrapper updates
            page.evaluate("""
                const sel = document.getElementById('userType');
                sel.value = '2';
                sel.dispatchEvent(new Event('change', { bubbles: true }));
            """)
            page.screenshot(path=str(OUT / "01_creds_filled.png"), full_page=True)

            # trigger login via the page's own handler
            page.evaluate("loginSubmit()")

            # the portal may show one of two modals after submit:
            #   (a) "User already login from other system" -> click OK to take over
            #   (b) nothing -> straight to providers.html
            # poll for up to 10s — if (a) shows, click it; otherwise carry on.
            for _ in range(20):
                page.wait_for_timeout(500)
                if not page.url.endswith("/login.html"):
                    break  # already redirected, no modal needed
                try:
                    btn = page.query_selector(
                        "button:has-text('OK'), a:has-text('OK'), input[value='OK']"
                    )
                    if btn and btn.is_visible():
                        btn.click()
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # wait specifically for providers.html (the medical-provider home)
            try:
                page.wait_for_url("**/providers.html", timeout=30000)
            except Exception:
                # didn't redirect — log what we got and bail
                print(f"  login redirect failed, still at {page.url}")
                page.screenshot(path=str(OUT / "02b_login_failed.png"), full_page=True)
                raise RuntimeError(f"login did not reach providers.html (still at {page.url})")
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3500)  # let the iframe finish loading
            page.screenshot(path=str(OUT / "02_after_login.png"), full_page=True)

            # dismiss the "Regulator License" notice modal if it appears
            for sel in ["button:has-text('OK')", ".btn-ok", "a:has-text('OK')"]:
                try:
                    btn = page.wait_for_selector(sel, timeout=2000, state="visible")
                    if btn:
                        btn.click()
                        page.wait_for_timeout(1500)
                        break
                except Exception:
                    continue

            # the provider dashboard lives inside an iframe — drill into it
            page.screenshot(path=str(OUT / "03_dashboard.png"), full_page=True)
            frame = page.frame(name="providerAdnicFrame")
            if frame is None:
                frames = [f for f in page.frames if f != page.main_frame]
                frame = frames[0] if frames else page.main_frame
            print(f"  iframe url: {frame.url}")
            (OUT / "03b_iframe.html").write_text(frame.content(), encoding="utf-8")

            # Tamer's tip: clicking OK on the regulator-license modal forces a
            # redirect to Document Management. We can short-circuit by calling
            # the slide-menu helper directly from inside the iframe. It lives
            # on the iframe's window (it's the menu the hamburger button
            # toggles), so reach into the iframe and invoke it. Note typo:
            # "Eligiblity" (missing the second 'i').
            try:
                frame.evaluate(
                    """() => {
                        if (typeof navFromSlideMenuMedPro === 'function') {
                            navFromSlideMenuMedPro('OnlineEligiblity');
                            return 'called';
                        }
                        // sometimes it's hung on parent
                        if (window.parent && typeof window.parent.navFromSlideMenuMedPro === 'function') {
                            window.parent.navFromSlideMenuMedPro('OnlineEligiblity');
                            return 'called-parent';
                        }
                        return 'not-found';
                    }"""
                )
            except Exception as e:
                print(f"  slide-menu call failed: {e}")

            page.wait_for_timeout(4000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.screenshot(path=str(OUT / "04_eligibility_page.png"), full_page=True)

            # refresh frame handle — navigation may have created a new iframe doc
            frame = page.frame(name="providerAdnicFrame") or frame

            # fill Emirates ID and search. txtTreatmntDt comes pre-populated with
            # today's date; txtProviderCode is auto-filled from session.
            frame.wait_for_selector("#txtEmiratesId", timeout=10000)
            frame.fill("#txtEmiratesId", eid_digits)
            page.screenshot(path=str(OUT / "05_eid_filled.png"), full_page=True)

            # the Search button triggers an inline handler — clicking is fine
            frame.click("#btnSearch")

            # wait for the AJAX response. The portal renders results into the
            # same page (no nav), so wait on networkidle then on a result hook.
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            page.screenshot(path=str(OUT / "06_result.png"), full_page=True)
            (OUT / "06_result_iframe.html").write_text(frame.content(), encoding="utf-8")

            body = frame.locator("body").inner_text()
            # the main page (not iframe) is where the "invalid ID" alert appears
            main_body = page.locator("body").inner_text().lower()
            details = _parse_result(body)
            raw_status = (details.get("status_raw") or "").lower()

            if "eligible" in raw_status and "not" not in raw_status:
                status = "ELIGIBLE"
            elif (
                "not eligible" in raw_status
                or "not found" in body.lower()
                or "no record" in body.lower()
                or "is invalid" in main_body          # "Given Member ID/Emirates ID is invalid"
                or "not registered" in main_body
            ):
                status = "NOT_ELIGIBLE"
                # clear partial details since the lookup failed
                details = {}
            else:
                status = "UNKNOWN"
            return {
                "portal": PORTAL_NAME,
                "status": status,
                "message": details.get("network", details.get("client", "")),
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
            page.wait_for_timeout(2000)
            browser.close()

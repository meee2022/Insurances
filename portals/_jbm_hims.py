"""Shared check() for JBM_HIMS-based portals (W Health, Lifeline).

The portals are identical templates with different hosts/credentials — same
login form, same dashboard structure, same eligibility form selectors.
"""
from pathlib import Path
from playwright.sync_api import sync_playwright

from . import format_eid, headless, load_config, LAUNCH_ARGS


def jbm_hims_check(portal_name: str, emirates_id: str, screenshot_root: Path) -> dict:
    cfg = load_config(portal_name)
    eid = format_eid(emirates_id)

    out = screenshot_root / portal_name
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless(), args=LAUNCH_ARGS)
        ctx = browser.new_context(viewport={"width": 1366, "height": 850})
        page = ctx.new_page()
        page.on("dialog", lambda d: d.accept())
        try:
            # ---- login ----
            page.goto(cfg["url"], wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#txtName", timeout=10000)
            page.wait_for_timeout(1500)
            page.fill("#txtName", cfg["username"], force=True)
            page.fill("#txtPassword", cfg["password"], force=True)
            page.screenshot(path=str(out / "01_creds.png"), full_page=True)
            # Enter from password triggers the login postback cleanly (page
            # has two #Button1 buttons so we avoid clicking by id)
            page.press("#txtPassword", "Enter")
            try:
                page.wait_for_url("**/PortalDashboard.aspx**", timeout=20000)
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            page.screenshot(path=str(out / "02_after_login.png"), full_page=True)

            # ---- navigate to the EligibilityCheckV3 page ----
            # Different JBM portals label this link differently:
            #   W Health labels it "Eligibility Check"
            #   Lifeline labels it "PA Request"
            # Both hrefs point to .../Operations/EligibilityCheckV3.aspx. The
            # link sits inside a sidebar dropdown that's expanded in W Health
            # but collapsed in Lifeline — clicking the link directly is fragile
            # because visibility depends on portal layout + headless rendering.
            # So we extract the href and navigate directly.
            href = page.locator("a[href*='EligibilityCheckV3']").first.get_attribute("href")
            if not href:
                raise RuntimeError("EligibilityCheckV3 link not found in sidebar")
            from urllib.parse import urljoin
            page.goto(urljoin(page.url, href))
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(2500)
            page.screenshot(path=str(out / "03_eligibility.png"), full_page=True)

            # ---- select Emirates ID radio + fill (dashed format), search ----
            page.evaluate("""
                const r = document.getElementById('ctl00_ContentPlaceHolder1_ubnosrchdiv_rdbUcxEmiratesID');
                r.checked = true;
                r.click();
            """)
            page.wait_for_timeout(800)
            page.fill(
                "#ctl00_ContentPlaceHolder1_ubnosrchdiv_txtUcxidno", eid, force=True
            )
            page.screenshot(path=str(out / "04_filled.png"), full_page=True)
            btn = page.locator("#ctl00_ContentPlaceHolder1_ubnosrchdiv_btnsearch")
            try:
                btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                btn.click(force=True, timeout=5000)
            except Exception:
                page.evaluate(
                    "document.getElementById('ctl00_ContentPlaceHolder1_ubnosrchdiv_btnsearch').dispatchEvent(new MouseEvent('click',{bubbles:true}))"
                )
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass
            page.wait_for_timeout(3500)
            page.screenshot(path=str(out / "05_result.png"), full_page=True)

            # ---- parse result ----
            body = page.locator("body").inner_text()
            body_lower = body.lower()
            if "not registered" in body_lower or "no record" in body_lower:
                return {
                    "portal": portal_name,
                    "status": "NOT_ELIGIBLE",
                    "message": f"ID not registered with {portal_name}",
                    "details": {},
                }
            if "format should be" in body_lower:
                return {
                    "portal": portal_name,
                    "status": "ERROR",
                    "message": "EID format rejected by portal",
                    "details": {},
                }

            def val(fid: str) -> str:
                el = page.query_selector(f"#{fid}")
                return (el.get_attribute("value") or "").strip() if el else ""

            details = {
                "customer":    val("ctl00_ContentPlaceHolder1_ubdetailsdiv1_txtUcxCustomer"),
                "tpa":         val("ctl00_ContentPlaceHolder1_ubdetailsdiv1_txtUcxTPAname"),
                "insurance":   val("ctl00_ContentPlaceHolder1_ubdetailsdiv1_txtUcxInsurcmpnyname"),
                "member_name": val("ctl00_ContentPlaceHolder1_ubdetailsdiv1_txtUcxMembername"),
                "card_id":     val("ctl00_ContentPlaceHolder1_ubdetailsdiv1_txtUcxCardid"),
                "emirates_id": val("ctl00_ContentPlaceHolder1_ubdetailsdiv1_txtUcxEID"),
                "dob":         val("ctl00_ContentPlaceHolder1_ubdetailsdiv1_txtUcxDOB"),
            }
            status = "ELIGIBLE" if details["member_name"] else "UNKNOWN"
            return {
                "portal": portal_name,
                "status": status,
                "message": details.get("insurance") or details.get("tpa") or "",
                "details": {k: v for k, v in details.items() if v},
            }
        except Exception as e:
            return {
                "portal": portal_name,
                "status": "ERROR",
                "message": str(e),
                "details": {},
            }
        finally:
            page.wait_for_timeout(1500)
            browser.close()

"""Open a Chrome window so the user can sign in to CalCentral HIMSELF (CalNet + Duo).

Run:  sh ~/code/calcentral-mcp/signin.sh          (normal sign-in)
      sh ~/code/calcentral-mcp/signin.sh --tour   (also opens Dashboard / My Academics / My Finances
                                                    and logs which /api/ feeds the pages load, URLs only)

The window uses a private Chrome profile (~/.local/share/calcentral-mcp/profile). This script never
types into any page. It waits until CalCentral reports "signed in", saves the session cookies
(chmod 600) for the MCP server, and closes the window.
"""
import json
import sys
import time

from playwright.sync_api import sync_playwright

from session import BASE, STATE_DIR, browser_lock, launch, save_cookies, status_via

TOUR = ["/dashboard", "/academics", "/finances"]
WAIT_MIN = 10


def main() -> int:
    tour = "--tour" in sys.argv
    api_calls, navs = [], []
    with browser_lock(timeout=5), sync_playwright() as pw:
        ctx = launch(pw, headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_response(resp):
            u = resp.url
            if "/api/" in u or "bcsweb" in u or "/campus_solutions" in u:
                api_calls.append({"url": u.split("#")[0], "method": resp.request.method, "status": resp.status,
                                  "type": resp.headers.get("content-type", "")[:40]})

        ctx.on("response", on_response)
        page.on("framenavigated", lambda f: navs.append(f.url.split("?")[0]) if f == page.main_frame else None)

        if status_via(ctx.request).get("isLoggedIn"):
            print("Already signed in to CalCentral.")
        else:
            page.goto(BASE + "/", wait_until="domcontentloaded")
            print(f"A Chrome window is open on CalCentral. Click Sign In and finish CalNet + Duo there.\n"
                  f"Waiting up to {WAIT_MIN} minutes...", flush=True)
            end = time.time() + WAIT_MIN * 60
            while time.time() < end:
                time.sleep(3)
                if status_via(ctx.request).get("isLoggedIn"):
                    break
            else:
                print("Timed out before sign-in finished. Nothing saved.")
                ctx.close()
                return 1
            print("Signed in.", flush=True)

        if tour:
            for path in TOUR:
                try:
                    page.goto(BASE + path, wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_load_state("networkidle", timeout=30_000)
                except Exception:
                    pass
                time.sleep(2)
        else:
            time.sleep(2)

        n = save_cookies(ctx.cookies())
        (STATE_DIR / "api_calls.json").write_text(json.dumps({"navs": navs, "api_calls": api_calls}, indent=1))
        ctx.close()
    print(f"Saved the session ({n} cookies) for the CalCentral MCP. You can close this terminal.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CalCentral session handling: the user's own sign-in, reused. Read-only.

How auth works here:
- The user signs in HIMSELF (CalNet + Duo) in a Chrome window opened by signin.py. That window uses a
  private profile at ~/.local/share/calcentral-mcp/profile, separate from their everyday Chrome.
- After sign-in, the cookies are exported to cookies.json (chmod 600). The MCP server sends plain
  HTTPS GETs with them: no browser at run time, so calls take well under a second.
- When CalCentral says the session is over, the server tries ONE silent refresh: a headless Chrome
  on the same profile reopens CalCentral. If CalNet still remembers them (single sign-on cookie),
  CalCentral hands out a new session without any password. If CalNet asks for a password or Duo,
  the server stops and returns "please sign in" (SignInRequired). Nothing here ever types,
  stores or sees a password.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

BASE = "https://calcentral.berkeley.edu"
STATE_DIR = Path.home() / ".local" / "share" / "calcentral-mcp"
PROFILE_DIR = STATE_DIR / "profile"
COOKIES = STATE_DIR / "cookies.json"
LOCK = STATE_DIR / "browser.lock"
SIGNIN_CMD = "~/code/calcentral-mcp/signin.sh"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/157.0.0.0 Safari/537.36")
COOKIE_DOMAINS = ("berkeley.edu",)


class SignInRequired(Exception):
    """CalCentral needs the user to sign in again themselves."""

    def __init__(self, why: str = "session expired"):
        super().__init__(
            f"CalCentral: please sign in ({why}). The user runs `sh {SIGNIN_CMD}` (or asks Claude to start it), "
            "then signs in with CalNet + Duo in the Chrome window that opens. Claude never types the password.")


def _ensure_dirs() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)


@contextmanager
def browser_lock(timeout: float = 120):
    """Only one Chrome may use the profile at a time (sign-in window vs. silent refresh)."""
    _ensure_dirs()
    with open(LOCK, "w") as fh:
        end = time.time() + timeout
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > end:
                    raise SignInRequired("the sign-in window is still open; finish signing in or close it")
                time.sleep(0.5)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def save_cookies(cookies: list[dict]) -> int:
    _ensure_dirs()
    keep = [c for c in cookies if any(c.get("domain", "").lstrip(".").endswith(d) for d in COOKIE_DOMAINS)]
    tmp = COOKIES.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump({"saved_at": time.time(), "cookies": keep}, fh)
    os.replace(tmp, COOKIES)
    return len(keep)


def load_jar() -> httpx.Cookies:
    jar = httpx.Cookies()
    if not COOKIES.exists():
        raise SignInRequired("no saved session yet")
    data = json.loads(COOKIES.read_text())
    for c in data.get("cookies", []):
        jar.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
    return jar


def launch(pw, headless: bool):
    """Persistent Chrome on the private profile. Uses the installed Google Chrome (no extra download)."""
    _ensure_dirs()
    return pw.chromium.launch_persistent_context(
        str(PROFILE_DIR), channel="chrome", headless=headless, viewport=None if not headless else {"width": 1280, "height": 900},
        args=["--no-first-run", "--no-default-browser-check", "--disable-features=Translate"],
    )


def status_via(request) -> dict:
    """GET /api/my/status with a Playwright APIRequestContext (shares the browser's cookies)."""
    r = request.get(BASE + "/api/my/status", headers={"Accept": "application/json"})
    try:
        return r.json()
    except Exception:
        return {}


def silent_refresh() -> bool:
    """Headless reopen of CalCentral on the saved profile. True if CalCentral is signed in afterward.

    Never fills a form. If CalNet shows its login page, that is the signal to stop.
    """
    from playwright.sync_api import sync_playwright

    with browser_lock(timeout=5), sync_playwright() as pw:
        ctx = launch(pw, headless=True)
        try:
            page = ctx.new_page()
            for url in (BASE + "/auth/cas", BASE + "/dashboard"):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                host = page.url.split("/")[2] if "://" in page.url else ""
                if host.startswith("auth.berkeley.edu") or "duosecurity" in host:
                    return False  # CalNet wants a real sign-in: that's the user's job
                if status_via(ctx.request).get("isLoggedIn"):
                    save_cookies(ctx.cookies())
                    return True
            return False
        finally:
            ctx.close()


class CalCentral:
    """Read-only HTTPS client for calcentral.berkeley.edu JSON feeds."""

    def __init__(self) -> None:
        self._client: httpx.Client | None = None
        self._checked_at = 0.0

    def _new_client(self) -> httpx.Client:
        return httpx.Client(base_url=BASE, cookies=load_jar(), timeout=30, follow_redirects=False,
                            headers={"User-Agent": UA, "Accept": "application/json"})

    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = self._new_client()
        return self._client

    def _signed_in(self) -> bool:
        r = self.client().get("/api/my/status")
        if r.status_code != 200:
            return False
        try:
            return bool(r.json().get("isLoggedIn"))
        except ValueError:
            return False

    def ensure(self) -> None:
        """Make sure the session is live (checked at most once a minute); refresh silently once if not."""
        if time.time() - self._checked_at < 60:
            return
        if self._signed_in():
            self._checked_at = time.time()
            return
        if not silent_refresh():
            raise SignInRequired("CalNet wants a fresh sign-in")
        self._client = self._new_client()
        if not self._signed_in():
            raise SignInRequired("refresh did not stick")
        self._checked_at = time.time()

    def get(self, path: str, params: dict | None = None):
        """GET a CalCentral /api/ path and return parsed JSON. GET only, by design."""
        if not path.startswith("/api/"):
            raise ValueError("only /api/ paths on calcentral.berkeley.edu")
        self.ensure()
        r = self.client().get(path, params=params)
        if r.status_code in (401, 403) or (300 <= r.status_code < 400 and "auth" in r.headers.get("location", "")):
            self._checked_at = 0
            self.ensure()
            r = self.client().get(path, params=params)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "json" not in ctype:
            raise RuntimeError(f"{path} returned {ctype or 'no content type'}, not JSON (status {r.status_code})")
        return r.json()

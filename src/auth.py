"""Drive a real installed browser via Playwright + a persistent profile dir.

The captcha on toolbox.tesla.com flags Playwright's bundled Chromium as an
automated browser. Two-part fix:

1. Launch your installed Brave / Chrome / Edge instead of bundled Chromium.
2. Use a persistent ``user_data_dir`` so once you solve the captcha and
   complete SSO, that state survives across runs.

API calls go through ``page.evaluate(async () => fetch(...))`` so they ride
the browser's real network stack (TLS, cookies, Akamai sensor). Playwright's
``ctx.request`` API does NOT — it's a Node-side HTTP client that only borrows
cookies, and the origin rejects it as a bot.
"""

from __future__ import annotations

import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from playwright.sync_api import Page, Request, Response, sync_playwright

BASE_URL = "https://toolbox.tesla.com"
# /api/v1/auth/login is finicky (frequently 400s outside of the SPA's exact
# call site). /api/v2/dashboards is a plain authed GET that's reliable.
PROBE_URLS = (
    f"{BASE_URL}/api/v2/dashboards",
    f"{BASE_URL}/api/v1/auth/login",
)
USER_PROBE = f"{BASE_URL}/api/v1/auth/login"

BROWSER_CANDIDATES = (
    ("brave", None, (
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        "/usr/bin/brave-browser", "/usr/bin/brave", "/snap/bin/brave",
    )),
    ("chrome", "chrome", (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
    )),
    ("edge", "msedge", (
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/usr/bin/microsoft-edge",
    )),
)


def detect_browser(
    prefer_channel: Optional[str] = None,
    prefer_executable: Optional[str] = None,
) -> tuple[str, Optional[str], Optional[str]]:
    if prefer_executable:
        if not Path(prefer_executable).exists():
            raise RuntimeError(f"--executable-path {prefer_executable!r} does not exist.")
        return ("custom", None, prefer_executable)
    if prefer_channel:
        return (prefer_channel, prefer_channel, None)
    for label, channel, paths in BROWSER_CANDIDATES:
        for p in paths:
            if Path(p).exists():
                return (label, None, p)
        if channel and shutil.which(channel):
            return (label, channel, None)
    return ("chromium", None, None)


# ---------------------------------------------------------------------------
# The crucial primitive: run an HTTP request inside the page via fetch().
# This is the ONLY way to get the browser's real TLS fingerprint + cookie jar.
# ---------------------------------------------------------------------------

_FETCH_JS = """
async ({url, method, headers, body}) => {
    // Use XMLHttpRequest (not fetch) to match the SPA's axios calls byte-for-byte.
    //
    // Token selection:
    //   /api/v?/datatank/* and other internal endpoints want the HS256
    //   toolbox-signed JWT (u.token, same value as the tbx_token cookie).
    //   Everything else (/api/toolbox/*, /api/v2/dashboards, etc) wants
    //   the RS256 azureAccessToken from auth.tesla.com.
    //   The error 'unexpected signing method' is the giveaway.
    let token = null;
    let tokenKind = null;
    try {
        const blob = localStorage.getItem('toolbox.user');
        if (blob) {
            const u = JSON.parse(blob);
            if (/\\/api\\/v\\d+\\/datatank/.test(url)) {
                token = u && u.token ? u.token : null;
                tokenKind = 'token(HS256)';
            } else {
                token = u && u.azureAccessToken ? u.azureAccessToken : (u && u.token ? u.token : null);
                tokenKind = u && u.azureAccessToken ? 'azure(RS256)' : 'token(HS256)';
            }
        }
    } catch (e) { /* ignore */ }

    return await new Promise((resolve) => {
        const xhr = new XMLHttpRequest();
        xhr.open(method || 'GET', url, true);
        xhr.withCredentials = true;
        xhr.setRequestHeader('Accept', 'application/json, text/plain, */*');
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        if (token) {
            xhr.setRequestHeader('Authorization', 'Bearer ' + token);
        }
        if (headers) {
            for (const k of Object.keys(headers)) {
                try { xhr.setRequestHeader(k, headers[k]); } catch (e) {}
            }
        }
        xhr.onload = () => resolve({ status: xhr.status, body: xhr.responseText, tokenKind: tokenKind });
        xhr.onerror = () => resolve({ status: 0, body: 'xhr network error', tokenKind: tokenKind });
        xhr.ontimeout = () => resolve({ status: 0, body: 'xhr timeout', tokenKind: tokenKind });
        xhr.send(body != null ? body : null);
    });
}
"""


_FETCH_MANY_JS = """
async ({urls, method}) => {
    return await Promise.all(urls.map(url => new Promise(resolve => {
        let token = null;
        try {
            const blob = localStorage.getItem('toolbox.user');
            if (blob) {
                const u = JSON.parse(blob);
                if (/\\/api\\/v\\d+\\/datatank/.test(url)) {
                    token = u && u.token ? u.token : null;
                } else {
                    token = u && u.azureAccessToken ? u.azureAccessToken : (u && u.token ? u.token : null);
                }
            }
        } catch (e) {}
        const xhr = new XMLHttpRequest();
        xhr.open(method || 'GET', url, true);
        xhr.withCredentials = true;
        xhr.setRequestHeader('Accept', 'application/json, text/plain, */*');
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);
        xhr.onload = () => resolve({ status: xhr.status, body: xhr.responseText });
        xhr.onerror = () => resolve({ status: 0, body: 'xhr network error' });
        xhr.ontimeout = () => resolve({ status: 0, body: 'xhr timeout' });
        xhr.send(null);
    })));
}
"""


def page_fetch_many(page: Page, urls: list[str], method: str = "GET") -> list[dict]:
    """Run N XHRs concurrently inside the page. One Playwright roundtrip,
    real browser-side parallelism over HTTP/2. Returns one {status, body} per URL.
    """
    relative = []
    for u in urls:
        relative.append(u[len(BASE_URL):] if u.startswith(BASE_URL) else u)
    return page.evaluate(_FETCH_MANY_JS, {"urls": relative, "method": method})


def page_fetch(page: Page, url: str, *, method: str = "GET",
               headers: Optional[dict] = None, body: Optional[str] = None) -> dict:
    """Run XHR inside the page. Returns {status, body}.

    Strips the toolbox base so the URL goes out as a relative path — that's
    what the SPA does and avoids any cross-origin treatment by Chrome.
    """
    relative = url
    if url.startswith(BASE_URL):
        relative = url[len(BASE_URL):] or "/"
    return page.evaluate(
        _FETCH_JS,
        {"url": relative, "method": method, "headers": headers, "body": body},
    )


def page_fetch_json(page: Page, url: str, **kw) -> tuple[int, Any, str]:
    """Like page_fetch but parses the body as JSON. Returns (status, json_or_None, raw)."""
    result = page_fetch(page, url, **kw)
    parsed: Any = None
    try:
        parsed = json.loads(result["body"])
    except Exception:  # noqa: BLE001
        pass
    return result["status"], parsed, result["body"]


def _ensure_on_toolbox(page: Page) -> None:
    if not page.url.startswith(BASE_URL):
        try:
            page.goto(BASE_URL, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            print(f"[auth] WARN: nav to {BASE_URL} failed ({e})", file=sys.stderr)


def is_logged_in(page: Page) -> bool:
    """Logged-in if either (a) the SPA has already made a successful /api/*
    request that we observed, or (b) one of our own probes returns 200."""
    _ensure_on_toolbox(page)
    capture: Optional[SPACapture] = getattr(page, "spa_capture", None)
    if capture is not None and capture.good_endpoints():
        return True
    for probe in PROBE_URLS:
        try:
            status, _, _ = page_fetch_json(page, probe)
        except Exception:  # noqa: BLE001
            continue
        if status == 200:
            return True
    return False


def whoami(page: Page) -> Optional[dict]:
    try:
        status, body, _ = page_fetch_json(page, USER_PROBE)
    except Exception:  # noqa: BLE001
        return None
    return body if status == 200 and isinstance(body, dict) else None


class SPACapture:
    """Listens to /api/* requests and matching responses.

    Records every request as an append-only log so SPA traffic doesn't get
    overwritten by our own probes hitting the same path.
    """

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.most_recent_auth: Optional[str] = None
        self._by_request: dict[int, dict] = {}

    def install(self, page: Page) -> None:
        def on_request(req: Request) -> None:
            try:
                url = req.url
                if "toolbox.tesla.com/api/" not in url:
                    return
                hdrs = {k.lower(): v for k, v in (req.headers or {}).items()}
                path = url.split("toolbox.tesla.com", 1)[1].split("?", 1)[0]
                evt = {
                    "method": req.method, "path": path, "url": url,
                    "headers": hdrs, "status": None,
                }
                self.events.append(evt)
                self._by_request[id(req)] = evt
                if "authorization" in hdrs:
                    self.most_recent_auth = hdrs["authorization"]
            except Exception:  # noqa: BLE001
                pass

        def on_response(resp: Response) -> None:
            try:
                req = resp.request
                evt = self._by_request.get(id(req))
                if evt is not None:
                    evt["status"] = resp.status
            except Exception:  # noqa: BLE001
                pass

        page.on("request", on_request)
        page.on("response", on_response)

    def summary(self, limit: int = 20) -> str:
        if not self.events:
            return "(no SPA /api/* requests captured)"
        lines = []
        for evt in self.events[-limit:]:
            hk = evt["headers"]
            keys = sorted(hk.keys())
            has_auth = "authorization" in hk
            has_xat = "x-azure-token" in hk
            has_origin = "origin" in hk
            status = evt["status"] if evt["status"] is not None else "?"
            lines.append(
                f"{evt['method']:4} {str(status):>3} {evt['path']}"
                f"  auth={has_auth} xat={has_xat} origin={has_origin}"
                f"  keys={keys}"
            )
        return "\n  ".join(lines)

    def good_endpoints(self) -> list[str]:
        """Paths where the SPA saw a 200 response."""
        seen: dict[str, int] = {}
        for evt in self.events:
            if evt["status"] == 200:
                seen[evt["path"]] = seen.get(evt["path"], 0) + 1
        return list(seen.keys())


def _read_azure_token(page: Page) -> Optional[str]:
    return page.evaluate(
        """() => {
            try {
                const blob = localStorage.getItem('toolbox.user');
                if (!blob) return null;
                const u = JSON.parse(blob);
                return u && u.azureAccessToken ? u.azureAccessToken : null;
            } catch (e) { return null; }
        }"""
    )


def _diagnose_failure(page: Page) -> str:
    lines = [f"page.url = {page.url}"]
    tok = _read_azure_token(page)
    lines.append(f"azureAccessToken in localStorage: {'present (len=' + str(len(tok)) + ')' if tok else 'MISSING'}")
    capture: Optional[SPACapture] = getattr(page, "spa_capture", None)
    if capture is not None:
        lines.append("SPA /api/* requests seen so far:")
        lines.append(capture.summary())
        if capture.most_recent_auth:
            lines.append(f"Most recent SPA Authorization header starts with: {capture.most_recent_auth[:32]}...")
        else:
            lines.append("No SPA request with Authorization header captured (try navigating around the toolbox UI before pressing ENTER).")
    for probe in PROBE_URLS:
        try:
            status, body, raw = page_fetch_json(page, probe)
            preview = body if body is not None else raw[:200]
            lines.append(f"GET {probe} -> {status}  body={preview!r}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"GET {probe} -> exception: {e}")
    return "\n  ".join(lines)


@contextmanager
def toolbox_session(
    profile_dir: Path,
    channel: Optional[str] = None,
    executable_path: Optional[str] = None,
    force_login: bool = False,
) -> Iterator[Page]:
    """Yield an authenticated Playwright Page backed by a real browser profile."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    label, ch, exe = detect_browser(prefer_channel=channel, prefer_executable=executable_path)

    launch_kwargs: dict = {
        "user_data_dir": str(profile_dir),
        "headless": False,
        "viewport": {"width": 1366, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if ch:
        launch_kwargs["channel"] = ch
        print(f"[auth] Using browser: {label} (channel={ch}), profile={profile_dir}")
    elif exe:
        launch_kwargs["executable_path"] = exe
        print(f"[auth] Using browser: {label} at {exe}, profile={profile_dir}")
    else:
        print(f"[auth] Using bundled Chromium, profile={profile_dir}")

    with sync_playwright() as pw:
        try:
            ctx = pw.chromium.launch_persistent_context(**launch_kwargs)
        except Exception as e:  # noqa: BLE001
            print(f"[auth] Launch of {label} failed ({e}); falling back to Chromium.", file=sys.stderr)
            launch_kwargs.pop("channel", None)
            launch_kwargs.pop("executable_path", None)
            ctx = pw.chromium.launch_persistent_context(**launch_kwargs)

        # Open our own tab first, then close anything else Brave opened at
        # startup (welcome page, new-tab page, restored session, etc).
        page = ctx.new_page()
        for other in list(ctx.pages):
            if other is page:
                continue
            try:
                other.close()
            except Exception:  # noqa: BLE001
                pass

        capture = SPACapture()
        capture.install(page)
        page.spa_capture = capture  # type: ignore[attr-defined]

        try:
            page.goto(BASE_URL, wait_until="domcontentloaded")
        except Exception as e:  # noqa: BLE001
            print(f"[auth] WARN: initial navigation issue ({e}); continuing.", file=sys.stderr)

        if is_logged_in(page) and not force_login:
            print("[auth] Existing profile session is valid — no re-login needed.")
        else:
            print()
            print("=" * 70)
            print("Please complete login in the browser window:")
            print(" 1. Sign in to Tesla SSO (solve the captcha — it will stick")
            print(f"    for future runs because the profile is saved to {profile_dir}).")
            print(" 2. Wait until the toolbox UI loads (dashboards / can_explorer).")
            print(" 3. Come back here and press ENTER.")
            print("=" * 70)
            try:
                input("[auth] Press ENTER once you're logged in... ")
            except EOFError:
                pass
            _ensure_on_toolbox(page)
            if not is_logged_in(page):
                page.wait_for_timeout(3000)
            if not is_logged_in(page):
                diag = _diagnose_failure(page)
                ctx.close()
                raise RuntimeError(
                    "Still not authenticated after login prompt.\n  "
                    + diag
                    + "\nMake sure you can see the toolbox UI before pressing ENTER."
                )

        try:
            yield page
        finally:
            ctx.close()

"""Interactive browser authentication; no password is written to disk."""

import asyncio
import time

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright
from playwright.sync_api import Error as SyncBrowserError
from playwright.sync_api import sync_playwright

from . import parsers
from .client import now
from .errors import QalamError
from .session import HOST, ORIGIN, SessionStore


def browser_read(cdp_url: str, path: str) -> tuple[str, list[dict], str]:
    """Use an already signed-in Chromium browser when Cloudflare binds verification to it.

    Called on an MCP worker thread. Only a new temporary tab is operated; existing tabs
    and the user's running browser are left open.
    """
    from .client import validate_url

    validate_url(ORIGIN + path)
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_url, timeout=10000)
            if not browser.contexts:
                raise QalamError("AUTH_REQUIRED", "Sign into Qalam in the attached browser first.")
            context = browser.contexts[0]
            page = context.new_page()
            redirect_error = []

            def route_navigation(route):
                if route.request.is_navigation_request() and route.request.frame == page.main_frame:
                    try:
                        validate_url(route.request.url)
                    except QalamError as exc:
                        redirect_error.append(exc)
                        route.abort()
                        return
                route.continue_()

            page.route("**/*", route_navigation)
            try:
                response = page.goto(ORIGIN + path, wait_until="domcontentloaded", timeout=30000)
                html = page.content()
                if parsers.page_state(html) == "challenge":
                    raise QalamError(
                        "BROWSER_LOGIN_REQUIRED",
                        "Complete verification using qalam-mcp login --browser --cdp-url "
                        "with the same endpoint as QALAM_CDP_URL, then retry.",
                    )
                if parsers.page_state(html) == "login" or "/web/login" in page.url:
                    raise QalamError(
                        "AUTH_REQUIRED", "Sign in again in the attached Qalam browser."
                    )
                if not response or response.status >= 400:
                    raise QalamError(
                        "PORTAL_ERROR", "Qalam could not load this page in the browser."
                    )
                if not parsers.is_student_page(html):
                    raise QalamError("PORTAL_CHANGED", "Could not verify the student page.")
                cookies = context.cookies(ORIGIN)
                for cookie in cookies:
                    if cookie.get("expires", -1) < 0:
                        cookie["expires"] = None
                return html, cookies, page.evaluate("navigator.userAgent")
            except SyncBrowserError:
                if redirect_error:
                    raise redirect_error[0] from None
                raise
            finally:
                page.close()
    except SyncBrowserError as exc:
        raise QalamError(
            "BROWSER_UNAVAILABLE",
            "Cannot reach the Chromium debug endpoint in QALAM_CDP_URL. Keep your browser "
            "running, or remove QALAM_CDP_URL to use the saved session over HTTPS.",
        ) from exc


async def browser_login(
    store: SessionStore, timeout: int = 300, cdp_url: str | None = None
) -> dict:
    try:
        async with async_playwright() as p:
            if cdp_url:
                browser = await p.chromium.connect_over_cdp(cdp_url)
                context = browser.contexts[0] if browser.contexts else await browser.new_context()
            else:
                browser = await p.chromium.launch(headless=False)
                context = await browser.new_context()
            page = await context.new_page()
            try:
                await page.goto(ORIGIN + "/web/login", wait_until="domcontentloaded")
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    if page.is_closed():
                        raise QalamError(
                            "LOGIN_CANCELLED", "Browser login was closed before completion."
                        )
                    url = page.url
                    if url.startswith(ORIGIN + "/student/"):
                        html = await page.content()
                        if parsers.page_state(html) == "content" and parsers.is_student_page(html):
                            cookies = await context.cookies(ORIGIN)
                            # Retain browser cookie attributes supported by the HTTP client.
                            for cookie in cookies:
                                if cookie.get("expires", -1) < 0:
                                    cookie["expires"] = None
                            with store.locked():
                                store.write(
                                    {
                                        "cookies": [
                                            c for c in cookies if c["domain"].lstrip(".") == HOST
                                        ],
                                        "user_agent": await page.evaluate("navigator.userAgent"),
                                        "username": None,
                                        "authenticated_at": now(),
                                    }
                                )
                            return {
                                "ok": True,
                                "authenticated": True,
                                "message": "Browser session saved. You can now use any agent.",
                            }
                    await asyncio.sleep(1)
                raise QalamError(
                    "LOGIN_TIMEOUT", "Browser sign-in timed out. Retry qalam-mcp login --browser."
                )
            finally:
                await page.close()
                if not cdp_url:
                    await browser.close()
    except BrowserError as exc:
        raise QalamError(
            "BROWSER_UNAVAILABLE",
            "Could not start or use Chromium. Install it with "
            "`uv run playwright install --with-deps chromium` and run browser login on a machine "
            "with a desktop. Alternatively pass --cdp-url for a browser you already launched.",
        ) from exc

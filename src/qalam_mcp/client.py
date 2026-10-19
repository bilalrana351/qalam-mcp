"""Restricted portal client. Only authentication posts and academic GETs are supported."""

import os
import re
from datetime import UTC, datetime
from http.cookiejar import Cookie
from urllib.parse import urljoin, urlsplit

import httpx

from . import parsers
from .errors import QalamError
from .session import HOST, ORIGIN, SessionStore


def now() -> str:
    return datetime.now(UTC).isoformat()


def cookie_state(cookies: httpx.Cookies) -> list[dict]:
    return [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path or "/",
            "secure": c.secure,
            "expires": c.expires,
        }
        for c in cookies.jar
        if c.domain.lstrip(".") == HOST
    ]


def restore_cookies(cookies: list[dict]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in cookies:
        if c.get("domain", "").lstrip(".") != HOST:
            continue
        jar.jar.set_cookie(
            Cookie(
                version=0,
                name=c["name"],
                value=c["value"],
                port=None,
                port_specified=False,
                domain=c["domain"],
                domain_specified=True,
                domain_initial_dot=c["domain"].startswith("."),
                path=c.get("path", "/"),
                path_specified=True,
                secure=c.get("secure", True),
                expires=c.get("expires"),
                discard=c.get("expires") is None,
                comment=None,
                comment_url=None,
                rest={},
            )
        )
    return jar


def validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != HOST
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise QalamError("UNSAFE_REDIRECT", "Qalam redirected outside its HTTPS origin; stopped.")
    if not re.fullmatch(
        r"/(?:web/login|student/(?:dashboard|results(?:/[\w-]+)*|"
        r"(?:class/schedule|timetable|schedule)(?:/[\w-]+)*|course/(?:info|attendance|gradebook)/\d+))/?",
        parsed.path,
    ):
        raise QalamError("UNSUPPORTED_ROUTE", "Qalam redirected to an unsupported portal route.")


class QalamClient:
    def __init__(
        self, store: SessionStore | None = None, transport=None, cdp_url: str | None = None
    ):
        self.store = store or SessionStore()
        self.transport = transport
        self.cdp_url = cdp_url or os.environ.get("QALAM_CDP_URL")

    def _http(self, state: dict | None = None) -> httpx.Client:
        state = state or {}
        return httpx.Client(
            cookies=restore_cookies(state.get("cookies", [])),
            headers={"User-Agent": state.get("user_agent", "QalamMCP/0.1")},
            timeout=httpx.Timeout(30),
            transport=self.transport,
            follow_redirects=False,
        )

    def _request(self, client: httpx.Client, path: str, method="GET", data=None) -> httpx.Response:
        url = urljoin(ORIGIN, path)
        for _ in range(7):
            validate_url(url)
            try:
                response = client.request(method, url, data=data)
            except httpx.HTTPError as exc:
                raise QalamError(
                    "NETWORK_ERROR", "Could not reach Qalam. Check your connection and retry."
                ) from exc
            if response.is_redirect:
                url = urljoin(str(response.url), response.headers.get("location", ""))
                if response.status_code in (301, 302, 303):
                    method, data = "GET", None
                # Never forward a password through 307/308 redirects, even within the origin.
                elif method != "GET":
                    raise QalamError(
                        "LOGIN_REDIRECT", "Unexpected login redirect. Use browser login."
                    )
                continue
            if parsers.page_state(response.text) == "challenge":
                raise QalamError(
                    "BROWSER_LOGIN_REQUIRED",
                    "Qalam requires browser verification. Run qalam-mcp login --browser on a "
                    "machine with a desktop, complete verification and sign in. Session cookies "
                    "are reused across agents on that machine. Verification may be required again.",
                )
            if response.status_code in (401, 403):
                raise QalamError(
                    "ACCESS_DENIED", "Qalam denied this request. Try signing in again."
                )
            if response.status_code >= 400:
                raise QalamError("PORTAL_ERROR", f"Qalam returned HTTP {response.status_code}.")
            return response
        raise QalamError("REDIRECT_LOOP", "Qalam redirected too many times. Try signing in again.")

    @staticmethod
    def _require_student(response: httpx.Response) -> None:
        if (
            parsers.page_state(response.text) == "login"
            or urlsplit(str(response.url)).path == "/web/login"
        ):
            raise QalamError(
                "AUTH_REQUIRED",
                "Your Qalam session expired. Run qalam-mcp login, or ask the user "
                "for credentials and call qalam_login. Passwords are not saved.",
            )
        if not parsers.is_student_page(response.text):
            raise QalamError(
                "PORTAL_CHANGED", "The response could not be verified as a student page."
            )

    def _get(self, path: str) -> str:
        with self.store.locked():
            state = self.store.load()
            if not state:
                raise QalamError(
                    "AUTH_REQUIRED",
                    "Sign in once using qalam-mcp login, or ask the user for "
                    "credentials and call qalam_login. All agents share the saved session.",
                )
            if self.cdp_url:
                from .browser_auth import browser_read

                html, cookies, agent = browser_read(self.cdp_url, path)
                state["cookies"], state["user_agent"] = cookies, agent
                self.store.write(state)
                return html
            with self._http(state) as client:
                response = self._request(client, path)
                self._require_student(response)
                state["cookies"] = cookie_state(client.cookies)
                self.store.write(state)
                return response.text

    def status(self, check_online: bool = True) -> dict:
        with self.store.locked():
            state = self.store.load()
        if not state:
            return {
                "ok": True,
                "authenticated": False,
                "reason": "No saved session.",
                "next_step": "Run qalam-mcp login, or call qalam_login with user credentials.",
            }
        result = {
            "ok": True,
            "session_saved": True,
            "username": state.get("username"),
            "authenticated_at": state.get("authenticated_at"),
        }
        if check_online:
            try:
                self._get("/student/dashboard")
                result["authenticated"] = True
            except QalamError as exc:
                if exc.code != "AUTH_REQUIRED":
                    raise
                result.update(authenticated=False, reason=str(exc))
        else:
            result.update(authenticated=None, note="Saved session exists; validity not checked.")
        return result

    def login(self, username: str, password: str) -> dict:
        if not username.strip() or not password:
            raise QalamError("INVALID_CREDENTIALS", "A username and password are required.")
        with self.store.locked():
            with self._http() as client:
                response = self._request(client, "/web/login")
                doc = parsers.soup(response.text)
                field = doc.select_one('input[name="csrf_token"]')
                if not field or not field.get("value"):
                    raise QalamError(
                        "PORTAL_CHANGED", "Login CSRF token not found. Use browser login."
                    )
                # Fixed destination keeps credentials away from page-supplied form actions.
                response = self._request(
                    client,
                    "/web/login",
                    method="POST",
                    data={
                        "csrf_token": field["value"],
                        "login": username.strip(),
                        "password": password,
                        "redirect": "/student/dashboard",
                    },
                )
                if parsers.page_state(response.text) == "login":
                    raise QalamError(
                        "LOGIN_FAILED",
                        "Qalam did not accept the login, or requires another "
                        "authentication step. Check credentials or use qalam-mcp login --browser.",
                    )
                response = self._request(client, "/student/dashboard")
                self._require_student(response)
                self.store.write(
                    {
                        "cookies": cookie_state(client.cookies),
                        "username": username.strip(),
                        "authenticated_at": now(),
                        "user_agent": client.headers["User-Agent"],
                    }
                )
        return {
            "ok": True,
            "authenticated": True,
            "username": username.strip(),
            "message": "Session saved. All agents using this state directory can reuse it.",
        }

    def list_courses(self) -> dict:
        html = self._get("/student/dashboard")
        result = parsers.courses(html)
        if not result:
            raise QalamError("NO_COURSES", "No course cards were found on your dashboard.")
        return {
            "ok": True,
            "courses": result,
            "source_url": ORIGIN + "/student/dashboard",
            "fetched_at": now(),
        }

    def _course(self, query: str) -> dict:
        candidates = self.list_courses()["courses"]
        query = query.strip()
        if not query:
            raise QalamError("COURSE_REQUIRED", "Supply a course ID, code, or course name.")
        exact = [
            c
            for c in candidates
            if query.casefold()
            in {
                c["course_id"].casefold(),
                c["name"].casefold(),
                c.get("code", "").casefold(),
            }
        ]
        matches = exact or [
            c
            for c in candidates
            if query.casefold() in (c["name"] + " " + c.get("code", "")).casefold()
        ]
        if not matches:
            raise QalamError(
                "COURSE_NOT_FOUND", "No matching dashboard course. Call qalam_list_courses."
            )
        if len(matches) > 1:
            choices = ", ".join(f"{c['course_id']}: {c['name']}" for c in matches)
            raise QalamError("COURSE_AMBIGUOUS", f"Use a course ID to select one of: {choices}")
        return matches[0]

    def attendance(self, course: str | None = None) -> dict:
        selected = [self._course(course)] if course else self.list_courses()["courses"]
        entries = []
        for item in selected:
            path = f"/student/course/attendance/{item['course_id']}"
            try:
                data = parsers.attendance(self._get(path))
                entries.append(
                    {
                        "ok": True,
                        "course": item,
                        **data,
                        "source_url": ORIGIN + path,
                        "fetched_at": now(),
                    }
                )
            except QalamError as exc:
                if course or exc.code in {
                    "AUTH_REQUIRED",
                    "BROWSER_LOGIN_REQUIRED",
                    "NETWORK_ERROR",
                }:
                    raise
                entries.append({**exc.as_dict(), "course": item})
        return {"ok": True, "courses": entries, "partial": any(not e["ok"] for e in entries)}

    def grades(self, course: str, assessment: str | None = None) -> dict:
        item = self._course(course)
        path = f"/student/course/gradebook/{item['course_id']}"
        return {
            "ok": True,
            "course": item,
            **parsers.gradebook(self._get(path), assessment),
            "source_url": ORIGIN + path,
            "fetched_at": now(),
        }

    def section(self, section: str) -> dict:
        dashboard = self._get("/student/dashboard")
        path = parsers.section_path(dashboard, section)
        return {
            "ok": True,
            **parsers.table_page(self._get(path)),
            "source_url": ORIGIN + path,
            "fetched_at": now(),
        }

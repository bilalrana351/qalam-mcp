"""The only module that knows Moodle's wire format: form encoding, error bodies, TLS, token use."""

import functools
import itertools
import json
import logging
import os
import ssl
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import certifi
import httpx

from .. import __version__
from ..errors import QalamError

DEFAULT_BASE_URL = "https://lms.nust.edu.pk"
MOBILE_SERVICE = "moodle_mobile_app"
AUTH_CODES = {"invalidtoken", "accessexception", "invalidlogin", "sitemaintenance_token"}
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
# A pluginfile auth failure is a small JSON object; a real file is never buffered past this.
ERROR_PEEK_BYTES = 4096
# httpx logs every request URL at INFO, and a signed pluginfile URL carries the token.
logging.getLogger("httpx").setLevel(logging.WARNING)
AUTH_MESSAGE = (
    "The LMS rejected the saved sign-in (token expired or revoked). Call nust_login, or have "
    "the user run qalam-mcp login, then retry. Passwords are not saved."
)


def parse_base_url(raw: str) -> str:
    """https anywhere; plain http only on loopback, so tests can point at a local mock."""
    value = raw.strip().rstrip("/")
    parsed = urlsplit(value)
    problem = None
    if parsed.username or parsed.password:
        problem = "credentials in the URL are not allowed"
    elif not parsed.hostname:
        problem = "expected https://host"
    elif parsed.scheme == "http" and parsed.hostname not in LOOPBACK:
        problem = "plain http is only allowed for 127.0.0.1 or localhost"
    elif parsed.scheme not in ("https", "http"):
        problem = "expected an https URL"
    if problem:
        raise QalamError("INVALID_BASE_URL", f"Invalid LMS_BASE_URL: {problem}.")
    return value


def base_url() -> str:
    raw = os.environ.get("LMS_BASE_URL", "").strip()
    return parse_base_url(raw) if raw else DEFAULT_BASE_URL


def serves(base: str, url: str) -> bool:
    """Whether `url` belongs to this site. The token is only ever sent to such URLs."""
    return url.startswith(base) and url[len(base) :].startswith("/")


def flatten(args: dict[str, Any]) -> list[tuple[str, str]]:
    """Moodle's flat form encoding: lists become `name[0]=..`, booleans 1/0, None is omitted."""
    pairs = []
    for name, value in args.items():
        if value is None:
            continue
        if isinstance(value, list):
            pairs.extend((f"{name}[{i}]", scalar(item)) for i, item in enumerate(value))
        else:
            pairs.append((name, scalar(value)))
    return pairs


def scalar(value: Any) -> str:
    return str(int(value)) if isinstance(value, bool) else str(value)


def json_start(text: str) -> str:
    """Strips what a NUST plugin echoes before some JSON bodies (an HTML comment, stray text)."""
    text = text.lstrip()
    while text.startswith("<!--"):
        _, found, rest = text[4:].partition("-->")
        text = rest.lstrip() if found else ""
    if text[:1] in ("[", "{", '"'):
        return text
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    return text[min(starts) :] if starts else text


def api_error(value: Any) -> QalamError | None:
    """REST exceptions and token.php/pluginfile.php errors all carry an `errorcode`."""
    if not isinstance(value, dict) or not isinstance(value.get("errorcode"), str):
        return None
    code = value["errorcode"]
    if code in AUTH_CODES:
        return QalamError("AUTH_REQUIRED", AUTH_MESSAGE, {"lms_errorcode": code})
    message = value.get("message") or value.get("error") or "no message"
    return QalamError(
        "LMS_ERROR", f"The LMS refused the request: {message} ({code}).", {"lms_errorcode": code}
    )


def decode(status: int, text: str) -> Any:
    try:
        value = json.loads(json_start(text))
    except ValueError:
        if status >= 400:
            raise QalamError("LMS_HTTP_ERROR", f"The LMS returned HTTP {status}.") from None
        raise QalamError(
            "LMS_UNAVAILABLE", "The LMS answered with something that is not JSON (maintenance?)."
        ) from None
    error = api_error(value)
    if error:
        raise error
    return value


@functools.cache
def ssl_context() -> ssl.SSLContext:
    # The server omits its intermediate certificate, so it ships next to certifi's roots.
    context = ssl.create_default_context(cafile=certifi.where())
    pem = resources.files(__package__).joinpath("sectigo_dv_r36.pem").read_text()
    context.load_verify_locations(cadata=pem)
    # The server does not support RFC 5746 secure renegotiation, which OpenSSL 3 requires by
    # default. Never renegotiating is what rustls does; certificate checks are unaffected.
    context.options |= ssl.OP_LEGACY_SERVER_CONNECT
    return context


def write_unknown(function: str, assignment_id: int) -> QalamError:
    return QalamError(
        "WRITE_OUTCOME_UNKNOWN",
        f"{function} got no usable answer, so the LMS may or may not have applied it. Call "
        f"lms_assignment_status for assignment {assignment_id} and check what is on record "
        "before retrying. It was not resent.",
        {"assignment_id": assignment_id, "function": function},
    )


class Moodle:
    """Reads go over a pooled client and are replayed once after a stale-connection failure.
    Writes go over a client without keep-alive and are sent exactly once: the server drops idle
    connections silently, and a failed write on a reused one could already have been applied."""

    def __init__(self, token: str | None = None, base: str | None = None, transport=None):
        self.base = base or base_url()
        self.token = token
        options = {
            "timeout": httpx.Timeout(30, connect=10),
            "headers": {"User-Agent": f"qalam-mcp/{__version__}"},
            "follow_redirects": False,
        }
        if transport is not None:
            options["transport"] = transport
        else:
            options["verify"] = ssl_context()
        self.pooled = httpx.Client(**options)
        self.fresh = httpx.Client(
            **options, limits=httpx.Limits(max_keepalive_connections=0, max_connections=1)
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.pooled.close()
        self.fresh.close()

    def _send(self, client: httpx.Client, path: str, replay: bool, **kwargs) -> httpx.Response:
        url = self.base + path
        try:
            return client.post(url, **kwargs)
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            if not replay:
                raise
        return client.post(url, **kwargs)

    def _read(self, path: str, form: list[tuple[str, str]]) -> Any:
        try:
            response = self._send(self.pooled, path, True, data=dict_form(form))
        except httpx.HTTPError:
            raise QalamError(
                "NETWORK_ERROR", "Could not reach the LMS. Check your connection and retry."
            ) from None
        return decode(response.status_code, response.text)

    def login(self, username: str, password: str) -> dict:
        form = [("username", username), ("password", password), ("service", MOBILE_SERVICE)]
        try:
            value = self._read("/login/token.php", form)
        except QalamError as exc:
            if exc.code == "AUTH_REQUIRED":
                raise QalamError(
                    "LOGIN_FAILED", "The LMS did not accept the username or password."
                ) from None
            raise
        token = value.get("token") if isinstance(value, dict) else None
        if not isinstance(token, str) or not token:
            raise QalamError("LMS_CHANGED", "The LMS login response has no token.")
        self.token = token
        return self.call("core_webservice_get_site_info")

    def _rest_form(self, function: str, args: dict[str, Any]) -> list[tuple[str, str]]:
        if not self.token:
            raise QalamError("AUTH_REQUIRED", AUTH_MESSAGE)
        return [
            ("wstoken", self.token),
            ("wsfunction", function),
            ("moodlewsrestformat", "json"),
            *flatten(args),
        ]

    def call(self, function: str, **args) -> Any:
        return self._read("/webservice/rest/server.php", self._rest_form(function, args))

    def write(self, function: str, assignment_id: int, **args) -> None:
        """Ok only when the server answered with an empty warnings list."""
        form = self._rest_form(function, args)
        try:
            response = self._send(
                self.fresh, "/webservice/rest/server.php", False, data=dict_form(form)
            )
            value = decode(response.status_code, response.text)
        except httpx.HTTPError:
            raise write_unknown(function, assignment_id) from None
        except QalamError as exc:
            if exc.code in ("LMS_HTTP_ERROR", "LMS_UNAVAILABLE"):
                raise write_unknown(function, assignment_id) from None
            if exc.code == "AUTH_REQUIRED":
                raise QalamError(
                    "AUTH_REQUIRED",
                    f"{function} was rejected because the LMS sign-in expired, so nothing was "
                    "written. " + AUTH_MESSAGE,
                    exc.details,
                ) from None
            raise
        if not isinstance(value, list):
            raise write_unknown(function, assignment_id)
        if value:
            warnings = [warning_text(w) for w in value]
            raise QalamError(
                "LMS_REJECTED",
                f"The LMS rejected {function}: " + "; ".join(warnings),
                {"warnings": warnings},
            )

    def upload(self, filename: str, data: bytes, itemid: int, assignment_id: int) -> dict:
        """One file into the user's draft area. itemid 0 opens a new draft; reuse the returned
        itemid for the rest so every file lands in the same draft."""
        if not self.token:
            raise QalamError("AUTH_REQUIRED", AUTH_MESSAGE)
        try:
            response = self._send(
                self.fresh,
                "/webservice/upload.php",
                False,
                data={"token": self.token, "filearea": "draft", "itemid": str(itemid)},
                files={"file_1": (filename, data, "application/octet-stream")},
            )
            value = decode(response.status_code, response.text)
        except httpx.HTTPError:
            raise write_unknown("upload", assignment_id) from None
        except QalamError as exc:
            if exc.code in ("LMS_HTTP_ERROR", "LMS_UNAVAILABLE"):
                raise write_unknown("upload", assignment_id) from None
            raise
        first = value[0] if isinstance(value, list) and value else None
        if not isinstance(first, dict) or not isinstance(first.get("itemid"), int):
            raise write_unknown("upload", assignment_id)
        return first

    def download(self, url: str, dest: Path) -> int:
        """Streams a pluginfile URL to dest atomically. Returns the bytes written."""
        if not serves(self.base, url) or not self.token:
            raise QalamError("UNSAFE_URL", "Refusing to send the LMS token outside the LMS.")
        part = dest.with_name(dest.name + ".part")
        try:
            signed = httpx.URL(url).copy_merge_params({"token": self.token})
            with self.pooled.stream("GET", signed) as response:
                if response.status_code != 200:
                    raise QalamError(
                        "LMS_HTTP_ERROR", f"The LMS returned HTTP {response.status_code}."
                    )
                chunks = response.iter_bytes()
                head = b""
                if "json" in response.headers.get("content-type", ""):
                    for chunk in chunks:
                        head += chunk
                        if len(head) >= ERROR_PEEK_BYTES:
                            break
                    try:
                        error = api_error(json.loads(head))
                    except ValueError:
                        error = None
                    if error:
                        raise error
                written = 0
                with open(part, "wb") as out:
                    for chunk in itertools.chain([head], chunks):
                        out.write(chunk)
                        written += len(chunk)
            os.replace(part, dest)
            return written
        except httpx.HTTPError:
            raise QalamError(
                "NETWORK_ERROR", "The download from the LMS failed. Check your connection."
            ) from None
        finally:
            part.unlink(missing_ok=True)


def dict_form(pairs: list[tuple[str, str]]) -> dict[str, str]:
    # Every key is unique once lists are indexed, so a dict keeps httpx's encoding simple.
    return dict(pairs)


def warning_text(warning: Any) -> str:
    if not isinstance(warning, dict):
        return str(warning)
    code, message = warning.get("warningcode") or "", warning.get("message") or ""
    if code and message:
        return f"{message} ({code})"
    return message or code or "unspecified warning"

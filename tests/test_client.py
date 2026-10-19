import json
from urllib.parse import parse_qs

import httpx
import pytest

from qalam_mcp.client import QalamClient, cookie_state, restore_cookies
from qalam_mcp.errors import QalamError

LOGIN = '<form><input name="csrf_token" value="synthetic-csrf"><input type="password"></form>'


def test_login_cookie_persistence_and_no_password(store, fixtures):
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            values = parse_qs(request.content.decode())
            assert values["csrf_token"] == ["synthetic-csrf"]
            assert values["password"] == ["fixture-password"]
            return httpx.Response(
                303,
                headers={
                    "Location": "/student/dashboard",
                    "Set-Cookie": "session_id=new-session; Secure; Path=/",
                },
            )
        if request.url.path == "/web/login":
            return httpx.Response(200, text=LOGIN)
        assert "session_id=new-session" in request.headers["cookie"]
        return httpx.Response(200, text=fixtures["dashboard"])

    client = QalamClient(store, httpx.MockTransport(handler))
    assert client.login("fixture-user", "fixture-password")["authenticated"] is True
    assert client.status()["authenticated"] is True
    contents = (store.directory / "session.json").read_text()
    assert "fixture-password" not in contents
    assert "csrf" not in contents
    assert "new-session" in contents
    assert calls.count(("POST", "/web/login")) == 1


def test_failed_login_never_saves_state(store):
    client = QalamClient(store, httpx.MockTransport(lambda r: httpx.Response(200, text=LOGIN)))
    with pytest.raises(QalamError) as caught:
        client.login("fixture-user", "bad-password")
    assert caught.value.code == "LOGIN_FAILED"
    assert store.load() is None


def test_expired_session_redirect_requires_reauthentication(client):
    def handler(request):
        if request.url.path == "/web/login":
            return httpx.Response(200, text=LOGIN)
        return httpx.Response(302, headers={"Location": "/web/login?redirect=/student/dashboard"})

    client.transport = httpx.MockTransport(handler)
    assert client.status()["authenticated"] is False
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "AUTH_REQUIRED"


def test_cloudflare_error_is_actionable_and_does_not_destroy_saved_session(client):
    client.transport = httpx.MockTransport(
        lambda r: httpx.Response(403, text="<title>Just a moment...</title><body>Cloudflare</body>")
    )
    before = client.store.load()
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "BROWSER_LOGIN_REQUIRED"
    assert client.store.load() == before


def test_external_redirect_is_stopped_before_following(client):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

    client.transport = httpx.MockTransport(handler)
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "UNSAFE_REDIRECT"
    assert len(calls) == 1


def test_missing_session_requires_login_without_network(store):
    client = QalamClient(store, httpx.MockTransport(lambda r: pytest.fail("Network called")))
    assert client.status()["authenticated"] is False
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "AUTH_REQUIRED"


def test_course_matching_and_unknown_id_are_restricted(client):
    assert (
        client.grades("CS-101", "Quiz 2")["categories"][0]["assessments"][0]["obtained_marks"] == 8
    )
    assert len(client.attendance()["courses"]) == 2
    with pytest.raises(QalamError) as caught:
        client.grades("Computing")
    assert caught.value.code == "COURSE_AMBIGUOUS"
    with pytest.raises(QalamError) as caught:
        client.grades("999999")
    assert caught.value.code == "COURSE_NOT_FOUND"
    assert client.section("results")["tables"][0]["rows"][0]["Grade"] == "A"


def test_permissions_and_expiring_cookie_roundtrip(store):
    cookies = restore_cookies(
        [
            {
                "name": "session_id",
                "value": "fixture",
                "domain": ".qalam.nust.edu.pk",
                "path": "/",
                "expires": 2000000000,
                "secure": True,
            }
        ]
    )
    assert cookie_state(cookies)[0]["expires"] == 2000000000
    store.write({"cookies": cookie_state(cookies), "password": "must-not-be-stored"})
    assert (store.directory.stat().st_mode & 0o777) == 0o700
    assert ((store.directory / "session.json").stat().st_mode & 0o777) == 0o600
    assert "password" not in json.dumps(store.load())
    store.clear()
    assert store.load() is None

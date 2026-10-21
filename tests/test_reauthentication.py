"""Exercise expiration, shared renewal, and failures without a real account."""

import json
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs

import httpx
import pytest

from qalam_mcp.client import QalamClient
from qalam_mcp.errors import QalamError
from qalam_mcp.session import QalamCredentials, SessionStore

LOGIN = '<input name="csrf_token" value="test-csrf"><input type="password">'
BLOCK = "<title>Attention Required! | Cloudflare</title><div>Sorry, you have been blocked</div>"


class Portal:
    def __init__(self, fixtures):
        self.html = fixtures["dashboard"]
        self.expired = False
        self.expiry_status = 302
        self.bad_password = False
        self.login_error = None
        self.posts = 0

    def __call__(self, request):
        if request.url.path == "/web/login":
            if self.login_error:
                return self.login_error
            if request.method == "POST":
                self.posts += 1
                assert parse_qs(request.content.decode())["password"] == ["synthetic-password"]
                if self.bad_password:
                    return httpx.Response(200, text=LOGIN)
                self.expired = False
                return httpx.Response(
                    303,
                    headers={
                        "location": "/student/dashboard",
                        "set-cookie": "session_id=renewed; Path=/",
                    },
                )
            return httpx.Response(200, text=LOGIN)
        if self.expired:
            return httpx.Response(self.expiry_status, headers={"location": "/web/login"})
        return httpx.Response(200, text=self.html)


@pytest.fixture
def portal(fixtures):
    return Portal(fixtures)


def make_client(store, portal):
    return QalamClient(store, httpx.MockTransport(portal))


def test_opt_in_survives_new_clients_and_concurrent_expiry(store, portal):
    client = make_client(store, portal)
    result = client.login("synthetic-user", "synthetic-password", remember=True)
    assert result["auto_login_enabled"] and "synthetic-password" not in json.dumps(result)
    assert "synthetic-password" not in (store.directory / "session.json").read_text()
    portal.expired = True

    def read(_):
        independent = make_client(SessionStore(store.directory), portal)
        return independent.list_courses()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(result["courses"] for result in pool.map(read, range(4)))
    assert portal.posts == 2  # initial login plus exactly one renewal across clients
    status = make_client(store, portal).status()
    assert status["authenticated"] and status["auto_login_enabled"]


def test_default_login_never_saves_password_and_does_not_auto_retry(store, portal):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password")
    assert client.credentials.read() is None
    portal.expired = True
    with pytest.raises(QalamError, match="expired"):
        client.list_courses()
    assert portal.posts == 1


def test_wrong_password_disables_auto_login(store, portal):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password", remember=True)
    portal.expired = portal.bad_password = True
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "LOGIN_FAILED"
    assert client.credentials.read() is None
    with pytest.raises(QalamError) as caught:
        make_client(store, portal).list_courses()
    assert caught.value.code == "AUTH_REQUIRED"
    assert portal.posts == 2


def test_failed_explicit_remember_does_not_keep_rejected_password(store, portal):
    portal.bad_password = True
    client = make_client(store, portal)
    with pytest.raises(QalamError) as caught:
        client.login("synthetic-user", "synthetic-password", remember=True)
    assert caught.value.code == "LOGIN_FAILED" and client.credentials.read() is None


def test_blocked_renewal_is_deferred_across_clients_without_destroying_state(store, portal):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password", remember=True)
    before = store.load()
    portal.expired = True
    portal.expiry_status = 401
    portal.login_error = httpx.Response(403, text=BLOCK)
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "PORTAL_BLOCKED"
    assert store.load() == before and client.credentials.read()
    with pytest.raises(QalamError) as caught:
        make_client(store, portal).list_courses()
    assert caught.value.code == "AUTO_LOGIN_DEFERRED" and portal.posts == 1


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(403, text=BLOCK), "PORTAL_BLOCKED"),
        (httpx.Response(403, text="Forbidden"), "ACCESS_DENIED"),
        (httpx.Response(403, text="<title>Just a moment...</title>"), "BROWSER_LOGIN_REQUIRED"),
        (httpx.Response(302, headers={"location": "https://evil.test/"}), "UNSAFE_REDIRECT"),
        (httpx.Response(303, headers={"location": "/student/qa/feedback"}), "FEEDBACK_REQUIRED"),
    ],
)
def test_access_and_portal_gates_never_trigger_password_retry(store, portal, response, code):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password", remember=True)
    before = store.load()
    client.transport = httpx.MockTransport(lambda _: response)
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == code
    assert portal.posts == 1 and store.load() == before


def test_missing_session_can_renew_and_logout_disables_it(store, portal):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password", remember=True)
    store.clear()  # simulate a missing cookie file, rather than an explicit logout
    assert make_client(store, portal).status()["authenticated"]
    assert portal.posts == 2
    client.logout()
    assert client.credentials.read() is None and store.load() is None
    assert not make_client(store, portal).status()["authenticated"]
    assert portal.posts == 2


def test_offline_status_never_renews_and_plain_login_forgets_opt_in(store, portal):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password", remember=True)
    store.clear()
    status = client.status(check_online=False)
    assert status["auto_login_enabled"] and not status["authenticated"]
    assert portal.posts == 1
    client.login("different-user", "synthetic-password")
    assert not client.status(check_online=False)["auto_login_enabled"]


def test_remember_can_be_configured_while_cloudflare_blocks_login(store, portal):
    portal.login_error = httpx.Response(403, text=BLOCK)
    client = make_client(store, portal)
    with pytest.raises(QalamError) as caught:
        client.login("synthetic-user", "synthetic-password", remember=True)
    assert caught.value.code == "PORTAL_BLOCKED"
    assert client.status(check_online=False)["auto_login_enabled"]
    assert store.load() is None and portal.posts == 0


def test_renewal_never_switches_to_a_different_saved_account(store, portal):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password", remember=True)
    with store.locked():
        client.credentials.save("different-user", "synthetic-password")
    portal.expired = True
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "AUTH_REQUIRED" and portal.posts == 1


def test_replayed_get_cannot_cause_unbounded_renewal(store, portal):
    client = make_client(store, portal)
    client.login("synthetic-user", "synthetic-password", remember=True)
    portal.expired = True
    original = client._login

    def immediately_expiring_login(username, password):
        result = original(username, password)
        portal.expired = True
        return result

    client._login = immediately_expiring_login
    with pytest.raises(QalamError) as caught:
        client.list_courses()
    assert caught.value.code == "AUTH_REQUIRED"
    with pytest.raises(QalamError) as caught:
        make_client(store, portal).list_courses()
    assert caught.value.code == "AUTO_LOGIN_DEFERRED" and portal.posts == 2


def test_private_credentials_permissions_and_symlink_rejection(store):
    credentials = QalamCredentials(store.directory)
    with store.locked():
        credentials.save("synthetic-user", "synthetic-password")
    path = store.directory / credentials.filename
    assert path.stat().st_mode & 0o777 == 0o600
    assert store.directory.stat().st_mode & 0o777 == 0o700
    path.chmod(0o644)
    with pytest.raises(QalamError) as caught:
        credentials.read()
    assert caught.value.code == "CREDENTIALS_UNSAFE"
    path.chmod(0o600)
    other = path.with_name("other.json")
    path.rename(other)
    path.symlink_to(other)
    with pytest.raises(QalamError) as caught:
        credentials.read()
    assert caught.value.code == "CREDENTIALS_UNSAFE"

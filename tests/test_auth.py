from urllib.parse import parse_qs

import httpx
import pytest
from conftest import TOKEN

from qalam_mcp import auth, cli
from qalam_mcp.client import QalamClient
from qalam_mcp.errors import QalamError
from qalam_mcp.lms.service import LmsService
from qalam_mcp.session import LmsSessionStore, SessionStore

LOGIN = '<form><input name="csrf_token" value="synthetic-csrf"><input type="password"></form>'
CHALLENGE = "<title>Just a moment...</title><body>Cloudflare</body>"


def qalam_portal(fixtures, challenge=False):
    def handler(request):
        if challenge:
            return httpx.Response(403, text=CHALLENGE)
        if request.method == "POST":
            assert parse_qs(request.content.decode())["password"] == ["fixture-password"]
            return httpx.Response(
                303,
                headers={"Location": "/student/dashboard", "Set-Cookie": "session_id=s; Path=/"},
            )
        if request.url.path == "/web/login":
            return httpx.Response(200, text=LOGIN)
        return httpx.Response(200, text=fixtures["dashboard"])

    return httpx.MockTransport(handler)


@pytest.fixture
def systems(tmp_path, lms):
    def build(qalam_transport):
        qalam = QalamClient(SessionStore(tmp_path), qalam_transport)
        service = LmsService(LmsSessionStore(tmp_path), httpx.MockTransport(lms.handler))
        return qalam, service

    return build


def test_one_login_signs_into_both_and_saves_no_password(systems, fixtures, tmp_path):
    qalam, lms = systems(qalam_portal(fixtures))
    result = auth.login(qalam, lms, "fixture-user", "fixture-password")
    assert result["ok"] is True
    assert result["qalam"]["authenticated"] and result["lms"]["authenticated"]
    for name in ("session.json", "lms-session.json"):
        assert "fixture-password" not in (tmp_path / name).read_text()
    assert TOKEN not in str(result)
    status = auth.status(qalam, lms)
    assert status["qalam"]["authenticated"] and status["lms"]["authenticated"]


def test_qalam_cloudflare_does_not_block_the_lms(systems, fixtures):
    qalam, lms = systems(qalam_portal(fixtures, challenge=True))
    result = auth.login(qalam, lms, "fixture-user", "fixture-password")
    assert result["ok"] is False
    assert result["qalam"]["error"] == "BROWSER_LOGIN_REQUIRED"
    assert result["lms"]["ok"] is True
    assert qalam.store.load() is None
    assert lms.store.load()["token"] == TOKEN


def test_lms_failure_keeps_the_qalam_login(systems, fixtures):
    qalam, lms = systems(qalam_portal(fixtures))
    result = auth.login(qalam, lms, "fixture-user", "fixture-password", lms_username="unknown-user")
    assert result["ok"] is False
    assert result["qalam"]["ok"] is True
    assert result["lms"]["error"] == "LOGIN_FAILED"
    assert qalam.store.load()["username"] == "fixture-user"
    assert lms.store.load() is None
    only = auth.login(qalam, lms, "fixture-user", "fixture-password", ["lms"])
    assert set(only) == {"ok", "lms"} and only["ok"] is True


def test_selected_systems_only_and_validation(systems):
    qalam, lms = systems(httpx.MockTransport(lambda r: pytest.fail("Qalam called")))
    assert auth.login(qalam, lms, "fixture-user", "fixture-password", ["LMS"])["ok"] is True
    with pytest.raises(QalamError) as caught:
        auth.login(qalam, lms, "u", "p", ["moodle"])
    assert caught.value.code == "INVALID_SYSTEMS"
    result = auth.logout(qalam, lms)
    assert result["ok"] and result["lms"]["authenticated"] is False
    assert lms.store.load() is None
    offline = auth.status(qalam, lms, check_online=False)
    assert offline["qalam"]["authenticated"] is False and offline["lms"]["authenticated"] is False


def test_cli_login_prompts_once_for_both(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("QALAM_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["qalam-mcp", "login", "--username", "fixture-user"])
    prompts, seen = [], {}
    monkeypatch.setattr(cli.getpass, "getpass", lambda p: prompts.append(p) or "pw")

    def fake_login(qalam, lms, username, password, systems, lms_username):
        seen.update(username=username, password=password, systems=systems)
        return {"ok": True}

    monkeypatch.setattr(cli.auth, "login", fake_login)
    cli.main()
    assert prompts == ["NUST password: "]
    assert seen == {"username": "fixture-user", "password": "pw", "systems": None}

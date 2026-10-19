import json

import pytest

from qalam_mcp.errors import QalamError
from qalam_mcp.session import LmsSessionStore, SessionStore


def test_lms_session_schema_permissions_and_no_extra_fields(tmp_path):
    store = LmsSessionStore(tmp_path / "private")
    store.save(
        {
            "token": "synthetic-token",
            "user_id": 7,
            "full_name": "Fixture Student",
            "username": "fixture-user",
            "authenticated_at": "2026-01-01T00:00:00Z",
            "password": "must-not-be-stored",
        }
    )
    path = store.directory / "lms-session.json"
    assert (store.directory.stat().st_mode & 0o777) == 0o700
    assert (path.stat().st_mode & 0o777) == 0o600
    saved = json.loads(path.read_text())
    assert set(saved) == {
        "version",
        "token",
        "user_id",
        "full_name",
        "username",
        "authenticated_at",
    }
    assert "must-not-be-stored" not in path.read_text()
    assert store.load()["token"] == "synthetic-token"
    store.clear()
    assert store.load() is None


def test_lms_and_qalam_sessions_lock_independently(tmp_path):
    qalam, lms = SessionStore(tmp_path), LmsSessionStore(tmp_path)
    with qalam.locked():
        lms.save({"token": "t", "user_id": 1})
        assert lms.load()["user_id"] == 1
    assert qalam.load() is None


def test_corrupt_lms_session_is_reported(tmp_path):
    store = LmsSessionStore(tmp_path)
    store.prepare()
    (tmp_path / "lms-session.json").write_text('{"version": 1, "token": 5}')
    with pytest.raises(QalamError) as caught:
        store.load()
    assert caught.value.code == "SESSION_INVALID"

import json
import logging

import httpx
import pytest
from conftest import TOKEN

from qalam_mcp.errors import QalamError
from qalam_mcp.lms.service import LmsService, sanitize_filename
from qalam_mcp.session import LmsSessionStore


def test_login_saves_the_token_but_never_the_password(lms, tmp_path):
    store = LmsSessionStore(tmp_path / "state")
    service = LmsService(store, httpx.MockTransport(lms.handler))
    result = service.login(" fixture-user ", "fixture-password")
    assert result["authenticated"] is True
    assert result["full_name"] == "Fixture Student"
    saved = (store.directory / "lms-session.json").read_text()
    assert TOKEN in saved
    assert "fixture-password" not in saved
    assert json.loads(saved)["user_id"] == 7
    assert TOKEN not in json.dumps(result)
    assert service.status()["authenticated"] is True


def test_failed_login_saves_nothing(lms, tmp_path):
    store = LmsSessionStore(tmp_path / "state")
    service = LmsService(store, httpx.MockTransport(lms.handler))
    with pytest.raises(QalamError) as caught:
        service.login("fixture-user", "wrong")
    assert caught.value.code == "LOGIN_FAILED"
    assert store.load() is None


def test_missing_session_requires_login_without_network(tmp_path):
    service = LmsService(
        LmsSessionStore(tmp_path), httpx.MockTransport(lambda r: pytest.fail("network"))
    )
    assert service.status()["authenticated"] is False
    with pytest.raises(QalamError) as caught:
        service.list_courses()
    assert caught.value.code == "AUTH_REQUIRED"


def test_rejected_token_is_auth_required(lms, lms_store):
    lms_store.save({"token": "stale-token", "user_id": 7})
    service = LmsService(lms_store, httpx.MockTransport(lms.handler))
    status = service.status()
    assert status["authenticated"] is False
    assert "nust_login" in status["reason"]
    with pytest.raises(QalamError) as caught:
        service.deadlines()
    assert caught.value.code == "AUTH_REQUIRED"


def test_courses_hide_hidden_and_put_favourites_first(lms_service):
    result = lms_service.list_courses()
    assert [c["id"] for c in result["courses"]] == [101, 102]
    assert result["courses"][0]["code"] == "CS-101"
    assert result["courses"][0]["title"] == "Intro to Widgets"
    assert result["source_url"] == "https://lms.nust.edu.pk/my/courses.php"


def test_deadlines_and_notifications(lms_service, lms):
    deadlines = lms_service.deadlines()
    assert deadlines["deadlines"][0]["activity"] == "Assignment 1"
    assert deadlines["deadlines"][0]["action"] == "Add submission"
    assert deadlines["deadlines"][0]["url"].endswith("view.php?id=901")
    form = lms.forms[-1]
    assert int(form["timesortto"]) - int(form["timesortfrom"]) == 104 * 86400
    assert form["limittononsuspendedevents"] == "1"
    lms_service.deadlines(include_all=True)
    assert lms.forms[-1]["timesortfrom"] == "0" and "timesortto" not in lms.forms[-1]

    notes = lms_service.notifications()
    assert notes["unread_count"] == 1
    assert notes["notifications"][0]["body"] == "Hi there,\n\nQuiz & lab"
    assert [n["id"] for n in lms_service.notifications(unread_only=True)["notifications"]] == [1]


def test_assignments_report_state_and_editability(lms_service):
    rows = {r["id"]: r for r in lms_service.assignments()["assignments"]}
    assert rows[501]["state"] == "new"
    assert rows[501]["editability"]["open"] is True
    assert rows[502]["editability"]["late"] == "1d"
    assert rows[503]["editability"]["closed_reason"] == "past_cutoff"
    assert rows[504]["state"] == "draft" and rows[504]["finalizes_on_save"] is False
    only = lms_service.assignments("CS-102")["assignments"]
    assert {r["id"] for r in only} == {503, 504}


def test_reference_resolution_and_ambiguity(lms_service):
    assert lms_service.assignment_status("Assignment 1")["id"] == 501
    assert lms_service.assignment_status("504")["name"] == "Essay"
    assert lms_service.assignment_status("cs-102/lab")["id"] == 503
    with pytest.raises(QalamError) as caught:
        lms_service.assignment_status("assignment")
    assert caught.value.code == "ASSIGNMENT_AMBIGUOUS"
    assert len(caught.value.details["candidates"]) == 2
    with pytest.raises(QalamError) as caught:
        lms_service.assignment_status("nope/Lab 1")
    assert caught.value.code == "ASSIGNMENT_NOT_FOUND"
    with pytest.raises(QalamError) as caught:
        lms_service.course_files("widget")
    assert caught.value.code == "COURSE_AMBIGUOUS"
    assert lms_service.course_files("102")["file_count"] == 0


def test_assignment_status_view(lms_service):
    view = lms_service.assignment_status("Essay")
    assert view["files"][0]["name"] == "draft.pdf"
    assert view["online_text"] == "my essay"
    assert view["statement"] == "I wrote this myself."
    assert view["source_url"] == "https://lms.nust.edu.pk/mod/assign/view.php?id=904"


def test_course_files_list_refs_but_never_urls(lms_service):
    result = lms_service.course_files("CS-101")
    modules = result["sections"][0]["modules"]
    assert [m["id"] for m in modules] == [11, 13, 14, 15, 16]
    assert modules[0]["files"][0]["ref"] == "11/slides.pdf"
    assert modules[1]["external_url"] == "https://example.org/read"
    assert len(result["sections"]) == 1
    text = json.dumps(result)
    assert "pluginfile" not in text and TOKEN not in text


def test_download_to_directory_and_default_location(lms_service, tmp_path, monkeypatch):
    target = tmp_path / "dl"
    result = lms_service.download_file("CS-101", "slides.pdf", str(target))
    assert result["path"] == str((target / "slides.pdf").resolve())
    assert (target / "slides.pdf").read_bytes() == b"%PDF-"
    again = lms_service.download_file("CS-101", "11/slides.pdf", str(target))
    assert again["skipped_existing"] is True

    monkeypatch.setenv("LMS_DOWNLOAD_DIR", str(tmp_path / "root"))
    result = lms_service.download_file("101", "slides-v2", None)
    assert result["path"] == str((tmp_path / "root" / "CS-101 W" / "slides-v2.pdf").resolve())


def test_download_refuses_traversal_and_relative_dirs(lms_service, tmp_path):
    result = lms_service.download_file("CS-101", "escape", str(tmp_path / "dl"))
    assert result["path"] == str((tmp_path / "dl" / "_.._escape.txt").resolve())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dl", "private"]
    assert sanitize_filename("..") == "_"
    with pytest.raises(QalamError) as caught:
        lms_service.download_file("CS-101", "slides.pdf", "relative/dir")
    assert caught.value.code == "DOWNLOAD_REFUSED"
    with pytest.raises(QalamError) as caught:
        lms_service.download_file("CS-101", "slides", str(tmp_path))
    assert caught.value.code == "FILE_AMBIGUOUS"


def test_downloads_never_log_the_token(lms_service, tmp_path, caplog):
    with caplog.at_level(logging.DEBUG):
        lms_service.download_file("CS-101", "slides.pdf", str(tmp_path / "dl"))
        lms_service.list_courses()
    assert caplog.records
    assert TOKEN not in caplog.text


def test_download_auth_failure_body_is_detected(lms_service, tmp_path):
    with pytest.raises(QalamError) as caught:
        lms_service.download_file("CS-101", "revoked.pdf", str(tmp_path / "dl"))
    assert caught.value.code == "AUTH_REQUIRED"
    assert list((tmp_path / "dl").iterdir()) == []

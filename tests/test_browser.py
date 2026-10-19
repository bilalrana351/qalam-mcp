"""Browser adapter tests use in-process fakes and never visit a student account."""

from unittest.mock import MagicMock

import pytest

from qalam_mcp.browser_auth import browser_read
from qalam_mcp.errors import QalamError


def attached_browser(monkeypatch, fixtures):
    playwright = MagicMock()
    monkeypatch.setattr("qalam_mcp.browser_auth.sync_playwright", lambda: playwright)
    browser = playwright.__enter__.return_value.chromium.connect_over_cdp.return_value
    context = MagicMock()
    browser.contexts = [context]
    page = context.new_page.return_value
    page.content.return_value = fixtures["attendance"]
    page.url = "https://qalam.nust.edu.pk/student/course/attendance/123"
    page.goto.return_value.status = 200
    page.evaluate.return_value = "test-user-agent"
    context.cookies.return_value = [
        {"domain": "qalam.nust.edu.pk", "expires": -1, "name": "session_id", "value": "synthetic"}
    ]
    return browser, page


def test_attached_browser_reads_temporary_tab_only(monkeypatch, fixtures):
    browser, page = attached_browser(monkeypatch, fixtures)
    html, cookies, agent = browser_read("http://127.0.0.1:9222", "/student/course/attendance/123")
    assert "Number of classes" in html
    assert cookies[0]["expires"] is None
    assert agent == "test-user-agent"
    page.close.assert_called_once()
    browser.close.assert_not_called()


def test_attached_browser_detects_expired_login(monkeypatch, fixtures):
    _, page = attached_browser(monkeypatch, fixtures)
    page.content.return_value = '<input type="password">'
    with pytest.raises(QalamError) as caught:
        browser_read("http://127.0.0.1:9222", "/student/dashboard")
    assert caught.value.code == "AUTH_REQUIRED"
    page.close.assert_called_once()

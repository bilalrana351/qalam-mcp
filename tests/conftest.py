from pathlib import Path

import httpx
import pytest

from qalam_mcp.client import QalamClient
from qalam_mcp.session import HOST, SessionStore


@pytest.fixture
def fixtures():
    directory = Path(__file__).parent / "fixtures"
    return {p.stem: p.read_text() for p in directory.glob("*.html")}


@pytest.fixture
def store(tmp_path):
    return SessionStore(tmp_path / "private")


def save_session(store):
    with store.locked():
        store.write(
            {
                "cookies": [
                    {
                        "name": "session_id",
                        "value": "synthetic-session",
                        "domain": HOST,
                        "path": "/",
                        "secure": True,
                    }
                ],
                "username": "fixture-user",
                "authenticated_at": "2026-01-01T00:00:00Z",
            }
        )


@pytest.fixture
def client(store, fixtures):
    save_session(store)

    def handler(request):
        assert request.url.host == HOST
        assert "session_id=synthetic-session" in request.headers.get("cookie", "")
        path = request.url.path
        if path == "/student/dashboard":
            html = fixtures["dashboard"]
        elif "/attendance/" in path:
            html = fixtures["attendance"]
        elif "/gradebook/" in path:
            html = fixtures["gradebook"]
        elif path in {"/student/results/", "/student/schedule"}:
            html = (
                '<a href="/student/dashboard">Dashboard</a><table><tr><th>Course</th>'
                "<th>Grade</th></tr><tr><td>CS-101</td><td>A</td></tr></table>"
            )
        else:
            return httpx.Response(404)
        return httpx.Response(200, text=html)

    return QalamClient(store, httpx.MockTransport(handler))

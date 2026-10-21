import json
import os
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from qalam_mcp.server import BearerAuth, create_server

LMS_TOOLS = {
    "lms_list_courses",
    "lms_deadlines",
    "lms_notifications",
    "lms_assignments",
    "lms_assignment_status",
    "lms_course_files",
    "lms_download_file",
    "lms_submit",
}


async def test_mcp_discovery_and_academic_calls(client, lms_service, lms, tmp_path):
    server = create_server(client, lms=lms_service)
    async with create_connected_server_and_client_session(server) as session:
        discovered = await session.list_tools()
        names = {t.name for t in discovered.tools}
        assert names == {
            "nust_auth_status",
            "nust_login",
            "nust_logout",
            "qalam_list_courses",
            "qalam_attendance",
            "qalam_grades",
            "qalam_results",
            "qalam_timetable",
            *LMS_TOOLS,
        }
        grades = await session.call_tool("qalam_grades", {"course": "123", "assessment": "Quiz 2"})
        assert not grades.isError
        assert grades.structuredContent["categories"][0]["assessments"][0]["obtained_marks"] == 8
        attendance = await session.call_tool("qalam_attendance", {"course": "123"})
        assert attendance.structuredContent["courses"][0]["combined"]["percentage"] == 75
        await session.call_tool("nust_logout", {"systems": ["qalam"]})
        status = await session.call_tool("nust_auth_status", {})
        assert status.structuredContent["qalam"]["authenticated"] is False
        assert status.structuredContent["lms"]["authenticated"] is True
        failure = await session.call_tool("qalam_list_courses", {})
        assert failure.isError
        assert "AUTH_REQUIRED" in failure.content[0].text
        courses = await session.call_tool("lms_list_courses", {})
        assert courses.structuredContent["courses"][0]["code"] == "CS-101"
        ambiguous = await session.call_tool("lms_assignment_status", {"assignment": "assign"})
        assert ambiguous.isError
        assert "ASSIGNMENT_AMBIGUOUS" in ambiguous.content[0].text
        tools = {t.name: t for t in discovered.tools}
        assert tools["lms_course_files"].annotations.readOnlyHint is True
        assert tools["lms_download_file"].annotations.readOnlyHint is False
        assert tools["lms_submit"].annotations.destructiveHint is True
        report = tmp_path / "report.pdf"
        report.write_bytes(b"synthetic")
        dry = await session.call_tool("lms_submit", {"assignment": "501", "files": [str(report)]})
        assert dry.structuredContent["outcome"] == "dry_run"
        assert dry.structuredContent["plan"]["finalizes"] is True
        assert "upload" not in lms.calls


async def test_real_stdio_subprocess_and_protocol_output(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qalam_mcp", "serve"],
        env={**os.environ, "QALAM_STATE_DIR": str(tmp_path / "state")},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 16
            status = await session.call_tool("nust_auth_status", {"check_online": False})
            assert not status.isError
            assert status.structuredContent["qalam"]["authenticated"] is False
            assert status.structuredContent["lms"]["authenticated"] is False


async def test_http_bearer_gate_and_initialize(client):
    server = create_server(client)
    token = "test-access-token-" + "a" * 40
    inner = server.streamable_http_app()
    app = BearerAuth(inner, token)
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765"
        ) as http:
            assert (await http.post("/mcp", json={})).status_code == 401
            assert (
                await http.post("/mcp", headers={"Authorization": "Bearer bad"}, json={})
            ).status_code == 401
            response = await http.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer " + token,
                    "Accept": "application/json, text/event-stream",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
            )
            assert response.status_code == 200
            assert json.loads(response.text)["result"]["serverInfo"]["name"] == "NUST Qalam + LMS"
            blocked = await http.post(
                "http://evil.test/mcp",
                headers={
                    "Authorization": "Bearer " + token,
                },
                json={},
            )
            assert blocked.status_code == 421


def test_http_rejects_short_token():
    with pytest.raises(ValueError, match="32"):
        BearerAuth(None, "short")


async def test_mcp_remember_is_explicit_and_logout_forgets_credentials(
    store, fixtures, lms_service
):
    from test_reauthentication import Portal

    from qalam_mcp.client import QalamClient

    portal = Portal(fixtures)
    client = QalamClient(store, httpx.MockTransport(portal))
    server = create_server(client, lms=lms_service)
    async with create_connected_server_and_client_session(server) as session:
        result = await session.call_tool(
            "nust_login",
            {
                "username": "synthetic-user",
                "password": "synthetic-password",
                "systems": ["qalam"],
                "remember_qalam": True,
            },
        )
        assert not result.isError
        assert result.structuredContent["qalam"]["auto_login_enabled"]
        assert "synthetic-password" not in str(result)
        portal.expired = True
        courses = await session.call_tool("qalam_list_courses", {})
        assert not courses.isError and portal.posts == 2
        await session.call_tool("nust_logout", {"systems": ["qalam"]})
        assert client.credentials.read() is None and store.load() is None

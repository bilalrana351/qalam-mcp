import json
import os
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_connected_server_and_client_session

from qalam_mcp.server import BearerAuth, create_server


async def test_mcp_discovery_and_academic_calls(client):
    server = create_server(client)
    async with create_connected_server_and_client_session(server) as session:
        discovered = await session.list_tools()
        names = {t.name for t in discovered.tools}
        assert names == {
            "qalam_auth_status",
            "qalam_login",
            "qalam_logout",
            "qalam_list_courses",
            "qalam_attendance",
            "qalam_grades",
            "qalam_results",
            "qalam_timetable",
        }
        grades = await session.call_tool("qalam_grades", {"course": "123", "assessment": "Quiz 2"})
        assert not grades.isError
        assert grades.structuredContent["categories"][0]["assessments"][0]["obtained_marks"] == 8
        attendance = await session.call_tool("qalam_attendance", {"course": "123"})
        assert attendance.structuredContent["courses"][0]["combined"]["percentage"] == 75
        await session.call_tool("qalam_logout", {})
        status = await session.call_tool("qalam_auth_status", {})
        assert status.structuredContent["authenticated"] is False
        failure = await session.call_tool("qalam_list_courses", {})
        assert failure.isError
        assert "AUTH_REQUIRED" in failure.content[0].text


async def test_real_stdio_subprocess_and_protocol_output(tmp_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "qalam_mcp", "serve"],
        env={**os.environ, "QALAM_STATE_DIR": str(tmp_path / "state")},
    )
    async with stdio_client(params) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            assert len((await session.list_tools()).tools) == 8
            status = await session.call_tool("qalam_auth_status", {"check_online": False})
            assert not status.isError
            assert status.structuredContent["authenticated"] is False


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
            assert json.loads(response.text)["result"]["serverInfo"]["name"] == "Qalam"
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

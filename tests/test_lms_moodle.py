import ssl
from urllib.parse import parse_qsl

import httpx
import pytest

from qalam_mcp.errors import QalamError
from qalam_mcp.lms import moodle
from qalam_mcp.lms.moodle import Moodle

BASE = "https://lms.nust.edu.pk"


def test_base_url_overrides():
    assert moodle.parse_base_url("https://lms.example.edu/") == "https://lms.example.edu"
    for url in ["http://127.0.0.1:8080", "http://localhost:9", "http://[::1]:3000/"]:
        assert moodle.parse_base_url(url)
    for url in [
        "http://lms.example.edu",
        "http://127.0.0.1.evil.test",
        "http://169.254.169.254",
        "ftp://127.0.0.1",
        "lms.example.edu",
        "https://user:pw@host",
        "https://",
    ]:
        with pytest.raises(QalamError) as caught:
            moodle.parse_base_url(url)
        assert caught.value.code == "INVALID_BASE_URL", url


def test_token_only_goes_to_the_site():
    assert moodle.serves(BASE, BASE + "/webservice/pluginfile.php/1/f.pdf")
    assert not moodle.serves(BASE, "https://lms.nust.edu.pk.evil.test/f.pdf")
    assert not moodle.serves(BASE, "http://lms.nust.edu.pk/f.pdf")
    assert not moodle.serves(BASE, BASE)


def test_flat_form_encoding():
    assert moodle.flatten({"userid": 5, "flag": True, "ids": [3, 9], "skip": None}) == [
        ("userid", "5"),
        ("flag", "1"),
        ("ids[0]", "3"),
        ("ids[1]", "9"),
    ]


def test_json_start_strips_plugin_noise():
    assert moodle.json_start('<!-- mod_enhancedlabel [x] -->\n{"a":1}') == '{"a":1}'
    assert moodle.json_start("<!-- a --><!-- b -->[1]") == "[1]"
    assert moodle.json_start("notice: oops\n[1,2]") == "[1,2]"
    assert moodle.json_start("null") == "null"


@pytest.mark.parametrize(
    "code", ["invalidtoken", "accessexception", "invalidlogin", "sitemaintenance_token"]
)
def test_auth_errorcodes_require_login(code):
    with pytest.raises(QalamError) as caught:
        moodle.decode(200, f'{{"exception":"x","errorcode":"{code}","message":"No"}}')
    assert caught.value.code == "AUTH_REQUIRED"
    assert "nust_login" in str(caught.value)


def test_other_errors_and_bodies():
    with pytest.raises(QalamError) as caught:
        moodle.decode(200, '{"exception":"x","errorcode":"nopermissions","message":"No"}')
    assert caught.value.code == "LMS_ERROR"
    assert caught.value.details == {"lms_errorcode": "nopermissions"}
    assert moodle.decode(200, '{"warnings":[]}') == {"warnings": []}
    with pytest.raises(QalamError) as caught:
        moodle.decode(503, "<html>down</html>")
    assert caught.value.code == "LMS_HTTP_ERROR"
    with pytest.raises(QalamError) as caught:
        moodle.decode(200, "<html>down</html>")
    assert caught.value.code == "LMS_UNAVAILABLE"


def test_tls_context_trusts_the_shipped_intermediate():
    subjects = [dict(x[0] for x in c["subject"]) for c in moodle.ssl_context().get_ca_certs()]
    assert any("Sectigo" in s.get("organizationName", "") for s in subjects)
    assert moodle.ssl_context().verify_mode.name == "CERT_REQUIRED"
    assert moodle.ssl_context().check_hostname is True
    assert moodle.ssl_context().options & ssl.OP_LEGACY_SERVER_CONNECT


def test_reads_send_the_token_strip_noise_and_replay_once_on_a_stale_connection():
    calls = []

    def handler(request):
        calls.append(dict(parse_qsl(request.content.decode())))
        if len(calls) == 1:
            raise httpx.RemoteProtocolError("connection closed", request=request)
        return httpx.Response(200, text='<!-- plugin -->\n[{"id": 1}]')

    with Moodle("t0k", BASE, httpx.MockTransport(handler)) as client:
        assert client.call("core_enrol_get_users_courses", userid=7) == [{"id": 1}]
    assert len(calls) == 2
    assert calls[1] == {
        "wstoken": "t0k",
        "wsfunction": "core_enrol_get_users_courses",
        "moodlewsrestformat": "json",
        "userid": "7",
    }


@pytest.mark.parametrize(
    "response",
    [
        httpx.ReadError("reset"),
        httpx.Response(502, text="<html>bad gateway</html>"),
        httpx.Response(200, text='{"unexpected": true}'),
    ],
)
def test_failed_write_is_unknown_and_never_retried(response):
    calls = []

    def handler(request):
        calls.append(request)
        if isinstance(response, Exception):
            raise response
        return response

    with Moodle("t0k", BASE, httpx.MockTransport(handler)) as client:
        with pytest.raises(QalamError) as caught:
            client.write("mod_assign_save_submission", 42, assignmentid=42)
    assert caught.value.code == "WRITE_OUTCOME_UNKNOWN"
    assert "lms_assignment_status" in str(caught.value)
    assert len(calls) == 1


def test_write_warnings_are_a_rejection_and_auth_failure_wrote_nothing():
    def handler(request):
        return httpx.Response(200, json=[{"warningcode": "couldnotsave", "message": "Locked"}])

    with Moodle("t0k", BASE, httpx.MockTransport(handler)) as client:
        with pytest.raises(QalamError) as caught:
            client.write("mod_assign_save_submission", 42)
    assert caught.value.code == "LMS_REJECTED"
    assert caught.value.details["warnings"] == ["Locked (couldnotsave)"]

    expired = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"errorcode": "invalidtoken", "message": "x"})
    )
    with Moodle("t0k", BASE, expired) as client:
        with pytest.raises(QalamError) as caught:
            client.write("mod_assign_save_submission", 42)
    assert caught.value.code == "AUTH_REQUIRED"
    assert "nothing was written" in str(caught.value)


def test_download_detects_the_json_auth_body_and_leaves_no_file(tmp_path):
    body = b'{"error":"Invalid token","errorcode":"invalidtoken"}'

    def handler(request):
        assert request.url.params["token"] == "t0k"
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    dest = tmp_path / "a.pdf"
    with Moodle("t0k", BASE, httpx.MockTransport(handler)) as client:
        with pytest.raises(QalamError) as caught:
            client.download(BASE + "/webservice/pluginfile.php/1/a.pdf", dest)
    assert caught.value.code == "AUTH_REQUIRED"
    assert list(tmp_path.iterdir()) == []


def test_download_writes_atomically_and_refuses_foreign_urls(tmp_path):
    payload = b'{"a JSON file": "that is real data"}'

    def handler(request):
        return httpx.Response(200, content=payload, headers={"content-type": "application/json"})

    with Moodle("t0k", BASE, httpx.MockTransport(handler)) as client:
        url = BASE + "/webservice/pluginfile.php/1/a.json"
        assert client.download(url, tmp_path / "a") == len(payload)
        with pytest.raises(QalamError) as caught:
            client.download("https://evil.test/webservice/pluginfile.php/1/a", tmp_path / "b")
    assert caught.value.code == "UNSAFE_URL"
    assert (tmp_path / "a").read_bytes() == payload
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a"]


def test_login_maps_bad_credentials():
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, json={"error": "Invalid login", "errorcode": "invalidlogin"})
    )
    with Moodle(None, BASE, transport) as client:
        with pytest.raises(QalamError) as caught:
            client.login("u", "p")
    assert caught.value.code == "LOGIN_FAILED"

import os

import httpx
import pytest
from conftest import DAY, NOW, plugin_configs

from qalam_mcp.errors import QalamError
from qalam_mcp.lms.service import LmsService

WRITES = {"upload", "mod_assign_save_submission", "mod_assign_submit_for_grading"}


def local(tmp_path, name, size=10, mtime=NOW - DAY):
    path = tmp_path / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return str(path)


def writes(lms):
    return [c for c in lms.calls if c in WRITES]


def refused(service, *args, **kwargs):
    with pytest.raises(QalamError) as caught:
        service.submit(*args, **kwargs)
    return caught.value


def test_dry_run_makes_zero_writes_and_returns_the_plan(lms_service, lms, tmp_path):
    a, b = local(tmp_path, "report.pdf"), local(tmp_path, "code.txt", 20)
    result = lms_service.submit("Assignment 1", [a, b])
    assert result["outcome"] == "dry_run"
    assert len(result["plan_id"]) == 32
    plan = result["plan"]
    assert [f["name"] for f in plan["files"]] == ["report.pdf", "code.txt"]
    assert plan["files"][0]["path"] == a and len(plan["files"][0]["sha256"]) == 64
    assert plan["finalization"] == "on_save" and plan["finalizes"] is True
    assert plan["statement"] == "I wrote this myself."
    assert plan["late"] is False and plan["replaces"] == []
    assert any("FINALIZES immediately" in line for line in plan["summary"])
    assert writes(lms) == []
    assert lms_service.submit("Assignment 1", [a, b])["plan_id"] == result["plan_id"]


def test_plan_id_mismatch_and_changed_file_write_nothing(lms_service, lms, tmp_path):
    a = local(tmp_path, "report.pdf")
    plan_id = lms_service.submit("501", [a])["plan_id"]
    error = refused(lms_service, "501", [a], plan_id="0" * 32, accept_statement=True)
    assert error.code == "PLAN_CHANGED"
    local(tmp_path, "report.pdf", size=11)
    error = refused(lms_service, "501", [a], plan_id=plan_id, accept_statement=True)
    assert error.code == "PLAN_CHANGED"
    error = refused(lms_service, "501", [a], finalize=True, plan_id=plan_id)
    assert error.code == "PLAN_CHANGED"
    assert writes(lms) == []


def test_statement_must_be_accepted(lms_service, lms, tmp_path):
    a = local(tmp_path, "report.pdf")
    plan_id = lms_service.submit("501", [a])["plan_id"]
    error = refused(lms_service, "501", [a], plan_id=plan_id)
    assert error.code == "STATEMENT_NOT_ACCEPTED"
    assert error.details["statement"] == "I wrote this myself."
    assert writes(lms) == []


def test_late_is_shown_and_turning_late_changes_the_plan_id(lms, lms_store, tmp_path):
    clock = [NOW]
    service = LmsService(lms_store, httpx.MockTransport(lms.handler), clock=lambda: clock[0])
    a = local(tmp_path, "new.pdf", 5)
    plan = service.submit("Assignment 10", [a])["plan"]
    assert plan["late"] is True and plan["late_by"] == "1d"
    assert any(line.startswith("LATE by 1d") for line in plan["summary"])
    assert plan["replaces"] == [{"name": "old.pdf", "size": 10, "fate": "removed"}]

    due = NOW + 3 * DAY
    clock[0] = due - 60
    on_time = service.submit("501", [a])["plan_id"]
    clock[0] = due - 30
    assert service.submit("501", [a])["plan_id"] == on_time
    clock[0] = due + 30
    assert service.submit("501", [a])["plan_id"] != on_time


def test_closed_and_local_refusals(lms_service, lms, tmp_path):
    ok = local(tmp_path, "ok.pdf")
    (tmp_path / "other").mkdir()
    cases = [
        (("Lab 1", [str(tmp_path / "missing.pdf")]), "past_cutoff"),
        (("501", [str(tmp_path / "missing.pdf")]), "file_missing"),
        (("501", ["relative.pdf"]), "path_not_absolute"),
        (("501", [str(tmp_path)]), "not_a_file"),
        (("501", []), "nothing_to_submit"),
        (("501", [local(tmp_path, "empty.pdf", 0)]), "empty_file"),
        (("501", [ok, local(tmp_path / "other", "ok.pdf")]), "duplicate_name"),
        (("501", [local(tmp_path, "big.pdf", 1025)]), "file_too_large"),
        (("501", [local(tmp_path, f"f{i}.pdf") for i in range(4)]), "too_many_files"),
        (("501", [local(tmp_path, "a:b.pdf")]), "name_not_allowed"),
    ]
    for args, reason in cases:
        error = refused(lms_service, *args)
        assert (error.code, error.details["reason"]) == ("SUBMIT_REFUSED", reason), args
        assert "nothing was submitted" in str(error)
    assert refused(lms_service, "501", [ok], text="hi").details["reason"] == "text_not_accepted"
    lms.assignments[501] = (101, lms.assignments[501][1], plugin_configs(types=".pdf; .docx"))
    error = refused(lms_service, "501", [local(tmp_path, "notes.txt")])
    assert error.details["reason"] == "type_not_allowed"
    lms.statuses[501]["lastattempt"]["locked"] = True
    assert refused(lms_service, "501", [ok]).details["reason"] == "locked"
    assert writes(lms) == []


def test_already_up_to_date_sends_nothing_even_with_a_plan_id(lms_service, lms, tmp_path):
    same = local(tmp_path, "old.pdf", 10, mtime=NOW - 3 * DAY)
    result = lms_service.submit("502", [same])
    assert result["outcome"] == "up_to_date"
    assert "plan_id" not in result
    result = lms_service.submit("502", [same], plan_id="0" * 32, accept_statement=True)
    assert result["outcome"] == "up_to_date"
    edited = local(tmp_path, "old.pdf", 10, mtime=NOW - DAY)
    assert lms_service.submit("502", [edited])["outcome"] == "dry_run"
    assert writes(lms) == []


def test_happy_path_uploads_saves_and_verifies(lms_service, lms, tmp_path):
    files = [local(tmp_path, "report.pdf", 12), local(tmp_path, "code.txt", 20)]
    plan_id = lms_service.submit("501", files)["plan_id"]
    result = lms_service.submit("501", files, plan_id=plan_id, accept_statement=True)
    assert result["outcome"] == "submitted"
    assert result["server"]["state"] == "submitted"
    assert sorted(f["name"] for f in result["server"]["files"]) == ["code.txt", "report.pdf"]
    tail = lms.calls[lms.calls.index("upload") :]
    assert tail == [
        "upload",
        "upload",
        "mod_assign_save_submission",
        "mod_assign_get_submission_status",
    ]
    save = next(f for f in lms.forms if f["wsfunction"] == "mod_assign_save_submission")
    assert save["plugindata[files_filemanager]"] == "1000"
    assert not any(k.startswith("plugindata[onlinetext") for k in save)
    assert lms_service.submit("501", files)["outcome"] == "up_to_date"


def test_verification_mismatch_is_reported(lms_service, lms, tmp_path):
    files = [local(tmp_path, "report.pdf", 12), local(tmp_path, "code.txt", 20)]
    plan_id = lms_service.submit("501", files)["plan_id"]
    lms.save_effect = lambda sub: sub["plugins"][0]["fileareas"][0]["files"].pop()
    error = refused(lms_service, "501", files, plan_id=plan_id, accept_statement=True)
    assert error.code == "VERIFICATION_FAILED"
    assert error.details["differences"] == ["code.txt (20 bytes) is not on the server"]


@pytest.mark.parametrize(
    "failure", [httpx.ReadError("reset"), httpx.Response(504, text="<html>timeout</html>")]
)
def test_write_failure_is_unknown_and_not_retried(lms_service, lms, tmp_path, failure):
    a = local(tmp_path, "report.pdf")
    plan_id = lms_service.submit("501", [a])["plan_id"]
    lms.fail["mod_assign_save_submission"] = failure
    error = refused(lms_service, "501", [a], plan_id=plan_id, accept_statement=True)
    assert error.code == "WRITE_OUTCOME_UNKNOWN"
    assert "lms_assignment_status" in str(error)
    assert writes(lms) == ["upload", "mod_assign_save_submission"]


def test_upload_failure_stops_before_the_save(lms_service, lms, tmp_path):
    a = local(tmp_path, "report.pdf")
    plan_id = lms_service.submit("501", [a])["plan_id"]
    lms.fail["upload"] = httpx.ConnectError("refused")
    error = refused(lms_service, "501", [a], plan_id=plan_id, accept_statement=True)
    assert error.code == "WRITE_OUTCOME_UNKNOWN"
    assert writes(lms) == ["upload"]


def test_files_only_save_resends_held_text_and_drafts_finalize_on_request(
    lms_service, lms, tmp_path
):
    a = local(tmp_path, "essay.pdf")
    dry = lms_service.submit("Essay", [a])
    plan = dry["plan"]
    assert plan["finalization"] == "draft_only" and plan["statement"] is None
    assert plan["text_on_record"] == {"text": "my essay", "fate": "kept"}
    assert plan["replaces"] == [{"name": "draft.pdf", "size": 8, "fate": "removed"}]
    result = lms_service.submit("Essay", [a], plan_id=dry["plan_id"])
    assert result["server"]["state"] == "draft"
    assert result["server"]["online_text"] == "my essay"
    save = [f for f in lms.forms if f["wsfunction"] == "mod_assign_save_submission"][-1]
    assert save["plugindata[onlinetext_editor][text]"] == "<p>my  essay</p>"
    assert save["plugindata[onlinetext_editor][format]"] == "1"
    assert save["plugindata[onlinetext_editor][itemid]"] == "0"

    b = local(tmp_path, "final.pdf")
    dry = lms_service.submit("Essay", [b], text="a < b", finalize=True)
    assert dry["plan"]["finalization"] == "submit_for_grading"
    assert dry["plan"]["text_on_record"]["fate"] == "replaced"
    error = refused(lms_service, "Essay", [b], text="a < b", finalize=True, plan_id=dry["plan_id"])
    assert error.code == "STATEMENT_NOT_ACCEPTED"
    result = lms_service.submit(
        "Essay", [b], text="a < b", finalize=True, plan_id=dry["plan_id"], accept_statement=True
    )
    assert result["server"]["state"] == "submitted"
    assert result["server"]["online_text"] == "a < b"
    grading = next(f for f in lms.forms if f["wsfunction"] == "mod_assign_submit_for_grading")
    assert grading["acceptsubmissionstatement"] == "1"


def test_held_text_with_embedded_files_refuses_a_files_only_save(lms_service, lms, tmp_path):
    html = '<p>see <img src="https://lms.nust.edu.pk/webservice/pluginfile.php/1/x.png"></p>'
    lms.statuses[504]["lastattempt"]["submission"]["plugins"][1]["editorfields"][0]["text"] = html
    a = local(tmp_path, "essay.pdf")
    assert refused(lms_service, "Essay", [a]).details["reason"] == "text_embeds_files"
    assert lms_service.submit("Essay", [a], text="fresh")["outcome"] == "dry_run"
    assert writes(lms) == []

from qalam_mcp.lms import models
from qalam_mcp.lms.models import (
    Assignment,
    Closed,
    ClosedReason,
    Open,
    Section,
    SubmissionState,
    SubmissionStatus,
    editability,
)

NOW = 1_800_000_000


def test_course_names():
    parse = models.parse_course_name
    assert parse("CS-101-Intro to Widgets - 2K24-ABC-1") == ("CS-101", "Intro to Widgets")
    assert parse("HU-212-Technical Writing AB 2K25-XYZ-3") == ("HU-212", "Technical Writing")
    assert parse("MATH-101-Calculus I") == ("MATH-101", "Calculus I")
    assert parse("Orientation 2026") == ("", "Orientation 2026")
    assert parse("C-1-Foo") == ("", "C-1-Foo")
    assert parse("ABCDEF-123-Too Long") == ("", "ABCDEF-123-Too Long")


def test_html_to_text():
    html = "<p>Hi <strong>there</strong>,</p>\n<p>Lab &amp; Quiz &#8211; due&nbsp;soon</p>"
    assert models.html_to_text(html) == "Hi there,\n\nLab & Quiz – due soon"
    assert models.html_to_text("a<style>p{x:y}</style><script>alert(1)</script>b") == "ab"
    assert models.decode_entities("a &bogus; b & c") == "a &bogus; b & c"
    assert models.html_to_text("text <b unclosed") == "text"
    rendered = '<div class="text_to_html">a &lt; b<br />\n<br />\nand&nbsp; more</div>'
    assert models.rendered_words(rendered) == models.plain_words("a < b\n\nand  more")


def test_sizes_and_spans():
    assert models.human_size(12) == "12 B"
    assert models.human_size(616_666) == "602 KB"
    assert models.human_size(52_428_800) == "50.0 MB"
    assert models.span(2 * 86400 + 4 * 3600 + 59) == "2d 4h"
    assert models.span(86400) == "1d"
    assert models.span(59) == "<1m"


def test_sections_keep_visible_file_modules_and_links():
    sections = [
        Section.parse(raw)
        for raw in [
            {
                "id": 1,
                "name": "General",
                "modules": [
                    {
                        "id": 10,
                        "name": "Syllabus",
                        "modname": "resource",
                        "contents": [
                            {
                                "type": "file",
                                "filename": "syllabus.pdf",
                                "filesize": 2048,
                                "fileurl": "https://x/webservice/pluginfile.php/1/syllabus.pdf",
                                "timemodified": 0,
                            }
                        ],
                    },
                    {"id": 11, "name": "Spacer", "modname": "label"},
                    {
                        "id": 12,
                        "name": "Reading",
                        "modname": "url",
                        "contents": [{"type": "url", "fileurl": "https://example.org/list"}],
                    },
                    {"id": 13, "name": "Hidden", "modname": "resource", "uservisible": False},
                ],
            },
            {"id": 2, "name": "Secret", "visible": 0, "modules": [{"id": 20, "modname": "x"}]},
        ]
    ]
    assert [m.id for m in sections[0].modules] == [10, 12]
    assert sections[0].modules[0].files[0].modified is None
    assert sections[0].modules[1].external_url == "https://example.org/list"
    assert sections[1].modules == []


def assignment(**fields):
    raw = {"id": 1, "cmid": 2, "course": 3, "duedate": NOW + 100, **fields}
    return Assignment.parse(raw)


def status(sub=None, **flags):
    attempt = {"submissionsenabled": 1, "locked": 0, "canedit": 1, **flags}
    if sub:
        attempt["submission"] = sub
    return SubmissionStatus.parse({"lastattempt": attempt})


def test_missing_submission_is_new_and_file_limits_parse():
    assert status().state is SubmissionState.NEW
    assert SubmissionStatus.parse({"warnings": []}).state is SubmissionState.NEW
    a = assignment(
        configs=[
            {"plugin": "file", "subtype": "assignsubmission", "name": "enabled", "value": "1"},
            {
                "plugin": "file",
                "subtype": "assignsubmission",
                "name": "filetypeslist",
                "value": ".pdf; .docx",
            },
        ]
    )
    assert a.file_limits.allowed_types == [".pdf", ".docx"]
    assert a.file_limits.max_files == 0
    assert assignment().file_limits is None
    assert not assignment().submission_drafts


def test_editability():
    open_now = editability(assignment(), status(), NOW)
    assert open_now == Open(None)
    assert editability(assignment(duedate=NOW - 90), status(), NOW) == Open(90)
    assert editability(assignment(), status(locked=1), NOW) == Closed(ClosedReason.LOCKED)
    assert editability(assignment(), status(submissionsenabled=0), NOW) == Closed(
        ClosedReason.SUBMISSIONS_DISABLED
    )
    assert editability(assignment(), status(canedit=0), NOW) == Closed(ClosedReason.NOT_EDITABLE)
    assert editability(assignment(allowsubmissionsfromdate=NOW + 1), status(), NOW) == Closed(
        ClosedReason.NOT_OPEN_YET
    )
    past = assignment(duedate=NOW - 100, cutoffdate=NOW - 10)
    assert editability(past, status(), NOW) == Closed(ClosedReason.PAST_CUTOFF)
    assert editability(past, status(extensionduedate=NOW + 3600), NOW) == Open(None)
    assert editability(assignment(), SubmissionStatus.parse({}), NOW) == Closed(
        ClosedReason.NOT_EDITABLE
    )
    assert models.editability_dict(Open(90)) == {
        "open": True,
        "late_seconds": 90,
        "late": "1m",
        "closed_reason": None,
    }

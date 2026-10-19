import re
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

from qalam_mcp.client import QalamClient
from qalam_mcp.lms.service import LmsService
from qalam_mcp.session import HOST, LmsSessionStore, SessionStore


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


LMS = "https://lms.nust.edu.pk"
NOW = 1_800_000_000
DAY = 86400
TOKEN = "synthetic-token-0123456789abcdef"
REST = "/webservice/rest/server.php"


def plugin_configs(files=True, text=False, max_files=3, max_bytes=1024, types=""):
    def config(plugin, name, value):
        return {"plugin": plugin, "subtype": "assignsubmission", "name": name, "value": value}

    return [
        config("file", "enabled", "1" if files else "0"),
        config("file", "maxfilesubmissions", str(max_files)),
        config("file", "maxsubmissionsizebytes", str(max_bytes)),
        config("file", "filetypeslist", types),
        config("onlinetext", "enabled", "1" if text else "0"),
    ]


def submission(status, files=(), text=None, modified=NOW - 2 * DAY):
    plugins = [
        {
            "type": "file",
            "fileareas": [
                {
                    "area": "submission_files",
                    "files": [
                        {"filename": n, "filesize": s, "timemodified": modified} for n, s in files
                    ],
                }
            ],
        }
    ]
    if text is not None:
        plugins.append(
            {
                "type": "onlinetext",
                "editorfields": [{"name": "onlinetext", "text": text, "format": 1}],
            }
        )
    return {"id": 9, "status": status, "timemodified": modified, "plugins": plugins}


def attempt(sub=None, **flags):
    value = {"submissionsenabled": True, "locked": False, "canedit": True, "cansubmit": True}
    value.update(flags)
    if sub is not None:
        value["submission"] = sub
    return {"lastattempt": value, "assignmentdata": {"attachments": {"intro": []}}}


class FakeLms:
    """A synthetic Moodle. Every request is recorded; writes change the stored statuses the
    way Moodle does, so verification can be exercised end to end."""

    def __init__(self):
        self.calls: list[str] = []
        self.forms: list[dict] = []
        self.fail: dict[str, object] = {}
        self.save_effect = None
        self.drafts: dict[int, list[tuple[str, int]]] = {}
        self.clock = NOW
        self.courses = [
            {
                "id": 101,
                "fullname": "CS-101-Intro to Widgets - 2K24-ABC-1",
                "shortname": "CS-101 W",
                "visible": 1,
                "lastaccess": NOW - 100,
                "isfavourite": True,
            },
            {
                "id": 102,
                "fullname": "CS-102-Widget Lab AB 2K24-ABC-1",
                "shortname": "CS-102 Lab",
                "visible": 1,
                "lastaccess": NOW - 50,
                "isfavourite": False,
            },
            {"id": 103, "fullname": "Hidden Course", "visible": 0},
        ]
        file = LMS + "/webservice/pluginfile.php/1/mod_resource/content/0/"
        self.contents = {
            101: [
                {
                    "id": 1,
                    "name": "Week 1: Basics",
                    "modules": [
                        {
                            "id": 11,
                            "name": "Slides",
                            "modname": "resource",
                            "url": LMS + "/mod/resource/view.php?id=11",
                            "contents": [
                                {
                                    "type": "file",
                                    "filename": "slides.pdf",
                                    "filepath": "/",
                                    "filesize": 5,
                                    "fileurl": file + "slides.pdf?forcedownload=1",
                                    "mimetype": "application/pdf",
                                    "timemodified": NOW - 100,
                                }
                            ],
                        },
                        {"id": 12, "name": "Spacer", "modname": "label"},
                        {
                            "id": 13,
                            "name": "Reading",
                            "modname": "url",
                            "contents": [{"type": "url", "fileurl": "https://example.org/read"}],
                        },
                        {
                            "id": 14,
                            "name": "Odd",
                            "modname": "resource",
                            "contents": [
                                {
                                    "type": "file",
                                    "filename": "../../escape.txt",
                                    "filesize": 4,
                                    "fileurl": file + "escape.txt",
                                }
                            ],
                        },
                        {
                            "id": 15,
                            "name": "Revoked",
                            "modname": "resource",
                            "contents": [
                                {
                                    "type": "file",
                                    "filename": "revoked.pdf",
                                    "filesize": 3,
                                    "fileurl": file + "revoked.pdf",
                                }
                            ],
                        },
                        {
                            "id": 16,
                            "name": "Slides v2",
                            "modname": "resource",
                            "contents": [
                                {
                                    "type": "file",
                                    "filename": "slides-v2.pdf",
                                    "filesize": 6,
                                    "fileurl": file + "slides-v2.pdf",
                                }
                            ],
                        },
                    ],
                },
                {
                    "id": 2,
                    "name": "Hidden",
                    "visible": 0,
                    "modules": [{"id": 20, "name": "x", "modname": "resource"}],
                },
            ],
            102: [],
        }
        self.downloads = {
            file + "slides.pdf?forcedownload=1": ("application/pdf", b"%PDF-"),
            file + "slides-v2.pdf": ("application/pdf", b"%PDF-2"),
            file + "escape.txt": ("text/plain", b"nope"),
            file + "revoked.pdf": (
                "application/json",
                b'{"error":"Invalid token","errorcode":"invalidtoken"}',
            ),
        }
        statement = "I wrote this myself."
        self.assignments = {
            501: (101, {"name": "Assignment 1", "duedate": NOW + 3 * DAY}, plugin_configs()),
            502: (101, {"name": "Assignment 10", "duedate": NOW - DAY}, plugin_configs()),
            503: (
                102,
                {"name": "Lab 1", "duedate": NOW - 2 * DAY, "cutoffdate": NOW - 3600},
                plugin_configs(),
            ),
            504: (
                102,
                {"name": "Essay", "duedate": NOW + DAY, "submissiondrafts": 1},
                plugin_configs(text=True),
            ),
        }
        for _, fields, _ in self.assignments.values():
            fields.setdefault("requiresubmissionstatement", 1)
            fields.setdefault("submissionstatement", statement)
        self.statuses = {
            501: attempt(),
            502: attempt(submission("submitted", [("old.pdf", 10)])),
            503: attempt(submission("submitted", [("lab.pdf", 7)]), canedit=False),
            504: attempt(submission("draft", [("draft.pdf", 8)], text="<p>my  essay</p>")),
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/webservice/upload.php":
            return self.upload(request)
        if path.startswith("/webservice/pluginfile.php"):
            self.calls.append("download")
            assert request.url.params.get("token") == TOKEN
            url = str(request.url.copy_remove_param("token"))
            content_type, body = self.downloads[url]
            return httpx.Response(200, content=body, headers={"content-type": content_type})
        form = dict(parse_qsl(request.content.decode()))
        if path == "/login/token.php":
            self.calls.append("login")
            if form["password"] != "fixture-password" or form["username"] == "unknown-user":
                return httpx.Response(200, json={"error": "Invalid", "errorcode": "invalidlogin"})
            return httpx.Response(200, json={"token": TOKEN, "privatetoken": None})
        function = form["wsfunction"]
        self.calls.append(function)
        self.forms.append(form)
        failure = self.fail.get(function)
        if isinstance(failure, Exception):
            raise failure
        if failure is not None:
            return failure
        if form["wstoken"] != TOKEN:
            return httpx.Response(
                200, json={"exception": "x", "errorcode": "invalidtoken", "message": "Invalid"}
            )
        return httpx.Response(200, json=self.answer(function, form))

    def answer(self, function, form):
        if function == "core_webservice_get_site_info":
            return {"userid": 7, "fullname": "Fixture Student", "username": "fixture-user"}
        if function == "core_enrol_get_users_courses":
            assert form["userid"] == "7"
            return self.courses
        if function == "core_course_get_contents":
            return self.contents[int(form["courseid"])]
        if function == "core_calendar_get_action_events_by_timesort":
            return {
                "events": [
                    {
                        "id": 1,
                        "name": "Assignment 1 is due",
                        "activityname": "Assignment 1",
                        "modulename": "assign",
                        "timesort": NOW + 3 * DAY,
                        "overdue": False,
                        "url": LMS + "/mod/assign/view.php?id=901",
                        "course": {"id": 101, "fullname": self.courses[0]["fullname"]},
                        "action": {"name": "Add submission", "url": "", "actionable": True},
                    }
                ]
            }
        if function == "message_popup_get_popup_notifications":
            return {
                "notifications": [
                    {
                        "id": 1,
                        "subject": "Grade posted",
                        "fullmessagehtml": "<p>Hi <b>there</b>,</p><p>Quiz &amp; lab</p>",
                        "contexturl": LMS + "/mod/assign/view.php?id=901",
                        "read": False,
                        "timecreated": NOW - 60,
                    },
                    {"id": 2, "subject": "Old", "text": "seen", "read": True, "timecreated": 5},
                ],
                "unreadcount": 1,
            }
        if function == "mod_assign_get_assignments":
            wanted = {int(v) for k, v in form.items() if k.startswith("courseids[")}
            groups = {}
            for aid, (course, fields, configs) in self.assignments.items():
                if wanted and course not in wanted:
                    continue
                group = groups.setdefault(
                    course,
                    {
                        "id": course,
                        "fullname": next(c["fullname"] for c in self.courses if c["id"] == course),
                        "assignments": [],
                    },
                )
                group["assignments"].append(
                    {"id": aid, "cmid": aid + 400, "course": course, "configs": configs, **fields}
                )
            return {"courses": list(groups.values()), "warnings": []}
        if function == "mod_assign_get_submission_status":
            return self.statuses[int(form["assignid"])]
        if function == "mod_assign_save_submission":
            return self.save(form)
        if function == "mod_assign_submit_for_grading":
            aid = int(form["assignmentid"])
            self.statuses[aid]["lastattempt"]["submission"]["status"] = "submitted"
            return []
        raise AssertionError(function)

    def upload(self, request):
        self.calls.append("upload")
        failure = self.fail.get("upload")
        if isinstance(failure, Exception):
            raise failure
        body = request.content
        fields = dict(re.findall(rb'name="(\w+)"\r\n\r\n([^\r]*)\r\n', body))
        assert fields[b"token"].decode() == TOKEN
        name = re.search(rb'filename="([^"]+)"', body).group(1).decode()
        data = body.split(b"application/octet-stream\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
        itemid = int(fields[b"itemid"]) or 1000 + len(self.drafts)
        self.drafts.setdefault(itemid, []).append((name, len(data)))
        return httpx.Response(200, json=[{"itemid": itemid, "filename": name}])

    def save(self, form):
        aid = int(form["assignmentid"])
        fields = self.assignments[aid][1]
        current = self.statuses[aid]["lastattempt"].get("submission")
        files = [
            (f["filename"], f["filesize"])
            for f in (current or {"plugins": [{"fileareas": [{"files": []}]}]})["plugins"][0][
                "fileareas"
            ][0]["files"]
        ]
        if "plugindata[files_filemanager]" in form:
            files = self.drafts.pop(int(form["plugindata[files_filemanager]"]))
        text = form.get("plugindata[onlinetext_editor][text]")
        if text is not None and form["plugindata[onlinetext_editor][format]"] == "2":
            text = "<p>" + text.replace("<", "&lt;") + "</p>"
        state = "draft" if fields.get("submissiondrafts") else "submitted"
        self.clock += 5
        self.statuses[aid]["lastattempt"]["submission"] = submission(
            state, files, text, modified=self.clock
        )
        if self.save_effect:
            self.save_effect(self.statuses[aid]["lastattempt"]["submission"])
        return []


@pytest.fixture
def lms():
    return FakeLms()


@pytest.fixture
def lms_store(tmp_path):
    store = LmsSessionStore(tmp_path / "private")
    store.save({"token": TOKEN, "user_id": 7, "full_name": "Fixture Student"})
    return store


@pytest.fixture
def lms_service(lms, lms_store):
    return LmsService(lms_store, httpx.MockTransport(lms.handler), clock=lambda: NOW)

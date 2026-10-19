"""Typed views of the Moodle responses we use, parsed once at the boundary. Unknown fields are
ignored and optional ones default, so only a changed shape for a field we read fails."""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

# Module types that only carry layout text, never files.
LAYOUT_MODULES = {"label", "enhancedlabel"}
FORMAT_HTML = 1
# Typed text must keep a literal "<", so it is sent as Moodle's FORMAT_PLAIN.
FORMAT_PLAIN = 2


def flag(value: Any) -> bool:
    """Moodle encodes booleans as 0/1 in some payloads and true/false in others."""
    if isinstance(value, str):
        return value not in ("", "0")
    return bool(value)


def when(value: Any) -> int | None:
    """Moodle uses 0 (or null) for "no date"."""
    return int(value) if value and int(value) > 0 else None


def iso(seconds: int | None) -> str | None:
    return datetime.fromtimestamp(seconds).astimezone().isoformat() if seconds else None


def span(seconds: int) -> str:
    """`2d 4h`, `3h 5m`, `12m`, `<1m`: two units at most, zero units dropped."""
    days, hours, minutes = seconds // 86400, seconds % 86400 // 3600, seconds % 3600 // 60
    if days:
        parts = [(days, "d"), (hours, "h")]
    elif hours:
        parts = [(hours, "h"), (minutes, "m")]
    elif minutes:
        parts = [(minutes, "m")]
    else:
        return "<1m"
    return " ".join(f"{n}{unit}" for n, unit in parts if n)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.0f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    return f"{size / 1024**3:.1f} GB"


BLOCK_TAGS = {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
ENTITIES = {
    "amp": "&",
    "lt": "<",
    "gt": ">",
    "quot": '"',
    "apos": "'",
    "nbsp": " ",
    "ndash": "\u2013",
    "mdash": "\u2014",
    "hellip": "…",
    "lsquo": "‘",
    "rsquo": "’",
    "ldquo": "“",
    "rdquo": "”",
}


def html_to_text(html: str) -> str:
    """Plain text from the HTML fragments Moodle returns. Kept identical to lms-cli, because
    submit verification compares the server's rendering of online text with what was sent."""
    out, rest = [], html
    while (start := rest.find("<")) >= 0:
        out.append(rest[:start])
        after = rest[start + 1 :]
        close = after.find(">")
        if close < 0:
            rest = ""
            break
        name = re.split(r"[\s/]", after[:close].lstrip("/").lower(), maxsplit=1)[0]
        rest = after[close + 1 :]
        if name in ("script", "style") and not after.startswith("/"):
            end = rest.lower().find("</" + name)
            gt = rest.find(">", end) if end >= 0 else -1
            rest = rest[gt + 1 :] if gt >= 0 else ""
        elif name in BLOCK_TAGS:
            out.append("\n")
    out.append(rest)
    lines: list[str] = []
    for line in decode_entities("".join(out)).split("\n"):
        line = " ".join(line.split())
        if line or (lines and lines[-1]):
            lines.append(line)
    return "\n".join(lines).strip()


def decode_entities(text: str) -> str:
    out, rest = [], text
    while (amp := rest.find("&")) >= 0:
        out.append(rest[:amp])
        rest = rest[amp:]
        end = rest.find(";")
        decoded = entity(rest[1:end]) if 0 <= end <= 10 else None
        if decoded is None:
            out.append("&")
            rest = rest[1:]
        else:
            out.append(decoded)
            rest = rest[end + 1 :]
    out.append(rest)
    return "".join(out)


def entity(name: str) -> str | None:
    if name.startswith("#"):
        number = name[1:]
        if number[:1] in ("x", "X") and re.fullmatch(r"[0-9a-fA-F]+", number[1:]):
            code = int(number[1:], 16)
        elif re.fullmatch(r"[0-9]+", number):
            code = int(number)
        else:
            return None
        return chr(code) if code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else None
    return ENTITIES.get(name)


def plain_words(text: str) -> str:
    return " ".join(text.split())


def rendered_words(html: str) -> str:
    return plain_words(html_to_text(html))


BATCH_SUFFIX = re.compile(r"\s*(?:[A-Z]{1,3}\s+)?-?\s*2K[0-9]{2}(?!\w)")


def parse_course_name(full_name: str) -> tuple[str, str]:
    """`CS-101-Intro to Widgets - 2K24-ABC-1` -> (`CS-101`, `Intro to Widgets`). Names outside the
    NUST pattern come back as an empty code and the whole name as the title."""
    found = re.match(r"([A-Z]{2,5}-[0-9]{2,4})-", full_name)
    if not found:
        return "", full_name
    rest = full_name[found.end() :]
    end = next(
        (i for i, c in enumerate(rest) if c.isspace() and BATCH_SUFFIX.match(rest, i)), len(rest)
    )
    title = rest[:end].strip()
    return (found.group(1), title) if title else ("", full_name)


@dataclass(frozen=True)
class CourseRef:
    id: int
    code: str
    title: str
    full_name: str
    short_name: str = ""

    @classmethod
    def from_names(cls, id: int, full_name: str, short_name: str = "") -> "CourseRef":
        code, title = parse_course_name(full_name)
        return cls(int(id), code, title, full_name, short_name)

    @property
    def label(self) -> str:
        return f"{self.code} {self.title}" if self.code else self.title

    @property
    def names(self) -> list[str]:
        return [n for n in (self.code, self.title, self.full_name, self.short_name) if n]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.code or None,
            "title": self.title,
            "full_name": self.full_name,
            "short_name": self.short_name or None,
        }


@dataclass(frozen=True)
class Course:
    ref: CourseRef
    favourite: bool
    last_access: int | None
    visible: bool

    @classmethod
    def parse(cls, raw: dict) -> "Course":
        return cls(
            ref=CourseRef.from_names(raw["id"], raw.get("fullname") or "", raw.get("shortname")),
            favourite=flag(raw.get("isfavourite")),
            last_access=when(raw.get("lastaccess")),
            visible=flag(raw.get("visible", 1)) and not flag(raw.get("hidden")),
        )


@dataclass(frozen=True)
class RemoteFile:
    """A file the server holds: a course resource or part of a submission."""

    name: str
    size: int
    url: str
    mime: str | None
    modified: int | None
    path: str = "/"

    @classmethod
    def parse(cls, raw: dict) -> "RemoteFile":
        return cls(
            name=raw.get("filename") or "",
            size=int(raw.get("filesize") or 0),
            url=raw.get("fileurl") or "",
            mime=raw.get("mimetype") or None,
            modified=when(raw.get("timemodified")),
            path=raw.get("filepath") or "/",
        )

    def as_dict(self) -> dict:
        """Never the URL: the token is appended to it for downloads, so it stays internal."""
        return {
            "name": self.name,
            "size": self.size,
            "size_text": human_size(self.size),
            "mime": self.mime,
            "modified": iso(self.modified),
        }


@dataclass(frozen=True)
class Module:
    id: int
    name: str
    kind: str
    url: str | None
    external_url: str | None
    files: list[RemoteFile]


@dataclass(frozen=True)
class Section:
    id: int
    name: str
    modules: list[Module]

    @classmethod
    def parse(cls, raw: dict) -> "Section":
        shown = flag(raw.get("visible", 1))
        modules = [m for m in map(parse_module, raw.get("modules") or []) if m] if shown else []
        return cls(int(raw["id"]), raw.get("name") or "", modules)


def parse_module(raw: dict) -> Module | None:
    kind = raw.get("modname") or ""
    if not flag(raw.get("visible", 1)) or not flag(raw.get("uservisible", True)):
        return None
    if kind in LAYOUT_MODULES:
        return None
    contents = raw.get("contents") or []
    external_url = None
    files = []
    if kind == "url":
        external_url = (contents[0].get("fileurl") or None) if contents else None
    else:
        files = [
            RemoteFile.parse(c) for c in contents if c.get("type") == "file" and c.get("fileurl")
        ]
    return Module(int(raw["id"]), raw.get("name") or "", kind, raw.get("url"), external_url, files)


@dataclass(frozen=True)
class Deadline:
    id: int
    activity: str
    kind: str
    due: int
    overdue: bool
    url: str
    course: CourseRef
    action: str | None

    @classmethod
    def parse(cls, raw: dict) -> "Deadline":
        action = raw.get("action")
        action = action if isinstance(action, dict) and flag(action.get("actionable")) else None
        course = raw["course"]
        return cls(
            id=int(raw["id"]),
            activity=raw.get("activityname") or raw.get("name") or "",
            kind=raw.get("modulename") or raw.get("eventtype") or "",
            due=int(raw["timesort"]),
            overdue=flag(raw.get("overdue")),
            url=(action or {}).get("url") or raw.get("url") or "",
            course=CourseRef.from_names(
                course["id"], course.get("fullname") or "", course.get("shortname")
            ),
            action=(action or {}).get("name") or None,
        )


@dataclass(frozen=True)
class Notification:
    id: int
    subject: str
    body: str
    url: str | None
    read: bool
    created: int

    @classmethod
    def parse(cls, raw: dict) -> "Notification":
        candidates = [
            html_to_text(raw.get("fullmessagehtml") or ""),
            html_to_text(raw.get("text") or ""),
            raw.get("fullmessage") or "",
        ]
        return cls(
            id=int(raw["id"]),
            subject=raw.get("subject") or "",
            body=next((c for c in candidates if c), ""),
            url=raw.get("contexturl") or None,
            read=flag(raw.get("read")),
            created=int(raw.get("timecreated") or 0),
        )


@dataclass(frozen=True)
class FileLimits:
    max_files: int
    max_bytes: int
    # Empty means any type.
    allowed_types: list[str]

    def as_dict(self) -> dict:
        return {
            "max_files": self.max_files or None,
            "max_bytes": self.max_bytes or None,
            "allowed_types": self.allowed_types,
        }


def number(value: str | None) -> int:
    return int(value) if value and value.isdigit() else 0


@dataclass(frozen=True)
class Assignment:
    id: int
    cmid: int
    course_id: int
    name: str
    # False means saving a submission finalizes it: there is no separate submit step.
    submission_drafts: bool
    due: int | None
    allow_from: int | None
    cutoff: int | None
    requires_statement: bool
    statement: str
    team_submission: bool
    configs: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: dict) -> "Assignment":
        return cls(
            id=int(raw["id"]),
            cmid=int(raw["cmid"]),
            course_id=int(raw["course"]),
            name=raw.get("name") or "",
            submission_drafts=flag(raw.get("submissiondrafts")),
            due=when(raw.get("duedate")),
            allow_from=when(raw.get("allowsubmissionsfromdate")),
            cutoff=when(raw.get("cutoffdate")),
            requires_statement=flag(raw.get("requiresubmissionstatement")),
            statement=raw.get("submissionstatement") or "",
            team_submission=flag(raw.get("teamsubmission")),
            configs={
                (c["plugin"], c["name"]): str(c.get("value") or "")
                for c in raw.get("configs") or []
                if c.get("subtype") == "assignsubmission"
            },
        )

    @property
    def online_text_enabled(self) -> bool:
        return self.configs.get(("onlinetext", "enabled")) == "1"

    @property
    def file_limits(self) -> FileLimits | None:
        """None when the assignment does not take file submissions."""
        if self.configs.get(("file", "enabled")) != "1":
            return None
        types = re.split(r"[,;]", self.configs.get(("file", "filetypeslist"), ""))
        return FileLimits(
            max_files=number(self.configs.get(("file", "maxfilesubmissions"))),
            max_bytes=number(self.configs.get(("file", "maxsubmissionsizebytes"))),
            allowed_types=[t.strip() for t in types if t.strip() not in ("", "*")],
        )


@dataclass(frozen=True)
class CourseAssignment:
    course: CourseRef
    assignment: Assignment


def parse_assignments(raw: dict) -> list[CourseAssignment]:
    return [
        CourseAssignment(
            CourseRef.from_names(group["id"], group.get("fullname") or "", group.get("shortname")),
            Assignment.parse(item),
        )
        for group in raw.get("courses") or []
        for item in group.get("assignments") or []
    ]


class SubmissionState(StrEnum):
    NEW = "new"
    DRAFT = "draft"
    SUBMITTED = "submitted"
    REOPENED = "reopened"


@dataclass(frozen=True)
class EditorField:
    text: str
    format: int


@dataclass(frozen=True)
class Submission:
    id: int
    status: SubmissionState
    modified: int | None
    files: list[RemoteFile]
    online_text: EditorField | None

    @classmethod
    def parse(cls, raw: dict) -> "Submission":
        plugins = raw.get("plugins") or []
        files = [
            RemoteFile.parse(f)
            for p in plugins
            if p.get("type") == "file"
            for area in p.get("fileareas") or []
            if area.get("area") == "submission_files"
            for f in area.get("files") or []
        ]
        text = next(
            (
                EditorField(f.get("text") or "", int(f.get("format", FORMAT_HTML)))
                for p in plugins
                if p.get("type") == "onlinetext"
                for f in p.get("editorfields") or []
                if f.get("name") == "onlinetext"
            ),
            None,
        )
        return cls(
            id=int(raw["id"]),
            status=SubmissionState(raw.get("status") or "new"),
            modified=when(raw.get("timemodified")),
            files=files,
            online_text=text,
        )


@dataclass(frozen=True)
class LastAttempt:
    submission: Submission | None
    submissions_enabled: bool
    locked: bool
    can_edit: bool
    can_submit: bool
    extension_due: int | None
    grading_status: str

    @classmethod
    def parse(cls, raw: dict) -> "LastAttempt":
        submission = raw.get("submission")
        return cls(
            submission=Submission.parse(submission) if submission else None,
            submissions_enabled=flag(raw.get("submissionsenabled")),
            locked=flag(raw.get("locked")),
            can_edit=flag(raw.get("canedit")),
            can_submit=flag(raw.get("cansubmit")),
            extension_due=when(raw.get("extensionduedate")),
            grading_status=raw.get("gradingstatus") or "notgraded",
        )


@dataclass(frozen=True)
class SubmissionStatus:
    """`mod_assign_get_submission_status`."""

    last_attempt: LastAttempt | None
    intro_attachments: list[RemoteFile]

    @classmethod
    def parse(cls, raw: dict) -> "SubmissionStatus":
        attempt = raw.get("lastattempt")
        attachments = ((raw.get("assignmentdata") or {}).get("attachments") or {}).get("intro")
        return cls(
            last_attempt=LastAttempt.parse(attempt) if attempt else None,
            intro_attachments=[RemoteFile.parse(f) for f in attachments or []],
        )

    @property
    def submission(self) -> Submission | None:
        return self.last_attempt.submission if self.last_attempt else None

    @property
    def state(self) -> SubmissionState:
        """A missing attempt or submission object means the student never touched it."""
        return self.submission.status if self.submission else SubmissionState.NEW

    @property
    def files(self) -> list[RemoteFile]:
        return self.submission.files if self.submission else []

    @property
    def online_text(self) -> EditorField | None:
        return self.submission.online_text if self.submission else None

    @property
    def modified(self) -> int | None:
        """`lastattempt.submission.timemodified`; every save moves it forward."""
        return self.submission.modified if self.submission else None


class ClosedReason(StrEnum):
    SUBMISSIONS_DISABLED = "submissions_disabled"
    LOCKED = "locked"
    NOT_OPEN_YET = "not_open_yet"
    PAST_CUTOFF = "past_cutoff"
    NOT_EDITABLE = "not_editable"


@dataclass(frozen=True)
class Open:
    """Moodle accepts a submission now; late_secs is how far past the due date."""

    late_secs: int | None


@dataclass(frozen=True)
class Closed:
    reason: ClosedReason


Editability = Open | Closed


def editability_dict(value: Editability) -> dict:
    late = value.late_secs if isinstance(value, Open) else None
    return {
        "open": isinstance(value, Open),
        "late_seconds": late,
        "late": span(late) if late else None,
        "closed_reason": value.reason.value if isinstance(value, Closed) else None,
    }


def editability(assignment: Assignment, status: SubmissionStatus, now: int) -> Editability:
    """What the student may do right now. Checks run from the most specific reason to the
    generic server flag. Conservative: if the clock says closed it is closed, even if the
    server's canedit is still true."""
    attempt = status.last_attempt
    if attempt is None:
        return Closed(ClosedReason.NOT_EDITABLE)
    if not attempt.submissions_enabled:
        return Closed(ClosedReason.SUBMISSIONS_DISABLED)
    if attempt.locked:
        return Closed(ClosedReason.LOCKED)
    if assignment.allow_from and now < assignment.allow_from:
        return Closed(ClosedReason.NOT_OPEN_YET)
    # An extension can move the last moment either way, as in Moodle's submissions_open.
    last = latest(assignment.cutoff, attempt.extension_due)
    if last and now > last:
        return Closed(ClosedReason.PAST_CUTOFF)
    if not attempt.can_edit:
        return Closed(ClosedReason.NOT_EDITABLE)
    due = latest(assignment.due, attempt.extension_due)
    return Open(now - due if due and now > due else None)


def latest(a: int | None, b: int | None) -> int | None:
    return max(a, b) if a and b else a or b

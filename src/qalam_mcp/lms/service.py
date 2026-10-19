"""High-level LMS operations returning dicts in the Qalam style."""

import json
import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..errors import QalamError
from ..session import LmsSessionStore
from . import models, submit
from .models import CourseAssignment, CourseRef, RemoteFile, Section, SubmissionStatus
from .moodle import Moodle

DAY = 86400
EVENT_LIMIT = 50
NOTIFICATION_LIMIT = 40
NOT_SIGNED_IN = (
    "Not signed in to the LMS. Call nust_login, or have the user run qalam-mcp login. "
    "All agents share the saved sign-in."
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse(what: str, parser: Callable, value: Any):
    try:
        return parser(value)
    except (KeyError, TypeError, ValueError, AttributeError, IndexError):
        raise QalamError("LMS_CHANGED", f"Unexpected {what} response from the LMS.") from None


def sanitize_filename(name: str) -> str:
    """Makes a server-provided name safe as a single path component."""
    cleaned = "".join("_" if ord(c) < 32 or c == "\x7f" or c in '/\\:*?"<>|' else c for c in name)
    return cleaned.strip(". \t\r\n") or "_"


def pick(kind: str, query: str, items: list, names: Callable, describe: Callable):
    """One exact match beats fragment matches ("Assignment 1" over "Assignment 10").
    Ambiguity lists the candidates; it is never a guess."""
    query = query.strip()
    wanted = query.casefold()
    exact = [i for i in items if any(n.casefold() == wanted for n in names(i))]
    found = (
        exact
        if len(exact) == 1
        else [i for i in items if any(wanted in n.casefold() for n in names(i))]
    )
    label = kind.upper()
    if not found:
        raise QalamError(f"{label}_NOT_FOUND", f'No {kind} matches "{query}".')
    if len(found) > 1:
        candidates = [describe(i) for i in found]
        raise QalamError(
            f"{label}_AMBIGUOUS",
            f'"{query}" matches more than one {kind}; retry with an id: ' + "; ".join(candidates),
            {"candidates": candidates},
        )
    return found[0]


def resolve_course(courses: list[CourseRef], query: str) -> CourseRef:
    if not query.strip():
        raise QalamError("COURSE_REQUIRED", "Supply a course id, code, or name.")
    if query.strip().isdigit():
        for course in courses:
            if course.id == int(query):
                return course
    return pick("course", query, courses, lambda c: c.names, lambda c: f"{c.id}: {c.label}")


def resolve_assignment(entries: list[CourseAssignment], query: str) -> CourseAssignment:
    """An assignment id, a name fragment, or `course/name` to narrow the search."""
    if not query.strip():
        raise QalamError("ASSIGNMENT_REQUIRED", "Supply an assignment id or name.")
    if query.strip().isdigit():
        for entry in entries:
            if entry.assignment.id == int(query):
                return entry

    def within(scope, name):
        return pick(
            "assignment",
            name,
            scope,
            lambda e: [e.assignment.name],
            lambda e: f"{e.assignment.id}: {e.course.code or e.course.title} {e.assignment.name}",
        )

    scope, slash, name = query.partition("/")
    if slash:
        wanted = scope.strip().casefold()
        in_course = [e for e in entries if any(wanted in n.casefold() for n in e.course.names)]
        if in_course:
            return within(in_course, name)
    return within(entries, query)


def file_ref(module_id: int, file: RemoteFile) -> str:
    return f"{module_id}{file.path}{file.name}"


def resolve_file(sections: list[Section], query: str) -> tuple[str, RemoteFile]:
    entries = [
        (file_ref(module.id, f), f, module.name)
        for section in sections
        for module in section.modules
        for f in module.files
    ]
    for entry in entries:
        if entry[0] == query.strip():
            return entry[0], entry[1]
    ref, file, _ = pick("file", query, entries, lambda e: [e[1].name], lambda e: f"{e[0]} ({e[2]})")
    return ref, file


class LmsService:
    def __init__(
        self,
        store: LmsSessionStore | None = None,
        transport=None,
        clock: Callable[[], int] | None = None,
    ):
        self.store = store or LmsSessionStore()
        self.transport = transport
        self.clock = clock or (lambda: int(time.time()))

    @contextmanager
    def _client(self) -> Iterator[tuple[Moodle, dict]]:
        session = self.store.load()
        if not session:
            raise QalamError("AUTH_REQUIRED", NOT_SIGNED_IN)
        with Moodle(session["token"], transport=self.transport) as moodle:
            yield moodle, session

    def _done(self, moodle: Moodle, result: dict, path: str) -> dict:
        result = {"ok": True, **result, "source_url": moodle.base + path, "fetched_at": now_iso()}
        # Defence in depth: no field we return should ever carry the token.
        text = json.dumps(result)
        if moodle.token and moodle.token in text:
            result = json.loads(text.replace(moodle.token, "***"))
        return result

    def login(self, username: str, password: str) -> dict:
        if not username.strip() or not password:
            raise QalamError("INVALID_CREDENTIALS", "A username and password are required.")
        with Moodle(transport=self.transport) as moodle:
            info = parse("site info", dict, moodle.login(username.strip(), password))
            session = {
                "token": moodle.token,
                "user_id": parse("site info", lambda v: int(v["userid"]), info),
                "full_name": info.get("fullname"),
                "username": username.strip(),
                "authenticated_at": now_iso(),
            }
        self.store.save(session)
        return {
            "ok": True,
            "authenticated": True,
            "username": session["username"],
            "full_name": session["full_name"],
            "message": "LMS sign-in saved. All agents using this state directory can reuse it.",
        }

    def logout(self) -> dict:
        self.store.clear()
        return {
            "ok": True,
            "authenticated": False,
            "message": "Local LMS sign-in removed. The token is forgotten, not revoked on the LMS.",
        }

    def status(self, check_online: bool = True) -> dict:
        session = self.store.load()
        if not session:
            return {
                "ok": True,
                "authenticated": False,
                "reason": "No saved LMS sign-in.",
                "next_step": "Run qalam-mcp login, or call nust_login with user credentials.",
            }
        result = {
            "ok": True,
            "session_saved": True,
            "username": session.get("username"),
            "full_name": session.get("full_name"),
            "authenticated_at": session.get("authenticated_at"),
        }
        if not check_online:
            return {**result, "authenticated": None, "note": "Saved; validity not checked."}
        try:
            with self._client() as (moodle, _):
                moodle.call("core_webservice_get_site_info")
            result["authenticated"] = True
        except QalamError as exc:
            if exc.code != "AUTH_REQUIRED":
                raise
            result.update(authenticated=False, reason=str(exc))
        return result

    def _courses(self, moodle: Moodle, session: dict) -> list[CourseRef]:
        raw = moodle.call("core_enrol_get_users_courses", userid=session["user_id"])
        parsed = parse("course list", lambda v: list(map(models.Course.parse, v)), raw)
        courses = [c for c in parsed if c.visible]
        courses.sort(key=lambda c: (c.favourite, c.last_access or 0), reverse=True)
        return [c.ref for c in courses]

    def _course(self, moodle: Moodle, session: dict, query: str) -> CourseRef:
        return resolve_course(self._courses(moodle, session), query)

    def _contents(self, moodle: Moodle, course_id: int) -> list[Section]:
        raw = moodle.call("core_course_get_contents", courseid=course_id)
        sections = parse("course contents", lambda v: list(map(Section.parse, v)), raw)
        return [s for s in sections if s.modules]

    def _assignments(self, moodle: Moodle, course_id: int | None = None) -> list[CourseAssignment]:
        raw = moodle.call("mod_assign_get_assignments", courseids=[course_id] if course_id else [])
        return parse("assignment list", models.parse_assignments, raw)

    def _status(self, moodle: Moodle, assignment_id: int) -> SubmissionStatus:
        raw = moodle.call("mod_assign_get_submission_status", assignid=assignment_id)
        return parse("submission status", SubmissionStatus.parse, raw)

    def _status_or_none(self, moodle: Moodle, assignment_id: int) -> SubmissionStatus | None:
        """A status the server refuses (permissions, hidden activity) leaves the row blank."""
        try:
            return self._status(moodle, assignment_id)
        except QalamError as exc:
            if exc.code == "LMS_ERROR":
                return None
            raise

    def list_courses(self) -> dict:
        with self._client() as (moodle, session):
            courses = self._courses(moodle, session)
            return self._done(
                moodle, {"courses": [c.as_dict() for c in courses]}, "/my/courses.php"
            )

    def deadlines(self, include_all: bool = False) -> dict:
        now = self.clock()
        window = (
            {"timesortfrom": 0}
            if include_all
            else {"timesortfrom": now - 14 * DAY, "timesortto": now + 90 * DAY}
        )
        with self._client() as (moodle, _):
            raw = moodle.call(
                "core_calendar_get_action_events_by_timesort",
                limitnum=EVENT_LIMIT,
                limittononsuspendedevents=True,
                **window,
            )
            events = parse(
                "deadline", lambda v: [models.Deadline.parse(e) for e in v.get("events") or []], raw
            )
            items = [
                {
                    "id": e.id,
                    "activity": e.activity,
                    "kind": e.kind,
                    "course": e.course.as_dict(),
                    "due": models.iso(e.due),
                    "overdue": e.overdue,
                    "action": e.action,
                    "url": e.url or None,
                }
                for e in events
            ]
            window_text = (
                "everything still actionable"
                if include_all
                else ("two weeks back to three months ahead")
            )
            return self._done(
                moodle,
                {"window": window_text, "deadlines": items},
                "/calendar/view.php?view=upcoming",
            )

    def notifications(self, unread_only: bool = False) -> dict:
        with self._client() as (moodle, session):
            raw = moodle.call(
                "message_popup_get_popup_notifications",
                useridto=session["user_id"],
                limit=NOTIFICATION_LIMIT,
                offset=0,
                newestfirst=True,
            )
            items = parse(
                "notification",
                lambda v: [models.Notification.parse(n) for n in v.get("notifications") or []],
                raw,
            )
            unread = parse("notification", lambda v: int(v.get("unreadcount") or 0), raw)
            shown = [n for n in items if not (unread_only and n.read)]
            return self._done(
                moodle,
                {
                    "unread_count": unread,
                    "notifications": [
                        {
                            "id": n.id,
                            "subject": n.subject,
                            "body": n.body,
                            "url": n.url,
                            "read": n.read,
                            "created": models.iso(n.created),
                        }
                        for n in shown
                    ],
                },
                "/message/output/popup/notifications.php",
            )

    def assignments(self, course: str | None = None) -> dict:
        now = self.clock()
        with self._client() as (moodle, session):
            course_id = self._course(moodle, session, course).id if course else None
            entries = self._assignments(moodle, course_id)
            entries.sort(key=lambda e: e.assignment.due or float("inf"))
            rows = []
            for entry in entries:
                a = entry.assignment
                status = self._status_or_none(moodle, a.id)
                rows.append(
                    {
                        "id": a.id,
                        "name": a.name,
                        "course": entry.course.as_dict(),
                        "due": models.iso(a.due),
                        "cutoff": models.iso(a.cutoff),
                        "state": status.state.value if status else None,
                        "editability": models.editability_dict(models.editability(a, status, now))
                        if status
                        else None,
                        "finalizes_on_save": not a.submission_drafts,
                        "url": f"{moodle.base}/mod/assign/view.php?id={a.cmid}",
                    }
                )
            path = f"/mod/assign/index.php?id={course_id}" if course_id else "/my/"
            return self._done(moodle, {"assignments": rows}, path)

    def assignment_status(self, assignment: str) -> dict:
        now = self.clock()
        with self._client() as (moodle, _):
            entry = resolve_assignment(self._assignments(moodle), assignment)
            a = entry.assignment
            status = self._status(moodle, a.id)
            attempt = status.last_attempt
            limits = a.file_limits
            view = {
                "id": a.id,
                "name": a.name,
                "course": entry.course.as_dict(),
                "state": status.state.value,
                "grading_status": attempt.grading_status if attempt else "notgraded",
                "editability": models.editability_dict(models.editability(a, status, now)),
                "locked": bool(attempt and attempt.locked),
                "due": models.iso(a.due),
                "allow_submissions_from": models.iso(a.allow_from),
                "cutoff": models.iso(a.cutoff),
                "extension_due": models.iso(attempt.extension_due if attempt else None),
                "finalizes_on_save": not a.submission_drafts,
                "requires_statement": a.requires_statement,
                "statement": a.statement if a.requires_statement and a.statement else None,
                "accepts_files": limits is not None,
                "file_limits": limits.as_dict() if limits else None,
                "accepts_online_text": a.online_text_enabled,
                "online_text": models.html_to_text(status.online_text.text)
                if status.online_text
                else None,
                "last_modified": models.iso(status.modified),
                "files": [f.as_dict() for f in status.files],
                "teacher_attachments": [f.as_dict() for f in status.intro_attachments],
            }
            return self._done(moodle, view, f"/mod/assign/view.php?id={a.cmid}")

    def course_files(self, course: str) -> dict:
        with self._client() as (moodle, session):
            ref = self._course(moodle, session, course)
            sections = self._contents(moodle, ref.id)
            view = [
                {
                    "id": s.id,
                    "name": s.name,
                    "modules": [
                        {
                            "id": m.id,
                            "name": m.name,
                            "kind": m.kind,
                            "url": m.url,
                            "external_url": m.external_url,
                            "files": [{"ref": file_ref(m.id, f), **f.as_dict()} for f in m.files],
                        }
                        for m in s.modules
                    ],
                }
                for s in sections
            ]
            count = sum(len(m.files) for s in sections for m in s.modules)
            return self._done(
                moodle,
                {"course": ref.as_dict(), "file_count": count, "sections": view},
                f"/course/view.php?id={ref.id}",
            )

    def download_file(self, course: str, file: str, directory: str | None = None) -> dict:
        with self._client() as (moodle, session):
            ref = self._course(moodle, session, course)
            file_id, remote = resolve_file(self._contents(moodle, ref.id), file)
            dest = destination(download_root(directory, ref), remote.name)
            skipped = dest.is_file() and dest.stat().st_size == remote.size
            written = remote.size if skipped else moodle.download(remote.url, dest)
            return self._done(
                moodle,
                {
                    "course": ref.as_dict(),
                    "file": {"ref": file_id, **remote.as_dict()},
                    "path": str(dest),
                    "bytes": written,
                    "skipped_existing": skipped,
                },
                f"/course/view.php?id={ref.id}",
            )

    def submit(
        self,
        assignment: str,
        files: list[str],
        text: str | None = None,
        finalize: bool = False,
        plan_id: str | None = None,
        accept_statement: bool = False,
        force: bool = False,
    ) -> dict:
        now = self.clock()
        with self._client() as (moodle, _):
            entry = resolve_assignment(self._assignments(moodle), assignment)
            aid = entry.assignment.id
            status = self._status(moodle, aid)
            request = submit.Request([submit.inspect(f) for f in files], text, finalize)
            plan = submit.build_plan(entry, status, request, now)
            path = f"/mod/assign/view.php?id={entry.assignment.cmid}"
            if plan.up_to_date and not force:
                return self._done(
                    moodle,
                    {
                        "outcome": "up_to_date",
                        "plan": plan.as_dict(),
                        "message": "The LMS already holds these file names and sizes (stored "
                        "no earlier than each local file was modified) in the requested state. "
                        "Nothing was sent. Pass force=true to send anyway.",
                    },
                    path,
                )
            expected_id = plan.plan_id(force)
            if plan_id is None:
                return self._done(
                    moodle,
                    {
                        "outcome": "dry_run",
                        "plan": plan.as_dict(),
                        "plan_id": expected_id,
                        "next_step": "Nothing was sent. Show the user this plan, including "
                        "replaced files, LATE, whether it finalizes, and any statement. Only "
                        "after they explicitly approve this exact plan, call lms_submit again "
                        "with the same arguments and this plan_id.",
                    },
                    path,
                )
            if plan_id.strip() != expected_id:
                raise QalamError(
                    "PLAN_CHANGED",
                    "The plan no longer matches the approved plan_id (files, text, assignment "
                    "state, lateness or options changed). Nothing was submitted. Dry-run again "
                    "and show the user the new plan.",
                )
            if plan.statement is not None and not accept_statement:
                raise QalamError(
                    "STATEMENT_NOT_ACCEPTED",
                    "This assignment requires accepting its submission statement. Show the "
                    "statement to the user and pass accept_statement=true only if they accept "
                    "it. Nothing was submitted.",
                    {"statement": plan.statement},
                )
            submit.execute(moodle, plan)
            try:
                after = self._status(moodle, aid)
            except QalamError:
                raise QalamError(
                    "VERIFY_UNAVAILABLE",
                    "The LMS accepted the submission, but re-fetching its status failed. Call "
                    f"lms_assignment_status for assignment {aid} to confirm what is on record.",
                    {"assignment_id": aid},
                ) from None
            differences = submit.verify(plan.expected(), after)
            if differences:
                raise QalamError(
                    "VERIFICATION_FAILED",
                    "The LMS record does not match what was sent: "
                    + "; ".join(differences)
                    + f". Call lms_assignment_status for assignment {aid}; do not assume the "
                    "submission went through.",
                    {"assignment_id": aid, "differences": differences},
                )
            return self._done(
                moodle,
                {
                    "outcome": "submitted",
                    "plan": plan.as_dict(),
                    "server": {
                        "state": after.state.value,
                        "files": [f.as_dict() for f in after.files],
                        "online_text": models.html_to_text(after.online_text.text)
                        if after.online_text
                        else None,
                        "last_modified": models.iso(after.modified),
                    },
                    "message": "Verified: the LMS now holds exactly what was planned.",
                },
                path,
            )


def download_root(directory: str | None, course: CourseRef) -> Path:
    if directory:
        root = Path(directory).expanduser()
    else:
        configured = os.environ.get("LMS_DOWNLOAD_DIR", "").strip()
        base = Path(configured).expanduser() if configured else Path.home() / "Downloads"
        if not configured:
            base = base / "NUST LMS"
        root = base / sanitize_filename(course.short_name or course.code or str(course.id))
    if not root.is_absolute():
        raise QalamError("DOWNLOAD_REFUSED", "The download directory must be an absolute path.")
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def destination(root: Path, remote_name: str) -> Path:
    dest = root / sanitize_filename(remote_name)
    # resolve() follows a planted symlink, so this also refuses writing through one.
    if dest.resolve().parent != root or dest.is_dir():
        raise QalamError("DOWNLOAD_REFUSED", "Refusing to write outside the download directory.")
    return dest

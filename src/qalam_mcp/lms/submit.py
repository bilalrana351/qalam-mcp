"""The submission flow. build_plan and verify are pure and decide everything; inspect is the
only disk access; execute is the only part that writes to the LMS."""

import hashlib
import json
import stat
import unicodedata
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from ..errors import QalamError
from . import models
from .models import (
    FORMAT_PLAIN,
    Closed,
    CourseAssignment,
    FileLimits,
    SubmissionState,
    SubmissionStatus,
)
from .moodle import Moodle

Record = tuple[str, int]


@dataclass(frozen=True)
class LocalFile:
    name: str
    size: int
    path: str
    # Whole seconds; compared with the server's timemodified.
    modified: int
    sha256: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "size": self.size,
            "size_text": models.human_size(self.size),
            "modified": models.iso(self.modified),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class Problem:
    """A path that cannot be submitted, kept until planning so a closed assignment is
    reported first."""

    kind: str
    path: str
    cause: str = ""


Candidate = LocalFile | Problem


class Refusal(QalamError):
    """A safety check said no. Nothing was written."""

    def __init__(self, kind: str, message: str):
        super().__init__("SUBMIT_REFUSED", f"{message} (nothing was submitted)", {"reason": kind})


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def inspect(raw: str) -> Candidate:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        return Problem("path_not_absolute", raw)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or not path.name:
            return Problem("not_a_file", str(path))
        return LocalFile(path.name, info.st_size, str(path), int(info.st_mtime), digest(path))
    except FileNotFoundError:
        return Problem("file_missing", str(path))
    except OSError as exc:
        return Problem("unreadable", str(path), exc.strerror or type(exc).__name__)


PROBLEMS = {
    "path_not_absolute": "{path} is not an absolute path; pass the full path",
    "file_missing": "{path} does not exist",
    "not_a_file": "{path} is not a regular file",
    "unreadable": "cannot read {path}: {cause}",
}


class TextFate(StrEnum):
    # No new text: sent back unchanged, because a save without the editor field blanks it.
    KEPT = "kept"
    REPLACED = "replaced"


@dataclass(frozen=True)
class TextOnRecord:
    html: str
    format: int
    fate: TextFate


class Finalization(StrEnum):
    # submissiondrafts == 0: the save itself is the submission.
    ON_SAVE = "on_save"
    # submissiondrafts == 1 and finalize: save, then mod_assign_submit_for_grading.
    SUBMIT_FOR_GRADING = "submit_for_grading"
    # submissiondrafts == 1 without finalize: the result stays a draft.
    DRAFT_ONLY = "draft_only"

    @property
    def finalizes(self) -> bool:
        return self is not Finalization.DRAFT_ONLY

    @property
    def expected_state(self) -> SubmissionState:
        return SubmissionState.SUBMITTED if self.finalizes else SubmissionState.DRAFT


@dataclass(frozen=True)
class Request:
    files: list[Candidate]
    text: str | None = None
    finalize: bool = False


@dataclass(frozen=True)
class Expected:
    files: list[Record]
    state: SubmissionState
    text: str | None
    modified_after: int | None


@dataclass(frozen=True)
class SubmitPlan:
    entry: CourseAssignment
    current_state: SubmissionState
    files: list[LocalFile]
    online_text: str | None
    text_on_record: TextOnRecord | None
    # Files on record that the new file set replaces, with "overwritten" or "removed".
    replaces: list[tuple[Record, str]]
    # Files on record left alone because no files are sent (text only).
    keeps: list[Record]
    finalization: Finalization
    late_secs: int | None
    # Required when the save finalizes. An empty string means the LMS did not send the text.
    statement: str | None
    modified_before: int | None
    up_to_date: bool = False

    def editor_text(self) -> tuple[str, int] | None:
        if self.online_text is not None:
            return self.online_text, FORMAT_PLAIN
        held = self.text_on_record
        return (held.html, held.format) if held and held.fate is TextFate.KEPT else None

    def expected(self) -> Expected:
        files = [(f.name, f.size) for f in self.files] if self.files else list(self.keeps)
        held = self.text_on_record
        if self.online_text is not None:
            text = models.plain_words(self.online_text)
        elif held and held.fate is TextFate.KEPT:
            text = models.rendered_words(held.html)
        else:
            text = None
        return Expected(sorted(files), self.finalization.expected_state, text, self.modified_before)

    def plan_id(self, force: bool) -> str:
        """A hash over everything the user approves. Lateness counts as a yes/no, so the id
        stays stable while the clock moves but changes the moment the submission turns late."""
        held = self.text_on_record
        payload = {
            "assignment": self.entry.assignment.id,
            "files": [[f.path, f.size, f.modified, f.sha256] for f in self.files],
            "text": self.online_text,
            "text_on_record": [held.html, held.fate.value] if held else None,
            "replaces": [[name, size, fate] for (name, size), fate in self.replaces],
            "keeps": self.keeps,
            "finalization": self.finalization.value,
            "late": self.late_secs is not None,
            "statement": self.statement,
            "current_state": self.current_state.value,
            "modified_before": self.modified_before,
            "force": force,
        }
        encoded = json.dumps(payload, sort_keys=True).encode()
        return hashlib.sha256(encoded).hexdigest()[:32]

    def as_dict(self) -> dict:
        a, course = self.entry.assignment, self.entry.course
        held = self.text_on_record
        return {
            "assignment": {
                "id": a.id,
                "name": a.name,
                "course": course.as_dict(),
                "due": models.iso(a.due),
            },
            "current_state": self.current_state.value,
            "files": [f.as_dict() for f in self.files],
            "online_text": self.online_text,
            "text_on_record": {"text": models.html_to_text(held.html), "fate": held.fate.value}
            if held
            else None,
            "replaces": [{"name": n, "size": s, "fate": fate} for (n, s), fate in self.replaces],
            "keeps": [{"name": n, "size": s} for n, s in self.keeps],
            "finalization": self.finalization.value,
            "finalizes": self.finalization.finalizes,
            "late": self.late_secs is not None,
            "late_by": models.span(self.late_secs) if self.late_secs else None,
            "statement": self.statement,
            "up_to_date": self.up_to_date,
            "summary": self.summary(),
        }

    def summary(self) -> list[str]:
        a, course = self.entry.assignment, self.entry.course
        lines = [
            f'Assignment "{a.name}" ({course.label}), id {a.id}, currently {self.current_state}.'
        ]
        if self.files:
            names = ", ".join(f"{f.name} ({models.human_size(f.size)})" for f in self.files)
            lines.append(f"Files to send: {names}.")
        if self.online_text is not None:
            lines.append("Online text: replaced by the text given.")
        elif self.text_on_record:
            lines.append("Online text on record: sent back unchanged.")
        for (name, size), fate in self.replaces:
            lines.append(f"On record now and {fate.upper()} by this save: {name} ({size} bytes).")
        if self.late_secs:
            lines.append(f"LATE by {models.span(self.late_secs)}.")
        lines.append(
            {
                Finalization.ON_SAVE: "FINALIZES immediately: saving is the submission.",
                Finalization.SUBMIT_FOR_GRADING: "Saves, then FINALIZES (submit for grading).",
                Finalization.DRAFT_ONLY: "Saves a draft only; it is not submitted for grading.",
            }[self.finalization]
        )
        if self.statement is not None:
            lines.append(
                f'Requires accepting the statement: "{self.statement}"'
                if self.statement
                else "Requires accepting a statement the LMS did not send; read it on the site."
            )
        return lines


def build_plan(
    entry: CourseAssignment, status: SubmissionStatus, request: Request, now: int
) -> SubmitPlan:
    """Refusals run from the most fundamental (closed) to the most local (one bad file), so the
    first one reported is the one that matters."""
    a = entry.assignment
    open_or_closed = models.editability(a, status, now)
    if isinstance(open_or_closed, Closed):
        reason = open_or_closed.reason
        raise Refusal(reason.value, f"the assignment is not open for submission: {reason.value}")
    if a.team_submission:
        raise Refusal(
            "team_submission",
            "this is a group submission; submitting for a group is not supported, use the website",
        )
    text = (request.text or "").strip() or None
    if not request.files and text is None:
        raise Refusal("nothing_to_submit", "nothing to submit: pass files and/or text")
    if text is not None and not a.online_text_enabled:
        raise Refusal("text_not_accepted", "this assignment does not accept online text")
    files: list[LocalFile] = []
    if request.files:
        limits = a.file_limits
        if limits is None:
            raise Refusal("files_not_accepted", "this assignment does not accept file submissions")
        files = checked_files(request.files, limits)

    held = status.online_text
    text_on_record = None
    if held and models.rendered_words(held.text) and a.online_text_enabled:
        fate = TextFate.REPLACED if text is not None else TextFate.KEPT
        text_on_record = TextOnRecord(held.text, held.format, fate)
    if text_on_record and text_on_record.fate is TextFate.KEPT and embeds_files(held.text):
        raise Refusal(
            "text_embeds_files",
            "the online text on record embeds files (images or attachments); a files-only save "
            "would send that text back without them, so pass text to replace it or use the site",
        )

    on_record = [(f.name, f.size) for f in status.files]
    sent = {f.name for f in files}
    replaces = (
        [(r, "overwritten" if r[0] in sent else "removed") for r in on_record] if files else []
    )
    keeps = [] if files else on_record

    if not a.submission_drafts:
        finalization = Finalization.ON_SAVE
    elif request.finalize:
        finalization = Finalization.SUBMIT_FOR_GRADING
    else:
        finalization = Finalization.DRAFT_ONLY
    statement = a.statement if a.requires_statement and finalization.finalizes else None

    plan = SubmitPlan(
        entry=entry,
        current_state=status.state,
        files=files,
        online_text=text,
        text_on_record=text_on_record,
        replaces=replaces,
        keeps=keeps,
        finalization=finalization,
        late_secs=open_or_closed.late_secs,
        statement=statement,
        modified_before=status.modified,
    )
    return replace(plan, up_to_date=is_up_to_date(plan, status))


def embeds_files(html: str) -> bool:
    """Text the status call rewrote to point at stored files, or raw @@PLUGINFILE@@ tokens."""
    return "pluginfile.php" in html or "@@PLUGINFILE@@" in html


# upload.php runs names through Moodle's PARAM_FILE, which deletes these and control characters.
STRIPPED_BY_PARAM_FILE = "&/\\:;'\"|<>?*"


def name_problem(name: str) -> str | None:
    if "�" in name or any(0xD800 <= ord(c) <= 0xDFFF for c in name):
        return "it is not valid UTF-8"
    for c in name:
        if unicodedata.category(c) == "Cc":
            return "it contains a control character, which the LMS strips from file names"
        if c in STRIPPED_BY_PARAM_FILE:
            return f"it contains '{c}', which the LMS strips from file names"
    return None


def checked_files(candidates: list[Candidate], limits: FileLimits) -> list[LocalFile]:
    files: list[LocalFile] = []
    names: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, Problem):
            message = PROBLEMS[candidate.kind].format(path=candidate.path, cause=candidate.cause)
            raise Refusal(candidate.kind, message)
        file = candidate
        if file.size == 0:
            raise Refusal("empty_file", f"{file.path} is empty")
        if file.name in names:
            raise Refusal(
                "duplicate_name",
                f"two files are both named {file.name}; a submission needs unique names",
            )
        names.add(file.name)
        problem = name_problem(file.name)
        if problem:
            raise Refusal(
                "name_not_allowed",
                f"{file.name} cannot be used as a file name on the LMS: {problem}; rename it",
            )
        if limits.max_bytes and file.size > limits.max_bytes:
            raise Refusal(
                "file_too_large",
                f"{file.name} is {models.human_size(file.size)} but the assignment allows at "
                f"most {models.human_size(limits.max_bytes)} per file",
            )
        if not type_allowed(file.name, limits.allowed_types):
            raise Refusal(
                "type_not_allowed",
                f"{file.name} is not an allowed file type (allowed: "
                f"{', '.join(limits.allowed_types)})",
            )
        files.append(file)
    if limits.max_files and len(files) > limits.max_files:
        raise Refusal(
            "too_many_files",
            f"{len(files)} files given but the assignment allows at most {limits.max_files}",
        )
    return files


def type_allowed(name: str, allowed: list[str]) -> bool:
    """Only `.ext` entries can be checked here. Group names such as `document` or mime types
    are left to the server, so a list containing any of those never refuses."""
    if not allowed or any(not t.startswith(".") for t in allowed):
        return True
    _, dot, extension = name.rpartition(".")
    return bool(dot) and any(t[1:].lower() == extension.lower() for t in allowed)


def is_up_to_date(plan: SubmitPlan, status: SubmissionStatus) -> bool:
    """The LMS exposes no content hash, so a local file counts as already uploaded only when a
    server file has its name and size and was stored no earlier than the local file was last
    modified. Unknown times never count as up to date."""
    expected = plan.expected()
    held = status.online_text
    text_matches = expected.text is None or (
        held is not None and models.rendered_words(held.text) == expected.text
    )
    uploaded_after_edit = all(
        any(
            r.name == f.name and r.size == f.size and r.modified and f.modified <= r.modified
            for r in status.files
        )
        for f in plan.files
    )
    return (
        sorted((f.name, f.size) for f in status.files) == expected.files
        and status.state == expected.state
        and text_matches
        and uploaded_after_edit
    )


def verify(expected: Expected, status: SubmissionStatus) -> list[str]:
    """Every way the re-fetched record differs from what the plan said it would hold."""
    differences = []
    held = sorted((f.name, f.size) for f in status.files)
    unmatched = []
    for record in expected.files:
        if record in held:
            held.remove(record)
        else:
            unmatched.append(record)
    for name, size in unmatched:
        same_name = next((h for h in held if h[0] == name), None)
        if same_name:
            held.remove(same_name)
            differences.append(f"{name}: sent {size} bytes but the server holds {same_name[1]}")
        else:
            differences.append(f"{name} ({size} bytes) is not on the server")
    differences += [f"{n} ({s} bytes) is on the server but was not sent" for n, s in held]
    if status.state != expected.state:
        differences.append(f"state is {status.state}, expected {expected.state}")
    if expected.text is not None:
        on_server = models.rendered_words(status.online_text.text) if status.online_text else ""
        if not on_server:
            differences.append("the online text is not on the server")
        elif on_server != expected.text:
            differences.append(
                f"the online text differs; expected: {expected.text}; the server holds: {on_server}"
            )
    after = status.modified
    if after is None or (expected.modified_after is not None and after <= expected.modified_after):
        differences.append(
            f"the server shows no change: last modified {after} (it was "
            f"{expected.modified_after} before the save)"
        )
    return differences


def read_unchanged(file: LocalFile) -> bytes:
    """The bytes to upload, refused if the file changed after the plan was approved."""
    now = inspect(file.path)
    if isinstance(now, Problem):
        raise Refusal("file_changed", f"{file.path} is no longer readable as planned")
    if (now.size, now.modified) != (file.size, file.modified):
        raise Refusal("file_changed", f"{file.path} changed after the plan was made")
    data = Path(file.path).read_bytes()
    if len(data) != file.size or hashlib.sha256(data).hexdigest() != file.sha256:
        raise Refusal("file_changed", f"{file.path} changed after the plan was made")
    return data


def execute(moodle: Moodle, plan: SubmitPlan) -> None:
    """Uploads every file into one draft area (itemid 0 first, then the id the LMS returned),
    saves, and submits for grading only when the plan says so."""
    aid = plan.entry.assignment.id
    contents = [read_unchanged(f) for f in plan.files]
    itemid = 0
    for file, data in zip(plan.files, contents, strict=True):
        draft = moodle.upload(file.name, data, itemid, aid)
        if draft.get("filename") != file.name:
            raise QalamError(
                "UPLOAD_RENAMED",
                f"The LMS stored the upload of {file.name} as {draft.get('filename')}; "
                "nothing was saved to the submission.",
            )
        itemid = draft["itemid"]
    args: dict = {"assignmentid": aid}
    if plan.files:
        args["plugindata[files_filemanager]"] = itemid
    editor = plan.editor_text()
    if editor:
        # itemid 0: no editor draft area, so files embedded in the text are neither added
        # nor removed. build_plan refuses held text that embeds files for this reason.
        args["plugindata[onlinetext_editor][text]"] = editor[0]
        args["plugindata[onlinetext_editor][format]"] = editor[1]
        args["plugindata[onlinetext_editor][itemid]"] = 0
    moodle.write("mod_assign_save_submission", aid, **args)
    if plan.finalization is Finalization.SUBMIT_FOR_GRADING:
        try:
            moodle.write(
                "mod_assign_submit_for_grading",
                aid,
                assignmentid=aid,
                acceptsubmissionstatement=plan.statement is not None,
            )
        except QalamError as exc:
            raise QalamError(
                "SAVED_NOT_SUBMITTED",
                f"The submission was saved as a draft, but submitting it for grading failed: "
                f"{exc} Call lms_assignment_status for assignment {aid}, and submit for grading "
                "on the website if the draft is complete.",
                {"cause": exc.code, "assignment_id": aid},
            ) from None

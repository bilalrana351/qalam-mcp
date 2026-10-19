from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from ..tooling import READ, tool_errors
from .service import LmsService

DOWNLOAD = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
SUBMIT = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)


def register(server: FastMCP, service: LmsService) -> None:
    @server.tool(annotations=READ)
    @tool_errors
    def lms_list_courses() -> dict[str, Any]:
        """List visible LMS (Moodle) courses with id, code, title and short name.

        Favourites first, then most recently used.
        """
        return service.list_courses()

    @server.tool(annotations=READ)
    @tool_errors
    def lms_deadlines(include_all: bool = False) -> dict[str, Any]:
        """Upcoming LMS deadlines from the calendar, including recently overdue ones.

        Default window is two weeks back to three months ahead; include_all returns
        everything the LMS still considers actionable.
        """
        return service.deadlines(include_all)

    @server.tool(annotations=READ)
    @tool_errors
    def lms_notifications(unread_only: bool = False) -> dict[str, Any]:
        """The 40 newest LMS notifications as plain text, with the total unread count."""
        return service.notifications(unread_only)

    @server.tool(annotations=READ)
    @tool_errors
    def lms_assignments(course: str | None = None) -> dict[str, Any]:
        """Assignments with submission state, due date, cutoff and whether they can be edited.

        course is an LMS course id, code (CS-101) or name fragment; omit for all courses.
        state is new, draft, submitted or reopened; null when the LMS would not say.
        """
        return service.assignments(course)

    @server.tool(annotations=READ)
    @tool_errors
    def lms_assignment_status(assignment: str) -> dict[str, Any]:
        """Full status of one assignment: files on record, grading, dates, limits, statement.

        assignment is an id, an unambiguous name fragment, or `course/name`. An exact name
        beats longer names that contain it. Ambiguity lists candidates; retry with the id.
        """
        return service.assignment_status(assignment)

    @server.tool(annotations=READ)
    @tool_errors
    def lms_course_files(course: str) -> dict[str, Any]:
        """Sections, modules and files of an LMS course with sizes, types and dates.

        Each file has a stable `ref` to pass to lms_download_file.
        """
        return service.course_files(course)

    @server.tool(annotations=DOWNLOAD)
    @tool_errors
    def lms_download_file(course: str, file: str, directory: str | None = None) -> dict[str, Any]:
        """Download one course file and return its absolute local path.

        file is a `ref` from lms_course_files, or an unambiguous file name fragment.
        directory must be absolute; by default files go to LMS_DOWNLOAD_DIR or
        ~/Downloads/NUST LMS, in a folder named after the course. An existing file of the
        same size is kept, not downloaded again.
        """
        return service.download_file(course, file, directory)

    @server.tool(annotations=SUBMIT)
    @tool_errors
    def lms_submit(
        assignment: str,
        files: list[str] | None = None,
        text: str | None = None,
        finalize: bool = False,
        plan_id: str | None = None,
        accept_statement: bool = False,
        force: bool = False,
    ) -> dict[str, Any]:
        """Submit files and/or online text to an LMS assignment, in two phases. Treat a
        submission as irreversible: saving replaces the attempt's whole file set, and when the
        assignment has submissiondrafts == 0 (plan.finalization "on_save") saving finalizes it
        immediately, with no separate submit step.

        1. ALWAYS dry-run first: call without plan_id. Nothing is uploaded or written. The
           result has the full plan and a plan_id.
        2. Show the plan to the user: assignment and course, files to send, files on record
           that will be REPLACED or removed, LATE (plan.late_by), whether it FINALIZES, and
           the submission statement text if plan.statement is set (plan.summary has these).
        3. Only after the user explicitly approves that exact plan, call again with the same
           arguments plus plan_id. Pass accept_statement=true only if the user accepted the
           statement. An approval never carries over to another assignment or file list.

        files are absolute local paths. finalize only matters when the assignment keeps
        drafts. PLAN_CHANGED means something changed since the dry run: dry-run again and
        re-confirm. outcome "up_to_date" means the LMS already holds these files; nothing is
        sent unless force=true. Success is reported only after re-fetching the status and
        verifying it. On WRITE_OUTCOME_UNKNOWN or VERIFICATION_FAILED, call
        lms_assignment_status before anything else; never retry blindly. SUBMIT_REFUSED means a
        safety check said no and nothing was sent; fix the cause, do not work around it.
        """
        return service.submit(
            assignment, files or [], text, finalize, plan_id, accept_statement, force
        )

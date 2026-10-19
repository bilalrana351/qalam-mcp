# Qalam MCP

A personal MCP server for NUST Qalam and the NUST LMS (Moodle). Connect any agent/client
that supports MCP stdio or Streamable HTTP. Qalam and the LMS share one NUST username and
password, so one login signs you into both, and every agent reuses the saved sign-ins.
There is no model-provider dependency or model API key.

**Status:** live sign-in, session reuse, dashboard course discovery, attendance, filtered
assessment marks, results and schedule retrieval were verified against Qalam on 2026-10-19. Automated
tests use synthetic portal fixtures and real MCP stdio/HTTP protocol exchanges. The
development host initially encountered Cloudflare verification, so browser sign-in remains
available as a fallback. Portal changes may require parser updates. No student data or
credentials are included in the project; the signed-in session stays in private local state.

The LMS tools were verified read-only against the live LMS on 2026-10-19: courses,
deadlines, notifications, assignments, one assignment's status, one course's files, one
file download, and a submit dry run. No real submission was made. The LMS client is a port
of a separately tested Rust CLI for the same LMS web service.

## Tools

Sign-in, for both systems:

- `nust_auth_status(check_online=True)`: verify the Qalam and LMS sign-ins; offline mode
  checks saved state only.
- `nust_login(username, password, systems=None, lms_username=None)`: sign into Qalam and
  the LMS (or only the systems listed) and save both sign-ins. Passwords are never saved.
- `nust_logout(systems=None)`: forget the shared local sign-ins.

Qalam:

- `qalam_list_courses()`: courses shown on the student dashboard.
- `qalam_attendance(course=None)`: all courses or one course; lecture/lab counts and
  percentages.
- `qalam_grades(course, assessment=None)`: gradebook categories and individual marks;
  filter by `quiz`, `Quiz 2`, `assignment`, `midterm`, etc.
- `qalam_results()`: displayed result cards and tables, following the dashboard's Results
  link.
- `qalam_timetable()`: displayed schedule tables/cards when a supported link is available.

LMS:

- `lms_list_courses()`: visible LMS courses, favourites first.
- `lms_deadlines(include_all=False)`: calendar deadlines from two weeks back to three
  months ahead, or everything still actionable.
- `lms_notifications(unread_only=False)`: the 40 newest notifications as plain text, with
  the unread count.
- `lms_assignments(course=None)`: assignments with state (new, draft, submitted,
  reopened), due date, cutoff and whether they can still be edited.
- `lms_assignment_status(assignment)`: files on record, grading status, dates, file limits,
  submission statement.
- `lms_course_files(course)`: sections, modules and files with sizes, types and dates. Each
  file has a stable `ref`. No URLs are returned, because downloads append the token to them.
- `lms_download_file(course, file, directory=None)`: download one file and return its
  absolute path.
- `lms_submit(assignment, files=[], text=None, finalize=False, plan_id=None,
  accept_statement=False, force=False)`: submit files and/or online text, in two phases.
  See "Submitting to the LMS" below.

Qalam courses can be selected by dashboard ID, code, or name. LMS courses accept an id, a
code (`CS-101`) or a name fragment; assignments accept an id, a name fragment, or
`course/name`. An exact name beats longer names that contain it. Ambiguous references
return the candidates with their IDs instead of a guess. Gradebook tools cover dashboard
courses; the Results tool exposes only what the linked page displays. There is no
semester-switching implementation yet.

Blank marks remain `null`, while displayed zero marks stay `0`. Qalam sometimes displays
zeros for future/ungraded assessments; a zero alone does not establish publication status.
Attendance uses posted class counts. Unmarked attendance stays unknown, even when the
dashboard displays a default percentage. Lecture/lab data stays separate; a combined count-based percentage is also
provided. The server does not invent final grades, GPA, attendance requirements, or
eligibility decisions. Qalam tools only read data. `lms_submit` is the only tool that
changes anything on a server. There are no survey, registration, payment, or
grade-editing tools.

## Submitting to the LMS

A submission replaces the attempt's whole file set, and on assignments without drafts
(`finalization: "on_save"` in the plan) saving it finalizes it immediately. The server
treats every submission as irreversible and makes the agent go through two steps:

1. Without `plan_id`, `lms_submit` is a dry run. It reads the assignment and the local
   files and returns the plan: files to send (absolute path, size, SHA-256), files on
   record that will be replaced or removed, whether it is late and by how much, whether it
   finalizes, and the submission statement if one is required. Nothing is uploaded. The
   result includes a `plan_id`, a hash over everything in that plan.
2. The agent shows you the plan. Only after you approve that exact plan does it call
   `lms_submit` again with the same arguments and the `plan_id`. The server rebuilds the
   plan and refuses with `PLAN_CHANGED` if anything differs (a file was edited, the
   assignment changed, it became late). A required statement also needs
   `accept_statement=true`, which the agent should pass only after you accept the
   statement's text.

The server then uploads the files to a draft area, saves the submission, submits it for
grading when the assignment keeps drafts and you asked to finalize, and re-fetches the
status. Success is reported only when the LMS holds exactly the planned file names and
sizes, state and online text, with a newer modification time. Writes are sent once and
never retried. A write that gets no usable answer returns `WRITE_OUTCOME_UNKNOWN`: check
`lms_assignment_status` before trying again.

Closed assignments, missing or empty files, duplicate names, files over the size or count
limit, disallowed types and names the LMS would alter are refused before anything is sent
(`SUBMIT_REFUSED`), including on dry runs. If the LMS already holds the same file names
and sizes, uploaded after your local files were last modified, the result is
`up_to_date` and nothing is sent unless `force=true`. A files-only save on an assignment
with online text on record sends that text back unchanged, because Moodle would otherwise
blank it.

## Install and sign in

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cd /path/to/qalam-mcp
uv sync --frozen
uv run qalam-mcp login
```

Enter your NUST username and password at the terminal prompts. The password prompt is
hidden, and you are asked once for both systems. Each system is signed into independently,
so if Qalam asks for browser verification the LMS sign-in still succeeds; the output
reports each system separately. Use `--only qalam` or `--only lms` to sign into one, and
`--lms-username` if your LMS username differs. Do not put a password in a command, source
file, or MCP configuration. Alternatively, your agent can ask you for credentials and call
`nust_login`; those arguments may then be in that agent application's history, although
this server never saves the password.

Sessions are stored outside the project, by default in `~/.local/share/qalam-mcp` on Linux
(the platform's application-data directory on macOS/Windows). Qalam cookies live in
`session.json` and the LMS token in `lms-session.json`, each with its own lock so the two
systems never wait on each other. Files are created with mode `0600`, and the directory
uses `0700` on POSIX. Saved cookies and the LMS token are account credentials in their own
right. They are protected by filesystem permissions, not encrypted at rest. Separate agent
processes use locks and atomic writes to share updates.

Set `QALAM_STATE_DIR` to the same dedicated directory in each agent config if you want a
custom location. Every connection to one HTTP server shares **one person's** Qalam account.
This is not a multi-user hosting service. Use separate state directories/processes for
different accounts.

```bash
uv run qalam-mcp status
uv run qalam-mcp status --offline
uv run qalam-mcp logout
```

“Sign in once” lasts until Qalam expires or invalidates the session, or the LMS rejects
its token (LMS tokens are long-lived). The server returns `AUTH_REQUIRED` when a new login
is necessary. Logout forgets the local cookies and token; it does not revoke a copied
cookie on Qalam, the token on the LMS, or sign out an attached external browser.

LMS settings, both optional:

- `LMS_DOWNLOAD_DIR`: where `lms_download_file` saves files when no `directory` is given,
  in a folder per course. Defaults to `~/Downloads/NUST LMS`.
- `LMS_BASE_URL`: a different Moodle site. It must be `https`; plain `http` is accepted
  only for `127.0.0.1` or `localhost` (for a local mock).

### If Qalam asks for browser verification or another authentication step

On a machine with a desktop:

```bash
uv run playwright install chromium
uv run qalam-mcp login --browser
```

Browser sign-in covers Qalam only; run `uv run qalam-mcp login --only lms` for the LMS.
Complete verification and sign-in in the visible browser. The command waits up to five
minutes for the student portal, saves its Qalam cookies, and closes its temporary browser.
Use `--timeout 600` to wait longer. Linux may also need Playwright's system dependencies:
`uv run playwright install --with-deps chromium`, which can require administrator access.

Cloudflare can bind verification to the browser or network. If saved cookies do not work
with direct HTTPS requests, keep a browser running and use the **attached-browser mode**:

```bash
# Use a dedicated profile and a loopback-only debugging port.
# Replace chromium with the Chromium/Chrome executable installed on your machine.
chromium --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.local/share/qalam-browser"

uv run qalam-mcp login --browser --cdp-url http://127.0.0.1:9222
```

Then include `QALAM_CDP_URL=http://127.0.0.1:9222` in the MCP server's environment. Academic
reads use that browser's first context and a new temporary tab, leaving existing tabs and
the browser open. The debug endpoint must be reachable from the MCP server's machine.
Keep the debugging port private: it grants control over that browser. An attached browser
must remain open and signed in. Automatic headless challenge evasion is not implemented.

## Connect your agent locally

Print configuration with the correct absolute executable path for this installation:

```bash
uv run qalam-mcp config
```

Add the `qalam` entry to your MCP client's existing configuration; preserve its other
servers. Many MCP clients use this JSON shape:

```json
{
  "mcpServers": {
    "qalam": {
      "command": "uv",
      "args": ["--directory", "/ABSOLUTE/PATH/TO/qalam-mcp", "run", "--frozen", "qalam-mcp", "serve"]
    }
  }
}
```

An agent's working directory does not matter. An optional `env` object can specify
`QALAM_STATE_DIR` and `QALAM_CDP_URL`. Their exact UI/configuration location varies by
client. The server cannot make a client support a transport it does not implement.

You can also install the command for your OS user:

```bash
uv tool install --editable .
qalam-mcp config
```

## Connect agents from another machine

Start the optional Streamable HTTP server on the machine with your Qalam session:

```bash
uv run qalam-mcp token
uv run qalam-mcp serve --transport http
```

`token` prints a separate, randomly generated MCP access secret. Keep it private. The
server reads it from the private `access-token` file, or from `QALAM_MCP_TOKEN` if set.
The default URL is `http://127.0.0.1:8765/mcp`. All HTTP requests require
`Authorization: Bearer <access-token>`. This is manual bearer authentication, not OAuth;
clients that require an OAuth-only connector cannot use this endpoint directly.

For access from your laptop, keep the server on loopback and forward it over SSH:

```bash
ssh -N -L 8765:127.0.0.1:8765 bilal@YOUR_SERVER
```

Then configure a client that supports Streamable HTTP and authorization headers:

```json
{
  "mcpServers": {
    "qalam": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer REPLACE_WITH_ACCESS_TOKEN"}
    }
  }
}
```

Use your client's secret/header facility where available. If exposing the server on a
public domain, terminate **HTTPS** at a reverse proxy, use `--host 0.0.0.0` behind it,
and set exact `QALAM_ALLOWED_HOSTS` (e.g. `qalam-mcp.example.com`) and
`QALAM_ALLOWED_ORIGINS` (e.g. `https://qalam-mcp.example.com`). Lists are comma-separated.
DNS rebinding protection remains enabled. This project does not deploy a public endpoint
or configure a TLS proxy for you. Do not send account credentials over public plaintext HTTP.

Rotate the access token with `qalam-mcp token --rotate` and **restart the HTTP server**.
Clients must then update their authorization header. Token rotation does not affect the
separate Qalam session. An environment-provided token takes precedence over the token file.

## Try asking your agent

> What's my attendance in each course? Show lecture and lab separately.

> What did I get on Quiz 2 in CS-101?

> Show my assignment marks and class averages for Introduction to Computing.

> Check the results displayed in Qalam.

> What's due on the LMS this week, and which of those haven't I submitted?

> Download the course outline for CS-101 from the LMS.

> Any unread LMS notifications?

> Submit ~/work/report.pdf to Assignment 2 in CS-101. Show me the plan first.

## Development and verification

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Tests use synthetic HTML, synthetic Moodle responses, an HTTP mock transport, and real MCP
client/server exchanges. They cover session expiry, failed logins, cookies and private file
permissions, external redirect rejection, lecture/lab attendance, missing/zero marks,
assessment filtering, stdio discovery, HTTP bearer authentication and DNS rebinding
protection. For the LMS they cover unified and partial login, token storage, auth error
mapping, reference ambiguity, downloads (including path traversal and the auth-failure
body), and the submit flow: dry runs that write nothing, `plan_id` mismatch, the statement
gate, late and closed assignments, up-to-date detection, verified happy paths, verification
mismatches and unknown write outcomes. Separately,
live account checks confirmed the core paths listed at the top of this document. Future
portal changes can still affect compatibility.

The implementation only sends credentials to `https://qalam.nust.edu.pk/web/login` with a
CSRF token read from the login page, follows restricted same-origin routes, and does not
provide an arbitrary URL or Odoo RPC tool. LMS credentials go only to
`https://lms.nust.edu.pk/login/token.php`, and the token only to URLs on that site. The LMS
server omits its intermediate certificate and lacks secure renegotiation, so the LMS
client trusts certifi's roots plus the bundled Sectigo intermediate and allows the legacy
handshake; certificate verification is always on. Passwords, page bodies, session cookies
and the LMS token are not logged. MCP protocol traffic stays on stdout; operational
messages use stderr.

## References

- [Official MCP Python SDK v1 documentation](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
  — this project pins the supported v1 maintenance line with `<2` and locks exact dependencies.
- [Qalam Helper source](https://github.com/AbdullahHassan192/Qalam-Helper)
  — reference for Qalam routes and attendance/gradebook DOM structure. This project's parsers
  were written independently; no extension runtime is bundled.
- [Playwright browser installation](https://playwright.dev/python/docs/browsers)
  and [connecting over CDP](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp).

Unofficial personal integration; not affiliated with NUST. Portal or LMS changes can break
parsers.

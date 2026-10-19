# Qalam MCP

A personal MCP server for NUST Qalam. Connect any agent/client that supports MCP stdio or
Streamable HTTP. Sign into Qalam once and reuse the session across your agents. There is
no model-provider dependency or model API key.

**Status:** live sign-in, session reuse, dashboard course discovery, attendance, filtered
assessment marks, results and schedule retrieval were verified against Qalam on 2026-10-19. Automated
tests use synthetic portal fixtures and real MCP stdio/HTTP protocol exchanges. The
development host initially encountered Cloudflare verification, so browser sign-in remains
available as a fallback. Portal changes may require parser updates. No student data or
credentials are included in the project; the signed-in session stays in private local state.

## Tools

- `qalam_auth_status(check_online=True)` — verify sign-in; offline mode checks saved state only.
- `qalam_login(username, password)` — sign in and save session cookies, without saving a password.
- `qalam_logout()` — forget the shared local session.
- `qalam_list_courses()` — list courses shown on the student dashboard.
- `qalam_attendance(course=None)` — all courses or one course; lecture/lab counts and percentages.
- `qalam_grades(course, assessment=None)` — gradebook categories and individual marks; filter by
  `quiz`, `Quiz 2`, `assignment`, `midterm`, etc.
- `qalam_results()` — displayed result cards and tables, following the dashboard's Results link.
- `qalam_timetable()` — displayed schedule tables/cards when a supported link is available.

Courses can be selected by dashboard ID, code, or name. Ambiguous names produce a choice
of IDs. Gradebook tools cover dashboard courses; the Results tool exposes only what the
linked page displays. There is no semester-switching implementation yet.

Blank marks remain `null`, while displayed zero marks stay `0`. Qalam sometimes displays
zeros for future/ungraded assessments; a zero alone does not establish publication status.
Attendance uses posted class counts. Unmarked attendance stays unknown, even when the
dashboard displays a default percentage. Lecture/lab data stays separate; a combined count-based percentage is also
provided. The server does not invent final grades, GPA, attendance requirements, or
eligibility decisions. Academic tools only read data; there are no survey, registration,
payment, or grade-editing tools.

## Install and sign in

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
cd /path/to/qalam-mcp
uv sync --frozen
uv run qalam-mcp login
```

Enter your username and password at the terminal prompts. The password prompt is hidden.
Do not put a password in a command, source file, or MCP configuration. Alternatively, your
agent can ask you for credentials and call `qalam_login`; those arguments may then be in
that agent application's history, although this server never saves the password.

Sessions are stored outside the project, by default in `~/.local/share/qalam-mcp` on Linux
(the platform's application-data directory on macOS/Windows). Files are created with mode
`0600`, and the directory uses `0700` on POSIX. Saved cookies are account credentials in
their own right. They are protected by filesystem permissions, not encrypted at rest.
Separate agent processes use locks and atomic writes to share cookie updates.

Set `QALAM_STATE_DIR` to the same dedicated directory in each agent config if you want a
custom location. Every connection to one HTTP server shares **one person's** Qalam account.
This is not a multi-user hosting service. Use separate state directories/processes for
different accounts.

```bash
uv run qalam-mcp status
uv run qalam-mcp status --offline
uv run qalam-mcp logout
```

“Sign in once” lasts until Qalam expires or invalidates the session. The server returns
`AUTH_REQUIRED` when a new login is necessary. Logout forgets locally saved cookies; it
does not revoke a copied cookie on Qalam or sign out an attached external browser.

### If Qalam asks for browser verification or another authentication step

On a machine with a desktop:

```bash
uv run playwright install chromium
uv run qalam-mcp login --browser
```

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

## Development and verification

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run pytest -q
```

Tests use synthetic HTML, an HTTP mock transport, and real MCP client/server exchanges.
They cover session expiry, failed logins, cookies and private file permissions, external
redirect rejection, lecture/lab attendance, missing/zero marks, assessment filtering,
stdio discovery, HTTP bearer authentication and DNS rebinding protection. Separately,
live account checks confirmed the core paths listed at the top of this document. Future
portal changes can still affect compatibility.

The implementation only sends credentials to `https://qalam.nust.edu.pk/web/login` with a
CSRF token read from the login page, follows restricted same-origin routes, and does not
provide an arbitrary URL or Odoo RPC tool. Passwords, page bodies and session cookies are
not logged. MCP protocol traffic stays on stdout; operational messages use stderr.

## References

- [Official MCP Python SDK v1 documentation](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
  — this project pins the supported v1 maintenance line with `<2` and locks exact dependencies.
- [Qalam Helper source](https://github.com/AbdullahHassan192/Qalam-Helper)
  — reference for Qalam routes and attendance/gradebook DOM structure. This project's parsers
  were written independently; no extension runtime is bundled.
- [Playwright browser installation](https://playwright.dev/python/docs/browsers)
  and [connecting over CDP](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp).

Unofficial personal integration; not affiliated with NUST. Portal changes can break parsers.

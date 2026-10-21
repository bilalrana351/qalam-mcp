# Qalam LMS MCP

Ask your AI agent about your NUST classes. This server connects any agent that speaks MCP,
such as Claude, Cursor or Codex, to Qalam and the NUST LMS.

- Qalam gives you attendance, marks, results and your timetable.
- The LMS gives you deadlines, assignments, course files and notifications, and lets you
  submit work.

Qalam and the LMS use the same NUST password, so you sign in once and both work. By
default the password is never saved. You can opt into automatic Qalam re-login below.

> Unofficial personal project, not affiliated with NUST. If either portal changes, parts
> of this can break.

## Quick start

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/bilalrana351/qalam-mcp.git
cd qalam-mcp
uv sync --frozen
uv run qalam-mcp login     # asks for your NUST username and password
uv run qalam-mcp config    # prints the config block for your agent
```

Paste the printed block into your agent's MCP settings. It looks like this:

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

Now ask your agent something.

## Things you can ask

> What's my attendance in each course? Show lecture and lab separately.

> What did I get on Quiz 2 in CS-101?

> What's due on the LMS this week that I haven't submitted?

> Download the course outline for CS-101.

> Any unread LMS notifications?

> Submit ~/work/report.pdf to Assignment 2 in CS-101. Show me the plan first.

## Tools

### Sign-in

| Tool | What it does |
| --- | --- |
| `nust_login` | Signs into Qalam and the LMS. You can pick just one with `systems`. |
| `nust_auth_status` | Checks whether both sign-ins still work. |
| `nust_logout` | Forgets the saved sign-ins on this machine. |

### Qalam

| Tool | What it does |
| --- | --- |
| `qalam_list_courses` | Courses on your dashboard. |
| `qalam_attendance` | Lecture and lab attendance, for one course or all of them. |
| `qalam_grades` | Gradebook marks. Filter with words like `quiz`, `Quiz 2` or `midterm`. |
| `qalam_results` | Whatever the Results page shows. |
| `qalam_timetable` | Your class schedule. |

### LMS

| Tool | What it does |
| --- | --- |
| `lms_list_courses` | Your LMS courses, favourites first. |
| `lms_deadlines` | Upcoming deadlines, plus the last two weeks. |
| `lms_notifications` | Recent notifications and how many are unread. |
| `lms_assignments` | Assignments with their due dates and whether you've submitted. |
| `lms_assignment_status` | The full details of one assignment, including the files you've submitted. |
| `lms_course_files` | Files in a course, organised by section. |
| `lms_download_file` | Downloads one file. By default it goes to `~/Downloads/NUST LMS`. |
| `lms_submit` | Submits files or text to an assignment, in two steps. See below. |

You can refer to a course by its code, like `CS-101`, by its name or part of it, or by its ID.
If what you typed matches more than one course, the agent gets the candidates back and
asks you to pick.

Only `lms_submit` changes anything. Every other tool just reads. There are no tools for
surveys, registration, payments or editing grades.

## How submitting works

On NUST's LMS, a submission usually counts as final the moment it's saved. A submission
also replaces every file you submitted before. So `lms_submit` never sends anything until
you approve it.

1. **Plan.** The agent calls `lms_submit` without a `plan_id`. The server uploads nothing
   and sends back a plan that lists the files that will be sent, the files on the LMS they will
   replace, whether the submission is late and whether it will be final.
2. **Approve.** The agent shows you that plan. If you say yes, it calls `lms_submit` again
   with the plan's `plan_id`. If anything changed in between, say you edited a file or the
   deadline passed, the server refuses with `PLAN_CHANGED` and you start again.

After submitting, the server reloads the assignment and confirms the LMS has exactly the
files you planned. If the connection drops in the middle, the server doesn't retry. It
tells the agent to check the assignment status before trying again.

The server refuses some submissions before sending anything: closed assignments, empty or missing
files, too many files, files that are too large and file types the assignment doesn't
allow. If the LMS already has the same files, the server says so and does nothing.

## Where your login is stored

- By default the server keeps only the session that signing in produces.
- Sessions live in your user data folder, not in the project. That's
  `~/.local/share/qalam-mcp` on Linux and the usual app-data folder on macOS and Windows.
  Set `QALAM_STATE_DIR` to use a different folder.
- The files are readable only by you. They aren't encrypted, though, and anyone who can
  read them can act as you on Qalam and the LMS.
- A sign-in lasts until Qalam or the LMS expires it. Without automatic Qalam re-login,
  tools return `AUTH_REQUIRED` and you run `login` again.
- `logout` deletes the local sessions and remembered Qalam credentials. It doesn't sign
  you out on NUST's side.

Every agent on the same machine shares one sign-in, so this is meant for one person's
account. For a second account, use a separate `QALAM_STATE_DIR`.

```bash
uv run qalam-mcp status              # are both sign-ins still good?
uv run qalam-mcp login --only lms    # sign into just one system
uv run qalam-mcp logout
```

If your LMS username is different from your Qalam one, add `--lms-username`.

### Automatic Qalam re-login (optional)

If Qalam frequently expires your session, enable automatic re-login explicitly:

```bash
uv run qalam-mcp login --only qalam --remember-qalam
```

This saves your Qalam username and **plaintext password, without encryption**, in
`qalam-credentials.json` in the private state folder outside the project. On POSIX the
folder is `0700` and the file is `0600`; only your OS user can read it. Anyone with access
to your OS account or its backups can still read the password. It is never included in
tool results, logs, session cookies, or MCP configuration. Default login remains
password-free on disk. An agent may use `nust_login(..., remember_qalam=True)` only after
you explicitly ask it to remember your credentials.

On a missing or expired session, the next Qalam read signs in and retries that read once.
A shared lock prevents simultaneous agents from repeating the login. A failed automatic
login waits five minutes before another attempt; a rejected password removes the stored
credentials so later reads cannot repeatedly submit a bad password. Explicit login can
be retried immediately. Nothing retries an LMS submission or any other write.

The opt-in persists even if Cloudflare or a network error prevents the initial login, so
you do not need to provide credentials again once access is restored. Saving credentials
does **not** mean that sign-in succeeded; the login still returns its access error. Check
`status --offline` for `auto_login_enabled` without contacting Qalam.

This works with direct HTTPS access to Qalam. Cloudflare challenges, access blocks,
pending feedback and an attached browser's expired login still require user action.
The server does not retry passwords for those conditions. To disable automatic re-login,
run `logout --only qalam`, or sign in successfully without `--remember-qalam`. Successful
interactive browser login also removes previously remembered credentials.

## Troubleshooting

**"PORTAL_BLOCKED".** Cloudflare blocked this machine before Qalam could check the login.
This differs from an expired session or an interactive verification challenge. Repeated
password attempts will not fix it. Open Qalam in a browser on your own network; if access
is still blocked, contact Qalam support. The server keeps your existing session and any
remembered credentials.

**"AUTO_LOGIN_DEFERRED".** An automatic login recently failed. Wait five minutes or run
an explicit login after resolving the reported problem. A plain HTTP 403 returns
`ACCESS_DENIED` and does not automatically trigger another login.

**"FEEDBACK_REQUIRED".** Qalam hides every student page until you fill in the pending QA
feedback forms. Do them in your browser at
`https://qalam.nust.edu.pk/student/qa/feedback`, then ask again. You don't need to sign
in again. The server deliberately never touches these forms.

**Qalam shows a Cloudflare check.** Sign in through a real browser window:

```bash
uv run playwright install chromium
uv run qalam-mcp login --browser
```

This covers Qalam only. If Qalam still rejects the saved session afterwards, keep a
browser running and let the server use it:

```bash
chromium --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.local/share/qalam-browser"
uv run qalam-mcp login --browser --cdp-url http://127.0.0.1:9222
```

Then add `QALAM_CDP_URL=http://127.0.0.1:9222` to the server's `env` in your agent config.
Keep that port private, because anything that can reach it can control the browser.

## Using it from another machine

You can run the server on one machine and connect to it from another, for example a home
server and your laptop.

```bash
uv run qalam-mcp token                  # prints an access token, keep it secret
uv run qalam-mcp serve --transport http # listens on http://127.0.0.1:8765/mcp
```

From your laptop, tunnel to it over SSH and point your agent at the tunnel:

```bash
ssh -N -L 8765:127.0.0.1:8765 you@your-server
```

```json
{
  "mcpServers": {
    "qalam": {
      "url": "http://127.0.0.1:8765/mcp",
      "headers": {"Authorization": "Bearer YOUR_ACCESS_TOKEN"}
    }
  }
}
```

To expose it publicly, put it behind an HTTPS reverse proxy, run it with
`--host 0.0.0.0`, and set `QALAM_ALLOWED_HOSTS` and `QALAM_ALLOWED_ORIGINS` to your
domain. `qalam-mcp token --rotate` makes a new token. Restart the server after rotating.

## Settings

| Variable | Default | Purpose |
| --- | --- | --- |
| `QALAM_STATE_DIR` | your app-data folder | Where sign-ins are saved. |
| `LMS_DOWNLOAD_DIR` | `~/Downloads/NUST LMS` | Where downloads go. Each course gets its own folder. |
| `QALAM_CDP_URL` | none | Reuse a running browser for Qalam. |
| `QALAM_MCP_TOKEN` | the saved token file | Access token for HTTP mode. |
| `QALAM_ALLOWED_HOSTS`, `QALAM_ALLOWED_ORIGINS` | none | Allowed host names and origins for HTTP mode behind a proxy. |
| `LMS_BASE_URL` | `https://lms.nust.edu.pk` | A different Moodle site, or a local mock for testing. |

## A few things to know about the data

- A blank mark stays blank. Qalam sometimes shows `0` for assessments that haven't been
  graded yet, so a zero might just mean the mark isn't in.
- Attendance only counts classes your teacher has marked. You get lecture and lab
  numbers separately, plus a combined percentage.
- The server never makes up GPAs, final grades or attendance cut-offs. It only reports
  what the portals show.

## Security notes

- Your password is only sent to Qalam's login page and the LMS's login endpoint, and
  only over HTTPS.
- The LMS server sends an incomplete certificate chain, so the project bundles the
  missing Sectigo intermediate certificate. Certificate checks stay on.
- The server never logs passwords, cookies, LMS tokens or page contents.
- LMS tools don't return download links, because those links contain your login token.

## Development

```bash
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

The tests run against fake Qalam pages and a fake Moodle server, so you don't need a
real account to run them. They cover expired sessions, failed logins, file permissions, redirect
safety, attendance and marks parsing, the whole submit flow (plans, refusals, late work,
verification and dropped connections), and real MCP traffic over stdio and HTTP.
Qalam renewal tests also cover concurrent agents, bounded retries, login failure cooldowns,
credential rejection, account isolation and logout removing automatic-login credentials.

## Credits

- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x)
- [Qalam Helper](https://github.com/AbdullahHassan192/Qalam-Helper), which helped map
  Qalam's pages. The parsers here were written separately.
- The LMS client is a port of a separately tested Rust CLI for the same LMS.

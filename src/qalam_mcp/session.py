"""Private, atomic session storage shared by independent agent processes."""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout
from platformdirs import user_data_path

from .errors import QalamError

ORIGIN = "https://qalam.nust.edu.pk"
HOST = "qalam.nust.edu.pk"


def state_directory() -> Path:
    configured = os.environ.get("QALAM_STATE_DIR")
    return Path(configured).expanduser().resolve() if configured else user_data_path("qalam-mcp")


class PrivateStore:
    """One 0600 JSON file in the 0700 state directory, guarded by its own lock file."""

    filename = ""
    lockname = ""
    label = ""

    def __init__(self, directory: Path | None = None):
        self.directory = directory or state_directory()

    def prepare(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.directory.chmod(0o700)

    @contextmanager
    def locked(self):
        self.prepare()
        try:
            with FileLock(str(self.directory / self.lockname), timeout=90, mode=0o600):
                yield
        except Timeout as exc:
            raise QalamError(
                "SESSION_BUSY",
                f"Another agent is using the shared {self.label} session. Retry shortly.",
            ) from exc

    def read(self) -> dict | None:
        path = self.directory / self.filename
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict) or value.get("version") != 1 or not self.valid(value):
                raise ValueError("Unsupported session")
            return value
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise QalamError(
                "SESSION_INVALID", f"Saved {self.label} session is invalid. Run qalam-mcp login."
            ) from exc

    def valid(self, value: dict) -> bool:
        raise NotImplementedError

    def write_private(self, name: str, content: str) -> None:
        self.prepare()
        fd, temporary = tempfile.mkstemp(prefix=".write-", dir=self.directory)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.directory / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def clear(self) -> None:
        with self.locked():
            (self.directory / self.filename).unlink(missing_ok=True)


class SessionStore(PrivateStore):
    filename = "session.json"
    lockname = "session.lock"
    label = "Qalam"

    def valid(self, value: dict) -> bool:
        return isinstance(value.get("cookies"), list)

    def load(self) -> dict | None:
        return self.read()

    def write(self, state: dict) -> None:
        # Only this explicit schema is persisted; never accept/store arbitrary password fields.
        data = {
            "version": 1,
            "cookies": [c for c in state["cookies"] if c.get("domain", "").lstrip(".") == HOST],
            "user_agent": state.get("user_agent", "QalamMCP/0.1"),
            "username": state.get("username"),
            "authenticated_at": state.get("authenticated_at"),
        }
        self.write_private(self.filename, json.dumps(data))


class LmsSessionStore(PrivateStore):
    """The Moodle mobile token. Its own lock, so LMS calls never wait on Qalam's."""

    filename = "lms-session.json"
    lockname = "lms-session.lock"
    label = "LMS"

    def valid(self, value: dict) -> bool:
        return isinstance(value.get("token"), str) and isinstance(value.get("user_id"), int)

    def load(self) -> dict | None:
        with self.locked():
            return self.read()

    def save(self, session: dict) -> None:
        data = {
            "version": 1,
            "token": session["token"],
            "user_id": session["user_id"],
            "full_name": session.get("full_name"),
            "username": session.get("username"),
            "authenticated_at": session.get("authenticated_at"),
        }
        with self.locked():
            self.write_private(self.filename, json.dumps(data))

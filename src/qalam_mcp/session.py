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


class SessionStore:
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
            with FileLock(str(self.directory / "session.lock"), timeout=90, mode=0o600):
                yield
        except Timeout as exc:
            raise QalamError(
                "SESSION_BUSY", "Another agent is using the shared Qalam session. Retry shortly."
            ) from exc

    def load(self) -> dict | None:
        path = self.directory / "session.json"
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text())
            if value.get("version") != 1 or not isinstance(value.get("cookies"), list):
                raise ValueError("Unsupported session")
            return value
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise QalamError(
                "SESSION_INVALID", "Saved session is invalid. Run qalam-mcp login."
            ) from exc

    def write(self, state: dict) -> None:
        # Only this explicit schema is persisted; never accept/store arbitrary password fields.
        data = {
            "version": 1,
            "cookies": [c for c in state["cookies"] if c.get("domain", "").lstrip(".") == HOST],
            "user_agent": state.get("user_agent", "QalamMCP/0.1"),
            "username": state.get("username"),
            "authenticated_at": state.get("authenticated_at"),
        }
        self.write_private("session.json", json.dumps(data))

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
            (self.directory / "session.json").unlink(missing_ok=True)

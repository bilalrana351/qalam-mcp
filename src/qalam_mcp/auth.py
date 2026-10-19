"""One NUST account, two systems. Each system is attempted on its own, so Qalam's Cloudflare
check never blocks the LMS. Shared by the MCP tools and the CLI."""

from collections.abc import Callable

from .client import QalamClient
from .errors import QalamError
from .lms.service import LmsService

SYSTEMS = ("qalam", "lms")


def chosen(systems: list[str] | None) -> list[str]:
    names = list(dict.fromkeys(s.strip().lower() for s in systems or SYSTEMS))
    unknown = [s for s in names if s not in SYSTEMS]
    if unknown or not names:
        raise QalamError("INVALID_SYSTEMS", "systems may contain only 'qalam' and 'lms'.")
    return names


def each(actions: dict[str, Callable[[], dict]]) -> dict:
    results = {}
    for name, action in actions.items():
        try:
            results[name] = action()
        except QalamError as exc:
            results[name] = exc.as_dict()
    return {"ok": all(r.get("ok") for r in results.values()), **results}


def login(
    qalam: QalamClient,
    lms: LmsService,
    username: str,
    password: str,
    systems: list[str] | None = None,
    lms_username: str | None = None,
) -> dict:
    actions = {
        "qalam": lambda: qalam.login(username, password),
        "lms": lambda: lms.login(lms_username or username, password),
    }
    return each({name: actions[name] for name in chosen(systems)})


def logout(qalam: QalamClient, lms: LmsService, systems: list[str] | None = None) -> dict:
    def qalam_logout() -> dict:
        qalam.store.clear()
        return {"ok": True, "authenticated": False, "message": "Local Qalam session removed."}

    actions = {"qalam": qalam_logout, "lms": lms.logout}
    return each({name: actions[name] for name in chosen(systems)})


def status(qalam: QalamClient, lms: LmsService, check_online: bool = True) -> dict:
    return each(
        {"qalam": lambda: qalam.status(check_online), "lms": lambda: lms.status(check_online)}
    )

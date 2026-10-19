class QalamError(Exception):
    """An actionable error safe to return to an agent, without response bodies or secrets."""

    def __init__(self, code: str, message: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(message)

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "message": str(self), **self.details}

"""Shared MCP tool plumbing for the Qalam and LMS tool sets."""

import asyncio
import json
from collections.abc import Callable
from functools import wraps

from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .errors import QalamError


def tool_errors(fn: Callable) -> Callable:
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except QalamError as exc:
            raise ToolError(json.dumps(exc.as_dict())) from None

    return wrapped


READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
LOCAL = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)

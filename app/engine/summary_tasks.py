"""Meeting summary task management and cancellation module.

Provides global task registries for tracking, broadcasting, and cleanly cancelling
async meeting summary and finalization jobs.
"""
from __future__ import annotations

import asyncio
from typing import Any

# Global task registries for active meeting summary operations
summary_tasks: dict[int, asyncio.Task] = {}
summary_starts: dict[int, float] = {}
active_summary_thoughts: dict[int, list[dict[str, Any]]] = {}
finalizing_meetings: set[int] = set()


def cancel_summary_task(meeting_id: int) -> bool:
    """Cancel an active summary task for a meeting and clean up tracking state.

    Args:
        meeting_id (int): Primary key ID of the target meeting.

    Returns:
        bool: True if an active task was cancelled, False otherwise.
    """
    task = summary_tasks.pop(meeting_id, None)
    summary_starts.pop(meeting_id, None)
    active_summary_thoughts.pop(meeting_id, None)
    finalizing_meetings.discard(meeting_id)

    if task and not task.done():
        task.cancel()
        return True
    return False


def is_meeting_summarizing(meeting_id: int, status: str | None) -> bool:
    """Determine if a meeting is actively running a summary generation.

    Args:
        meeting_id (int): Primary key ID of the target meeting.
        status (str | None): Current meeting status string.

    Returns:
        bool: True if an active summary task is running or status is processing.
    """
    task = summary_tasks.get(meeting_id)
    if task and not task.done():
        return True
    if meeting_id in finalizing_meetings:
        return True
    if status == "processing":
        return True
    return False

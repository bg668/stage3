"""Presentation-only models for the Stage-2 live console.

Every object in this module is derived from AgentEvent facts.  None of them is
business state and none may be consumed by triage decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


@dataclass(frozen=True)
class ActiveTaskView:
    key: str
    label: str
    slot: int = 0
    step_name: str = ""


@dataclass(frozen=True)
class WorkState:
    actor: str
    phase: str
    active_tasks: tuple[ActiveTaskView, ...] = ()
    completed: int = 0
    total: int = 0
    note: str | None = None
    busy: bool = False
    max_concurrency: int = 0


@dataclass(frozen=True)
class ProductionStats:
    """Low-level accumulated counters derived only from emitted AgentEvents."""

    input_total: int = 0
    round1_analyze: int = 0
    round1_skip: int = 0
    round1_uncertain: int = 0
    context_complete: int = 0
    context_partial: int = 0
    context_failed: int = 0
    context_truncated: int = 0
    round2_analyze: int = 0
    round2_skip: int = 0
    round2_forced: int = 0
    final_analyze: int = 0
    final_skip: int = 0
    stage_complete: bool = False


@dataclass(frozen=True)
class StageMilestone:
    key: str
    label: str
    summary: str


@dataclass(frozen=True)
class ReasonCount:
    reason: str
    label: str
    count: int


@dataclass(frozen=True)
class AchievementHighlight:
    ref: str
    text: str


@dataclass(frozen=True)
class AchievementState:
    """Durable presentation summary of results already produced in Stage 2."""

    completed_stages: tuple[StageMilestone, ...] = ()
    converged: int = 0
    retained: int = 0
    excluded: int = 0
    pending: int = 0
    reason_counts: tuple[ReasonCount, ...] = ()
    highlights: tuple[AchievementHighlight, ...] = ()
    conclusion_count: int = 0


@dataclass(frozen=True)
class TaskBubble:
    """Internal grouped result before it is converted to a visible report.

    ``title`` is kept for compatibility/debugging.  Rich does not expose it to the
    user because task/unit ids are implementation details rather than useful status.
    """

    key: str
    title: str
    lines: tuple[str, ...]
    outcome: str = ""
    importance: str = "normal"  # normal | important | critical
    timestamp: str = ""
    actor: str = "员工1号"


class ReportState(str, Enum):
    """Presentation lifecycle state used only by live renderers."""

    WORKING = "WORKING"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True)
class RealtimeReport:
    """One visible right-column report with display-only sequence and local time."""

    sequence: int
    time_text: str
    actor: str
    key: str
    lines: tuple[str, ...]
    step_name: str = ""
    importance: str = "normal"
    state: ReportState = ReportState.WORKING

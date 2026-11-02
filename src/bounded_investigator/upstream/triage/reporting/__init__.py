"""Presentation-only reporting subsystem for Stage-2."""

from .achievement_tracker import AchievementTracker
from .active_task_slots import ActiveTaskSlotAllocator
from .activity_indicator import ActivityIndicator, ActivityPulseController
from .achievement_panel import AchievementPanelRenderer
from .dialogue_composer import DialogueComposer, DialogueMessage, DialogueUpdate
from .events import AgentEvent, AgentEventSink
from .highlight_viewport import HighlightViewport, VisibleHighlight
from .latest_result import LatestResultBuffer, LatestResultSnapshot
from .final_summary_renderer import render_final_summary
from .pacing_renderer import PacingConfig, PacingRenderer
from .render_scheduler import RenderScheduler
from .phrase_chunker import PhraseChunker
from .presentation_model import (
    AchievementHighlight,
    AchievementState,
    ActiveTaskView,
    ProductionStats,
    ReasonCount,
    RealtimeReport,
    ReportState,
    StageMilestone,
    TaskBubble,
    WorkState,
)
from .report_lifecycle import ReportLifecycle, ReportOperation, ReportUpdate
from .report_sequencer import ReportSequencer
from .step_labels import StepLabelResolver
from .report_viewport import ReportViewport, VisibleReports
from .reporter import Reporter, build_reporter
from .trace_logger import EventTraceLogger

__all__ = [
    "AchievementHighlight",
    "AchievementState",
    "AchievementTracker",
    "ActiveTaskSlotAllocator",
    "AchievementPanelRenderer",
    "ActivityIndicator",
    "ActivityPulseController",
    "ActiveTaskView",
    "AgentEvent",
    "AgentEventSink",
    "DialogueComposer",
    "DialogueMessage",
    "DialogueUpdate",
    "EventTraceLogger",
    "HighlightViewport",
    "LatestResultBuffer",
    "LatestResultSnapshot",
    "render_final_summary",
    "PacingConfig",
    "PacingRenderer",
    "PhraseChunker",
    "ProductionStats",
    "ReasonCount",
    "RealtimeReport",
    "ReportState",
    "ReportLifecycle",
    "ReportOperation",
    "ReportUpdate",
    "ReportSequencer",
    "ReportViewport",
    "RenderScheduler",
    "VisibleHighlight",
    "VisibleReports",
    "Reporter",
    "StageMilestone",
    "StepLabelResolver",
    "TaskBubble",
    "WorkState",
    "build_reporter",
]

"""Accumulate Stage-2 achievements from immutable AgentEvent facts.

This module owns result accumulation for presentation.  It does not compose prose,
render UI, or influence triage decisions.
"""

from __future__ import annotations

from collections import Counter, deque
from typing import Any

from .events import AgentEvent
from .presentation_model import (
    AchievementHighlight,
    AchievementState,
    ProductionStats,
    ReasonCount,
    StageMilestone,
)
from .reason_text import ANALYZE_REASON_LABELS, contains_cjk, compact, strip_terminal_punctuation


class AchievementTracker:
    def __init__(self, *, highlight_limit: int = 20) -> None:
        self._highlight_limit = max(1, highlight_limit)
        self._input_total = 0
        self._r1 = {"ANALYZE": 0, "SKIP": 0, "UNCERTAIN": 0}
        self._r2 = {"ANALYZE": 0, "SKIP": 0}
        self._r2_forced = 0
        self._context = {"COMPLETE": 0, "PARTIAL": 0, "FAILED": 0}
        self._context_truncated = 0
        self._seen_r1_tasks: set[str] = set()
        self._seen_r2_tasks: set[str] = set()
        self._seen_contexts: set[str] = set()
        self._reasons: Counter[str] = Counter()
        self._highlights: deque[AchievementHighlight] = deque(maxlen=self._highlight_limit)
        self._highlight_refs: set[str] = set()
        self._r1_completed = False
        self._context_completed = False
        self._r2_completed = False
        self._stage_complete = False
        self._final = {"ANALYZE": 0, "SKIP": 0}

    def apply(self, item: AgentEvent) -> None:
        data = item.data
        if item.event == "stage_started":
            self._input_total = int(data.get("undecided", 0) or 0)
            return

        if item.event == "task_completed" and str(data.get("status", "OK")) == "OK":
            verdicts = list(data.get("verdicts") or [])
            self._apply_task(item, verdicts, data)
            return

        if item.event == "round_completed":
            if item.round == 1:
                self._r1 = {
                    "ANALYZE": int(data.get("analyze", 0) or 0),
                    "SKIP": int(data.get("skip", 0) or 0),
                    "UNCERTAIN": int(data.get("uncertain", 0) or 0),
                }
                self._r1_completed = True
            elif item.round == 2:
                self._r2 = {
                    "ANALYZE": int(data.get("analyze", 0) or 0),
                    "SKIP": int(data.get("skip", 0) or 0),
                }
                self._r2_forced = int(data.get("forced", 0) or 0)
                self._r2_completed = True
            return

        if item.event == "context_assembled":
            self._apply_context(item)
            return

        if item.event == "context_assembly_completed":
            self._context_completed = True
            # Event totals are authoritative at the stage boundary.
            self._context = {
                "COMPLETE": int(data.get("complete", self._context["COMPLETE"]) or 0),
                "PARTIAL": int(data.get("partial", self._context["PARTIAL"]) or 0),
                "FAILED": int(data.get("failed", self._context["FAILED"]) or 0),
            }
            self._context_truncated = int(data.get("truncated", self._context_truncated) or 0)
            return

        if item.event == "stage_completed":
            self._stage_complete = bool(data.get("complete", True))
            self._final = {
                "ANALYZE": int(data.get("stage2_analyze", 0) or 0),
                "SKIP": int(data.get("stage2_skip", 0) or 0),
            }

    @property
    def stats(self) -> ProductionStats:
        return ProductionStats(
            input_total=self._input_total,
            round1_analyze=self._r1["ANALYZE"],
            round1_skip=self._r1["SKIP"],
            round1_uncertain=self._r1["UNCERTAIN"],
            context_complete=self._context["COMPLETE"],
            context_partial=self._context["PARTIAL"],
            context_failed=self._context["FAILED"],
            context_truncated=self._context_truncated,
            round2_analyze=self._r2["ANALYZE"],
            round2_skip=self._r2["SKIP"],
            round2_forced=self._r2_forced,
            final_analyze=self._final["ANALYZE"],
            final_skip=self._final["SKIP"],
            stage_complete=self._stage_complete,
        )

    @property
    def achievements(self) -> AchievementState:
        retained = self._final["ANALYZE"] if self._stage_complete else self._r1["ANALYZE"] + self._r2["ANALYZE"]
        excluded = self._final["SKIP"] if self._stage_complete else self._r1["SKIP"] + self._r2["SKIP"]
        converged = retained + excluded
        pending = max(0, self._input_total - converged)
        reasons = tuple(
            ReasonCount(reason=reason, label=ANALYZE_REASON_LABELS.get(reason, reason), count=count)
            for reason, count in sorted(self._reasons.items(), key=lambda item: (-item[1], item[0]))
            if count > 0
        )
        return AchievementState(
            completed_stages=self._completed_stages(),
            converged=converged,
            retained=retained,
            excluded=excluded,
            pending=pending,
            reason_counts=reasons,
            highlights=tuple(self._highlights),
            conclusion_count=converged,
        )

    def _apply_task(self, item: AgentEvent, verdicts: list[dict[str, Any]], data: dict[str, Any]) -> None:
        key = item.task_id or (item.refs[0] if item.refs else "task")
        if item.round == 1:
            if key in self._seen_r1_tasks:
                return
            self._seen_r1_tasks.add(key)
            target = self._r1
        elif item.round == 2:
            if key in self._seen_r2_tasks:
                return
            self._seen_r2_tasks.add(key)
            target = self._r2
        else:
            return

        for verdict in verdicts:
            decision = str(verdict.get("decision") or "")
            if decision in target:
                target[decision] += 1
            if decision == "ANALYZE":
                reason = str(verdict.get("reason") or "other")
                self._reasons[reason] += 1
                self._capture_highlight(verdict)
        if item.round == 2 and bool(data.get("forced_analyze")):
            self._r2_forced += 1

    def _apply_context(self, item: AgentEvent) -> None:
        key = item.refs[0] if item.refs else (item.task_id or "context")
        if key in self._seen_contexts:
            return
        self._seen_contexts.add(key)
        status = str(item.data.get("status") or "")
        if status in self._context:
            self._context[status] += 1
        if bool(item.data.get("truncated")):
            self._context_truncated += 1

    def _capture_highlight(self, verdict: dict[str, Any]) -> None:
        ref = str(verdict.get("ref") or "")
        note = strip_terminal_punctuation(compact(str(verdict.get("note") or ""), 96))
        if not ref or not note or not contains_cjk(note) or ref in self._highlight_refs:
            return
        if len(self._highlights) == self._highlights.maxlen and self._highlights:
            self._highlight_refs.discard(self._highlights[0].ref)
        self._highlights.append(AchievementHighlight(ref=ref, text=note))
        self._highlight_refs.add(ref)

    def _completed_stages(self) -> tuple[StageMilestone, ...]:
        stages: list[StageMilestone] = []
        if self._r1_completed:
            stages.append(
                StageMilestone(
                    key="round1",
                    label="第一轮分诊",
                    summary="%d 保留 / %d 排除 / %d 待补" % (
                        self._r1["ANALYZE"], self._r1["SKIP"], self._r1["UNCERTAIN"]
                    ),
                )
            )
        if self._context_completed:
            stages.append(
                StageMilestone(
                    key="second_hop",
                    label="上下文补料",
                    summary="%d 完整 / %d 部分 / %d 失败%s" % (
                        self._context["COMPLETE"],
                        self._context["PARTIAL"],
                        self._context["FAILED"],
                        " / %d 截断" % self._context_truncated if self._context_truncated else "",
                    ),
                )
            )
        if self._r2_completed:
            stages.append(
                StageMilestone(
                    key="round2",
                    label="第二轮判断",
                    summary="%d 保留 / %d 排除%s" % (
                        self._r2["ANALYZE"],
                        self._r2["SKIP"],
                        " / %d 技术兜底" % self._r2_forced if self._r2_forced else "",
                    ),
                )
            )
        if self._stage_complete:
            stages.append(
                StageMilestone(
                    key="stage2",
                    label="第二阶段",
                    summary="已完整收敛 · 最终保留 %d / 排除 %d" % (self._final["ANALYZE"], self._final["SKIP"]),
                )
            )
        return tuple(stages)

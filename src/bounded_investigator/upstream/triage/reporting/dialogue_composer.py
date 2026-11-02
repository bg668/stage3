"""Deterministic AgentEvent -> presentation composition.

The composer may reorganize and shorten facts for presentation, but it must never
invent analysis steps, evidence, or causal links that are absent from AgentEvent.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .achievement_tracker import AchievementTracker
from .active_task_slots import ActiveTaskSlotAllocator
from .events import AgentEvent
from .presentation_model import AchievementState, ActiveTaskView, ProductionStats, TaskBubble, WorkState
from .reason_text import compact as _compact, display_verdict_reason as _display_reason
from .report_lifecycle import ReportOperation, ReportUpdate
from .step_labels import StepLabelResolver


@dataclass(frozen=True)
class DialogueMessage:
    """Short stage-level report before display sequencing."""

    actor: str
    text: str
    importance: str = "normal"
    timestamp: str = ""
    step_name: str = ""


@dataclass(frozen=True)
class DialogueUpdate:
    messages: tuple[DialogueMessage, ...] = ()
    report_updates: tuple[ReportUpdate, ...] = ()
    status: str | None = None
    work: WorkState | None = None
    # Legacy compatibility/debug view.  Runtime presentation uses report_updates.
    task_bubble: TaskBubble | None = None
    stats: ProductionStats | None = None
    achievements: AchievementState | None = None


class DialogueComposer:
    """Maintain presentation-only context and compose concise factual updates."""

    def __init__(self) -> None:
        self._active_tasks: dict[str, dict[str, Any]] = {}
        self._active_slots = ActiveTaskSlotAllocator(capacity=3)
        self._max_concurrency = 3
        self._round_completed: dict[int, tuple[int, int]] = {}
        self._round_totals: dict[int, int] = {}
        self._phase = "第二阶段分诊"
        self._phase_note: str | None = None
        self._actor = "员工1号"
        self._achievement_tracker = AchievementTracker()
        self._step_labels = StepLabelResolver()

    def compose(self, item: AgentEvent) -> DialogueUpdate:
        self._actor = item.actor
        data = item.data
        self._achievement_tracker.apply(item)

        if item.event == "provider_probe_started":
            profile = str(data.get("model_profile") or "模型")
            model_id = str(data.get("model_id") or "")
            timeout = float(data.get("timeout_seconds", 0) or 0)
            self._phase = "模型服务检查"
            self._phase_note = "%s · %s" % (profile, model_id) if model_id else profile
            lines = (
                "正在检查 %s 的模型服务…" % profile,
                "等待 Provider 响应，超时上限 %.1f 秒。" % timeout,
            )
            update = ReportUpdate(
                operation=ReportOperation.OPEN,
                key="provider-health:round%d" % (item.round or 0),
                timestamp=item.timestamp,
                actor=item.actor,
                lines=lines,
                step_name=self._step_labels.resolve(item),
                importance="important",
            )
            return self._update(report_updates=(update,))

        if item.event == "provider_probe_completed":
            profile = str(data.get("model_profile") or "模型")
            model_id = str(data.get("model_id") or "")
            status = str(data.get("status") or "ERROR")
            latency_ms = int(data.get("latency_ms", 0) or 0)
            error = _compact(str(data.get("error") or ""), 120)
            self._phase = "模型服务检查"
            if status == "OK":
                lines = [
                    "处理结果：%s 模型服务可用。" % profile,
                    "依据：%s 已正常返回，探测耗时 %d ms。" % (model_id or "Provider", latency_ms),
                ]
                lines.append(
                    "下一步：%s。"
                    % ("继续检查第二轮模型" if item.round == 1 else "进入第二阶段任务准备")
                )
                importance = "important"
            elif status == "TIMEOUT":
                lines = [
                    "处理结果：%s 模型服务无响应。" % profile,
                    "依据：%s" % (error or "Provider 探测请求超时。"),
                    "下一步：停止第二阶段，请检查 Provider 服务、网络或模型配置。",
                ]
                importance = "critical"
            else:
                lines = [
                    "处理结果：%s 模型服务不可用。" % profile,
                    "依据：%s" % (error or "Provider 探测失败。"),
                    "下一步：停止第二阶段，请检查 Provider 服务和模型配置。",
                ]
                importance = "critical"
            update = ReportUpdate(
                operation=ReportOperation.COMPLETE,
                key="provider-health:round%d" % (item.round or 0),
                timestamp=item.timestamp,
                actor=item.actor,
                lines=tuple(lines),
                step_name=self._step_labels.resolve(item),
                importance=importance,
            )
            return self._update(report_updates=(update,))

        if item.event == "provider_degraded":
            profile = str(data.get("model_profile") or "模型")
            error = _compact(str(data.get("error") or ""), 120)
            self._phase = "模型服务恢复"
            self._phase_note = "%s · 暂停新增请求" % profile
            lines = [
                "检测到 %s 暂时不可用，已暂停该 Provider 的新增调用。" % profile,
                "处理方式：并发任务共用一次恢复流程，不消耗各自的业务重试次数。",
            ]
            if error:
                lines.insert(1, "依据：%s" % error)
            update = ReportUpdate(
                operation=ReportOperation.OPEN,
                key="provider-runtime:round%d" % (item.round or 0),
                timestamp=item.timestamp,
                actor=item.actor,
                lines=tuple(lines),
                step_name=self._step_labels.resolve(item),
                importance="critical",
            )
            return self._update(report_updates=(update,))

        if item.event == "provider_recovery_waiting":
            attempt = int(data.get("recovery_attempt", 0) or 0)
            maximum = int(data.get("max_recovery_attempts", 0) or 0)
            delay = float(data.get("delay_seconds", 0) or 0)
            lines = (
                "Provider 恢复中：第 %d/%d 次恢复尝试。" % (attempt, maximum),
                "当前退避 %.1f 秒；其他任务继续等待共享恢复结果。" % delay,
            )
            update = ReportUpdate(
                operation=ReportOperation.UPDATE,
                key="provider-runtime:round%d" % (item.round or 0),
                timestamp=item.timestamp,
                actor=item.actor,
                lines=lines,
                step_name=self._step_labels.resolve(item),
                importance="important",
            )
            return self._update(report_updates=(update,))

        if item.event == "provider_recovered":
            attempt = int(data.get("recovery_attempt", 0) or 0)
            self._phase = "模型服务恢复"
            self._phase_note = "Provider 已恢复"
            lines = (
                "Provider 已恢复，可继续模型调用。",
                "恢复发生在第 %d 次尝试；等待中的任务继续执行。" % attempt,
            )
            update = ReportUpdate(
                operation=ReportOperation.COMPLETE,
                key="provider-runtime:round%d" % (item.round or 0),
                timestamp=item.timestamp,
                actor=item.actor,
                lines=lines,
                step_name=self._step_labels.resolve(item),
                importance="important",
            )
            return self._update(report_updates=(update,))

        if item.event == "provider_recovery_exhausted":
            attempts = int(data.get("attempts", 0) or 0)
            error = _compact(str(data.get("error") or ""), 120)
            self._phase = "模型服务恢复失败"
            self._phase_note = "恢复预算已耗尽"
            lines = (
                "Provider 连续不可用，恢复预算已耗尽（%d 次）。" % attempts,
                "依据：%s" % (error or "Provider 仍不可用。"),
                "下一步：停止 Provider 级重试，由当前轮次的安全失败策略兜底。",
            )
            update = ReportUpdate(
                operation=ReportOperation.COMPLETE,
                key="provider-runtime:round%d" % (item.round or 0),
                timestamp=item.timestamp,
                actor=item.actor,
                lines=lines,
                step_name=self._step_labels.resolve(item),
                importance="critical",
            )
            return self._update(report_updates=(update,))

        if item.event == "stage_started":
            undecided = int(data.get("undecided", 0) or 0)
            self._phase = "第二阶段分诊"
            self._phase_note = "待定函数 %d" % undecided
            return self._update(
                messages=(
                    self._msg(item, "开始第二阶段分诊：%d 个待定函数需要收敛。" % undecided, "important"),
                )
            )

        if item.event == "round_started":
            total = int(data.get("task_total", 0) or 0)
            functions = int(data.get("function_total", 0) or 0)
            concurrency = int(data.get("max_concurrency", 0) or 0)
            if concurrency > 0:
                self._max_concurrency = concurrency
                self._active_slots.set_capacity(concurrency)
            if item.round is not None:
                self._round_totals[item.round] = total
                self._round_completed[item.round] = (0, total)
            if item.round == 1:
                self._phase = "第一轮快速分诊"
                self._phase_note = "%d 个任务 · %d 个函数 · 最多 %d 个模型任务并发" % (
                    total, functions, concurrency
                )
                messages = (self._msg(item, "第一轮开始：%d 个函数，最多 %d 个模型任务并发。" % (functions, concurrency), "important"),)
            else:
                forced = int(data.get("forced_without_model", 0) or 0)
                self._phase = "第二轮补料判断"
                self._phase_note = "%d 个函数需要重新判断 · 最多 %d 个模型任务并发" % (functions, concurrency)
                text = "第二轮开始：%d 个函数进入补料后判断。" % functions
                if forced:
                    text += " %d 个补料失败函数按安全策略直接保留。" % forced
                messages = (self._msg(item, text, "critical" if forced else "important"),)
            return self._update(messages=messages)

        if item.event == "attempt_started":
            key = item.task_id or "attempt:%s" % id(item)
            attempt = int(data.get("attempt", 1) or 1)
            self._active_tasks[key] = {
                "round": item.round,
                "refs": item.refs,
                "unit_id": str(data.get("unit_id") or ""),
                "ref_count": int(data.get("ref_count", len(item.refs)) or len(item.refs)),
                "attempt": attempt,
                "slot": self._active_slots.assign(key),
            }
            # Keep the structured OPEN event for trace/plain compatibility.  Rich no
            # longer renders this as a large active bubble; the fixed current-work
            # slots consume WorkState instead.
            operation = ReportOperation.OPEN if attempt == 1 else ReportOperation.UPDATE
            return self._update(report_updates=(self._processing_report(item, operation=operation, attempt=attempt),))

        if item.event == "attempt_finished":
            key = item.task_id or ""
            self._active_tasks.pop(key, None)
            self._active_slots.release(key)
            return self._update()

        if item.event == "attempt_failed":
            error = _compact(str(data.get("error") or ""), 100)
            status = str(data.get("status") or "")
            action = str(data.get("next_action", "terminal"))
            next_text = {
                "retry": "按既定策略重新调用模型。",
                "split": "拆成单函数任务分别判断。",
                "terminal": "停止重试，按当前轮次的失败策略处理。",
            }.get(action, "按既定失败策略继续处理。")
            if status == "PROVIDER_TIMEOUT":
                lines = ["处理状态：本次模型请求超时，没有收到 Provider 响应。"]
            else:
                lines = ["处理状态：本次模型调用未通过。"]
            if error:
                lines.append("依据：%s" % error)
            lines.append("下一步：%s" % next_text)
            update = self._report(item, ReportOperation.UPDATE, tuple(lines), "critical")
            return self._update(report_updates=(update,))

        if item.event == "task_split":
            child_count = int(data.get("child_count", 0) or 0)
            lines = (
                "处理结果：当前批次连续未通过输出校验。",
                "依据：批量输出无法形成合法、完整的结构化 verdict。",
                "下一步：拆成 %d 个单函数任务分别判断。" % child_count,
            )
            bubble = self._legacy_bubble(item, lines, "拆分", "critical")
            return self._update(
                task_bubble=bubble,
                report_updates=(self._report(item, ReportOperation.COMPLETE, lines, "critical"),),
            )

        if item.event == "task_completed":
            completed = int(data.get("completed", 0) or 0)
            total = int(data.get("total", 0) or 0)
            if item.round is not None:
                self._round_completed[item.round] = (completed, total)
            status = str(data.get("status", "OK"))
            verdicts = data.get("verdicts") or []
            if status != "OK":
                failure = str(data.get("failure_kind") or status)
                error = _compact(str(data.get("error") or ""), 120)
                if failure == "PROVIDER_TIMEOUT":
                    lines = (
                        "处理结果：模型请求超时，当前任务没有形成有效结论。",
                        "依据：%s" % (error or "Provider 在请求时限内没有响应。"),
                        "下一步：按当前轮次的超时失败策略处理。",
                    )
                else:
                    lines = (
                        "处理结果：任务未收敛。",
                        "依据：失败类型为 %s%s。" % (failure, ("；" + error) if error else ""),
                        "下一步：按当前轮次的失败策略处理。",
                    )
                bubble = self._legacy_bubble(item, lines, "失败", "critical")
                return self._update(
                    task_bubble=bubble,
                    report_updates=(self._report(item, ReportOperation.COMPLETE, lines, "critical"),),
                )
            bubble = self._round2_bubble(item, verdicts, data) if item.round == 2 else self._round1_bubble(item, verdicts, data)
            return self._update(
                task_bubble=bubble,
                report_updates=(self._report(item, ReportOperation.COMPLETE, bubble.lines, bubble.importance),),
            )

        if item.event == "round_completed":
            if item.round == 1:
                analyze = int(data.get("analyze", 0) or 0)
                skip = int(data.get("skip", 0) or 0)
                uncertain = int(data.get("uncertain", 0) or 0)
                self._phase = "第一轮完成"
                self._phase_note = "保留 %d · 跳过 %d · 待补上下文 %d" % (analyze, skip, uncertain)
                return self._update(messages=(self._msg(item, "第一轮结果已汇总：%d 个保留，%d 个排除，%d 个需要补上下文。" % (analyze, skip, uncertain), "important"),))
            analyze = int(data.get("analyze", 0) or 0)
            skip = int(data.get("skip", 0) or 0)
            forced = int(data.get("forced", 0) or 0)
            self._phase = "第二轮完成"
            self._phase_note = "保留 %d · 跳过 %d · 技术失败保守保留 %d" % (analyze, skip, forced)
            return self._update(messages=(self._msg(item, "第二轮结果已汇总：%d 个保留，%d 个排除，技术失败兜底 %d 个。" % (analyze, skip, forced), "important"),))

        if item.event == "context_requested":
            # Individual requests are audit facts.  The assembler runs as one deterministic
            # batch today, so claiming a per-function "正在整理" state would be false.
            return self._update()

        if item.event == "context_assembly_started":
            count = int(data.get("subject_total", 0) or 0)
            self._phase = "关联上下文装配"
            self._phase_note = "正在给 %d 个函数补充 second-hop 材料" % count
            update = ReportUpdate(
                operation=ReportOperation.OPEN,
                key="second-hop:assembly",
                timestamp=item.timestamp,
                actor=item.actor,
                lines=("正在整理 %d 个函数的关联上下文…" % count,),
                importance="important",
            )
            return self._update(report_updates=(update,))

        if item.event == "context_assembled":
            # Per-package results continuously feed the left achievement panel.  The right
            # side keeps one truthful batch-level second-hop report because no per-package
            # start event exists in the runtime.
            return self._update()

        if item.event == "context_assembly_completed":
            total = int(data.get("total", 0) or 0)
            complete = int(data.get("complete", 0) or 0)
            partial = int(data.get("partial", 0) or 0)
            failed = int(data.get("failed", 0) or 0)
            truncated = int(data.get("truncated", 0) or 0)
            self._phase = "上下文补料完成"
            self._phase_note = "%d 个函数的补充材料已整理完成" % total
            lines = ["处理结果：%d 个函数的关联上下文已整理。" % total]
            details = ["%d 个完整" % complete, "%d 个部分完整" % partial]
            if failed:
                details.append("%d 个失败" % failed)
            if truncated:
                details.append("%d 个达到预算上限" % truncated)
            lines.append("依据：%s。" % "，".join(details))
            lines.append("下一步：使用现有补充材料进入第二轮判断。")
            update = ReportUpdate(
                operation=ReportOperation.COMPLETE,
                key="second-hop:assembly",
                timestamp=item.timestamp,
                actor=item.actor,
                lines=tuple(lines),
                importance="critical" if failed else "important",
            )
            return self._update(report_updates=(update,))

        if item.event == "stage_completed":
            complete = bool(data.get("complete", True))
            self._phase = "第二阶段完成" if complete else "第二阶段未完整收敛"
            if complete:
                undecided = int(data.get("undecided", 0) or 0)
                stage2_analyze = int(data.get("stage2_analyze", 0) or 0)
                stage2_skip = int(data.get("stage2_skip", 0) or 0)
                self._phase_note = "%d 个待定函数已处理 · 保留 %d · 排除 %d" % (undecided, stage2_analyze, stage2_skip)
                text = "第二阶段结果已收敛：%d 个待定函数完成处理，保留 %d，排除 %d。" % (undecided, stage2_analyze, stage2_skip)
                messages = (self._msg(item, text, "important"),)
            else:
                unresolved = int(data.get("unresolved", 0) or 0)
                self._phase_note = "仍有 %d 个函数因技术失败未完成" % unresolved
                messages = (self._msg(item, "第二阶段未完整收敛：仍有 %d 个函数因技术失败未完成。" % unresolved, "critical"),)
            return self._update(messages=messages)

        return self._update()

    def _update(
        self,
        *,
        messages: tuple[DialogueMessage, ...] = (),
        report_updates: tuple[ReportUpdate, ...] = (),
        task_bubble: TaskBubble | None = None,
    ) -> DialogueUpdate:
        work = self._work_state()
        return DialogueUpdate(
            messages=messages,
            report_updates=report_updates,
            status=self._legacy_status(work),
            work=work,
            task_bubble=task_bubble,
            stats=self._achievement_tracker.stats,
            achievements=self._achievement_tracker.achievements,
        )

    def _work_state(self) -> WorkState:
        round_number = 1 if "第一轮" in self._phase else 2 if "第二轮" in self._phase else None
        active: list[ActiveTaskView] = []
        for key, value in self._active_tasks.items():
            if round_number is not None and value.get("round") != round_number:
                continue
            refs = tuple(value.get("refs") or ())
            ref_count = int(value.get("ref_count", len(refs)) or len(refs))
            round_value = value.get("round")
            if ref_count > 1:
                label = "%d 个函数" % ref_count
            elif len(refs) == 1:
                label = refs[0]
            elif ref_count > 0:
                label = "%d 个函数" % ref_count
            elif refs:
                label = refs[0]
            else:
                label = "模型任务"
            step_name = "第二轮复判" if round_value == 2 else "第一轮分诊" if round_value == 1 else self._phase
            active.append(
                ActiveTaskView(
                    key=key,
                    label=label,
                    slot=int(value.get("slot", 0) or 0),
                    step_name=step_name,
                )
            )
        active.sort(key=lambda item: (item.slot or 999, item.key))
        completed = total = 0
        if round_number is not None:
            completed, total = self._round_completed.get(round_number, (0, self._round_totals.get(round_number, 0)))
        busy = bool(active) or self._phase in ("第一轮快速分诊", "第二轮补料判断", "关联上下文装配")
        return WorkState(
            actor=self._actor,
            phase=self._phase,
            active_tasks=tuple(active[:8]),
            completed=completed,
            total=total,
            note=self._phase_note,
            busy=busy,
            max_concurrency=self._max_concurrency,
        )

    def _processing_report(self, item: AgentEvent, *, operation: ReportOperation, attempt: int) -> ReportUpdate:
        ref_count = int(item.data.get("ref_count", len(item.refs)) or len(item.refs))
        if item.round == 2 and len(item.refs) == 1:
            subject = item.refs[0]
            text = "正在%s处理 %s…" % ("重新" if attempt > 1 else "", subject)
        elif ref_count == 1 and item.refs:
            text = "正在%s处理 %s…" % ("重新" if attempt > 1 else "", item.refs[0])
        else:
            text = "正在%s处理一批 %d 个函数…" % ("重新" if attempt > 1 else "", ref_count)
        return self._report(item, operation, (text,), "normal")

    def _round1_bubble(self, item: AgentEvent, verdicts: list[dict[str, Any]], data: dict[str, Any]) -> TaskBubble:
        counts = _decision_counts(verdicts)
        total = sum(counts.values())
        result_parts = []
        if counts["ANALYZE"]:
            result_parts.append("%d 个保留" % counts["ANALYZE"])
        if counts["SKIP"]:
            result_parts.append("%d 个排除" % counts["SKIP"])
        if counts["UNCERTAIN"]:
            result_parts.append("%d 个需要补充上下文" % counts["UNCERTAIN"])
        lines = ["处理结果：%d 个函数，%s。" % (total, "，".join(result_parts) if result_parts else "没有形成有效结果")]
        lines.append("依据：%s" % self._round1_basis(verdicts))
        completed = int(data.get("completed", 0) or 0)
        task_total = int(data.get("total", 0) or 0)
        if counts["UNCERTAIN"]:
            next_text = "%d 个待定函数进入关联上下文补料" % counts["UNCERTAIN"]
            if counts["ANALYZE"]:
                next_text += "，保留项进入深入分析队列"
            next_text += "。"
        elif completed and task_total and completed >= task_total:
            next_text = "第一轮任务已全部完成，汇总本轮结果。"
        elif counts["ANALYZE"]:
            next_text = "保留项进入深入分析队列，继续处理剩余第一轮任务。"
        else:
            next_text = "本批结束，继续处理剩余第一轮任务。"
        lines.append("下一步：%s" % next_text)
        importance = "important" if counts["ANALYZE"] or counts["UNCERTAIN"] else "normal"
        unit_id = str(data.get("unit_id") or self._task_title(item))
        return TaskBubble(
            key=item.task_id or unit_id,
            title="%s · 第一轮完成" % unit_id,
            lines=tuple(lines),
            outcome="完成",
            importance=importance,
            timestamp=item.timestamp,
            actor=item.actor,
        )

    def _round2_bubble(self, item: AgentEvent, verdicts: list[dict[str, Any]], data: dict[str, Any]) -> TaskBubble:
        if not verdicts:
            lines = ("处理结果：没有形成有效 verdict。", "下一步：按失败策略处理。")
            return self._legacy_bubble(item, lines, "异常", "critical")
        verdict = verdicts[0]
        ref = str(verdict.get("ref", self._subject(item)))
        decision = str(verdict.get("decision", "?"))
        forced = bool(data.get("forced_analyze"))
        if forced:
            result = "保留（技术失败兜底）"
            importance = "critical"
        elif decision == "ANALYZE":
            result = "保留"
            importance = "important"
        else:
            result = "排除"
            importance = "normal"
        lines = ["处理结果：%s。" % result]
        reason = _display_reason(verdict)
        lines.append("依据：%s。" % (reason or "结构化 verdict 已完成"))
        completed = int(data.get("completed", 0) or 0)
        total = int(data.get("total", 0) or 0)
        if decision == "ANALYZE" or forced:
            next_text = "加入深入分析队列。"
        elif completed and total and completed >= total:
            next_text = "第二轮任务已全部完成，汇总本轮结果。"
        else:
            next_text = "不进入深入分析，继续处理剩余第二轮任务。"
        lines.append("下一步：%s" % next_text)
        return TaskBubble(
            key=item.task_id or ref,
            title="%s · 第二轮完成" % ref,
            lines=tuple(lines),
            outcome=result,
            importance=importance,
            timestamp=item.timestamp,
            actor=item.actor,
        )

    def _round1_basis(self, verdicts: list[dict[str, Any]]) -> str:
        notable: list[str] = []
        for verdict in verdicts:
            decision = str(verdict.get("decision", ""))
            if decision == "SKIP":
                continue
            ref = str(verdict.get("ref", "?"))
            reason = _display_reason(verdict)
            if reason:
                notable.append("%s：%s" % (ref, reason))
            if len(notable) >= 3:
                break
        if notable:
            suffix = "；另有其他结果已计入左侧成果。" if sum(1 for v in verdicts if str(v.get("decision")) != "SKIP") > len(notable) else "。"
            return "；".join(notable) + suffix

        skip_reasons = Counter(str(v.get("reason") or "") for v in verdicts if str(v.get("decision")) == "SKIP")
        parts: list[str] = []
        for reason, count in skip_reasons.most_common(2):
            text = _display_reason({"reason": reason, "note": ""})
            if text:
                parts.append("%d 个%s" % (count, text))
        return ("；".join(parts) + "。") if parts else "本批 verdict 均已形成。"

    def _report(
        self,
        item: AgentEvent,
        operation: ReportOperation,
        lines: tuple[str, ...],
        importance: str,
    ) -> ReportUpdate:
        return ReportUpdate(
            operation=operation,
            key=item.task_id or self._subject(item),
            timestamp=item.timestamp,
            actor=item.actor,
            lines=lines,
            step_name=self._step_labels.resolve(item),
            importance=importance,
        )

    def _legacy_bubble(self, item: AgentEvent, lines: tuple[str, ...], outcome: str, importance: str) -> TaskBubble:
        return TaskBubble(
            key=item.task_id or self._task_title(item),
            title=self._task_title(item),
            lines=lines,
            outcome=outcome,
            importance=importance,
            timestamp=item.timestamp,
            actor=item.actor,
        )

    def _msg(self, item: AgentEvent, text: str, importance: str = "normal") -> DialogueMessage:
        return DialogueMessage(
            actor=item.actor,
            text=text,
            importance=importance,
            timestamp=item.timestamp,
            step_name=self._step_labels.resolve(item),
        )

    def _subject(self, item: AgentEvent) -> str:
        if item.refs:
            return item.refs[0]
        return str(item.data.get("unit_id") or item.task_id or "这个任务")

    def _task_title(self, item: AgentEvent) -> str:
        unit_id = str(item.data.get("unit_id") or "")
        if unit_id:
            return unit_id
        if item.round == 2 and item.refs:
            return item.refs[0]
        return item.task_id or self._subject(item)

    @staticmethod
    def _legacy_status(work: WorkState) -> str:
        active_count = len(work.active_tasks)
        progress = ""
        if work.total:
            progress = " · 完成 %d/%d" % (work.completed, work.total)
        if active_count:
            return "%s · %d 个模型任务处理中%s" % (work.phase, active_count, progress)
        return "%s%s" % (work.phase, progress)


def _decision_counts(verdicts: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"ANALYZE": 0, "SKIP": 0, "UNCERTAIN": 0}
    for verdict in verdicts:
        decision = str(verdict.get("decision", ""))
        if decision in counts:
            counts[decision] += 1
    return counts

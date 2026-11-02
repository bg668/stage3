"""Deterministic stage/event -> short user-facing step labels."""

from __future__ import annotations

from .events import AgentEvent


class StepLabelResolver:
    """Resolve a compact display step without inspecting composed prose."""

    def resolve(self, event: AgentEvent) -> str:
        if event.stage == "provider_health":
            return "模型服务检查"
        if event.stage == "provider_runtime":
            return "模型服务恢复"
        if event.stage == "round1":
            return "第一轮分诊"
        if event.stage == "second_hop":
            return "上下文补料"
        if event.stage == "round2":
            return "第二轮复判"
        if event.event == "stage_completed":
            return "结果收敛"
        if event.stage == "stage2":
            return "任务准备"
        return "第二阶段"

"""Deterministic task construction for both triage rounds."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .domain import ROUND1_TEMPLATE_ID, ROUND2_TEMPLATE_ID, TriageTask
from .input import TriageInput
from .second_hop_domain import SecondHopPackage
from .second_hop_render import render_second_hop_package

_REF_LINE_RE = re.compile(r"^\[(F-\d+)\]\s", re.MULTILINE)
_UNIT_LINE_RE = re.compile(r"^# unit:.*$", re.MULTILINE)


@dataclass(frozen=True)
class TriageRequest:
    task: TriageTask
    input_text: str


@dataclass(frozen=True)
class ForcedRound2Seed:
    """A Round-2 subject that must conservatively ANALYZE without an LLM call."""

    task: TriageTask
    original_reason: str
    failure_kind: str
    error: str


@dataclass(frozen=True)
class Round2Plan:
    requests: list[TriageRequest]
    forced: list[ForcedRound2Seed]
    subject_order: list[str]

    @property
    def total(self) -> int:
        return len(self.subject_order)


def build_round1_requests(
    snapshot: TriageInput, template_id: str = ROUND1_TEMPLATE_ID
) -> list[TriageRequest]:
    requests: list[TriageRequest] = []
    for unit in snapshot.units:
        input_sha256 = sha256_text(unit.text)
        task_id = _task_id(
            unit_id=unit.unit_id,
            round_number=1,
            template_id=template_id,
            input_sha256=input_sha256,
        )
        task = TriageTask(
            task_id=task_id,
            unit_id=unit.unit_id,
            round=1,
            subject_refs=list(unit.subject_refs),
            input_sha256=input_sha256,
            template_id=template_id,
        )
        requests.append(TriageRequest(task=task, input_text=unit.text))
    return requests


def build_round2_plan(
    packages: list[SecondHopPackage],
    round1_tasks: list[TriageTask],
    template_id: str = ROUND2_TEMPLATE_ID,
) -> Round2Plan:
    """Build one deterministic Round-2 task per Round-1 UNCERTAIN subject."""
    parents: dict[str, tuple[str, str]] = {}
    for task in round1_tasks:
        if task.round != 1 or task.status != "OK":
            continue
        for verdict in task.verdicts:
            if verdict.decision != "UNCERTAIN":
                continue
            if verdict.ref in parents:
                raise ValueError("Round1 UNCERTAIN ref 重复: %s" % verdict.ref)
            parents[verdict.ref] = (task.task_id, verdict.reason)

    requests: list[TriageRequest] = []
    forced: list[ForcedRound2Seed] = []
    subject_order: list[str] = []
    seen: set[str] = set()
    for package in packages:
        ref = package.subject_ref
        if ref in seen:
            raise ValueError("second-hop package ref 重复: %s" % ref)
        seen.add(ref)
        subject_order.append(ref)
        parent = parents.get(ref)
        if parent is None:
            raise ValueError("second-hop package 没有对应 Round1 UNCERTAIN: %s" % ref)
        parent_task_id, original_reason = parent
        input_text = render_second_hop_package(package)
        input_sha256 = sha256_text(input_text)
        unit_id = "R2:%s" % ref
        task = TriageTask(
            task_id=_task_id(
                unit_id=unit_id,
                round_number=2,
                template_id=template_id,
                input_sha256=input_sha256,
            ),
            parent_task_id=parent_task_id,
            unit_id=unit_id,
            round=2,
            subject_refs=[ref],
            input_sha256=input_sha256,
            template_id=template_id,
        )
        if package.status == "FAILED":
            forced.append(
                ForcedRound2Seed(
                    task=task,
                    original_reason=original_reason,
                    failure_kind="ASSEMBLY_FAILED",
                    error=package.error or "second-hop assembly failed",
                )
            )
        else:
            requests.append(TriageRequest(task=task, input_text=input_text))

    missing = [ref for ref in parents if ref not in seen]
    if missing:
        raise ValueError("Round1 UNCERTAIN 缺少 second-hop package: %s" % missing)
    return Round2Plan(requests=requests, forced=forced, subject_order=subject_order)


def build_split_requests(request: TriageRequest) -> list[TriageRequest]:
    """Split one validated stage-1 unit into deterministic single-subject requests."""
    matches = list(_REF_LINE_RE.finditer(request.input_text))
    refs = [match.group(1) for match in matches]
    if refs != request.task.subject_refs:
        raise ValueError(
            "split 输入 marker 与 subject_refs 不一致: markers=%s subjects=%s"
            % (refs, request.task.subject_refs)
        )
    if not matches:
        raise ValueError("split 输入没有 [F-xxxx] marker")

    preamble = request.input_text[: matches[0].start()]
    children: list[TriageRequest] = []
    for index, match in enumerate(matches):
        ref = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(request.input_text)
        block = request.input_text[match.start() : end].rstrip() + "\n"
        child_preamble = _UNIT_LINE_RE.sub(
            "# unit: %s split-fallback (%s, 1 个函数)" % (request.task.unit_id, ref),
            preamble,
            count=1,
        )
        child_text = child_preamble.rstrip() + "\n\n" + block
        input_sha256 = sha256_text(child_text)
        child_id = _split_task_id(
            parent_task_id=request.task.task_id,
            ref=ref,
            template_id=request.task.template_id,
            input_sha256=input_sha256,
        )
        child = TriageTask(
            task_id=child_id,
            parent_task_id=request.task.task_id,
            unit_id=request.task.unit_id,
            round=request.task.round,
            subject_refs=[ref],
            input_sha256=input_sha256,
            template_id=request.task.template_id,
        )
        children.append(TriageRequest(task=child, input_text=child_text))
    return children


def build_retry_request(request: TriageRequest, *, failure_kind: str, error: str) -> TriageRequest:
    """Append a deterministic correction block for schema/ID retry attempts."""
    detail = " ".join((error or "").split())[:600]
    if request.task.round == 2:
        correction = (
            "\n\n[RETRY_CORRECTION]\n"
            "上一轮 Round2 输出未通过程序校验。失败类型: %s。\n"
            "校验信息: %s\n"
            "请只判断 [SUBJECT]，重新输出该 ref 恰好一条 verdict；"
            "decision 只能是 ANALYZE 或 SKIP，不能输出 UNCERTAIN。\n"
            % (failure_kind, detail or "无")
        )
    else:
        correction = (
            "\n\n[RETRY_CORRECTION]\n"
            "上一轮输出未通过程序校验。失败类型: %s。\n"
            "校验信息: %s\n"
            "请重新判断当前所有 SUBJECT，并输出每个 ref 恰好一条完整 verdict；"
            "不要遗漏、不要增加 ref。\n"
            % (failure_kind, detail or "无")
        )
    return TriageRequest(task=request.task, input_text=request.input_text.rstrip() + correction)


def _task_id(*, unit_id: str, round_number: int, template_id: str, input_sha256: str) -> str:
    raw = "%s|%d|%s|%s" % (unit_id, round_number, template_id, input_sha256)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _split_task_id(*, parent_task_id: str, ref: str, template_id: str, input_sha256: str) -> str:
    raw = "%s|split|%s|%s|%s" % (parent_task_id, ref, template_id, input_sha256)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

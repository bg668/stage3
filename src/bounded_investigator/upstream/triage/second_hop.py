"""V4 deterministic second-hop material assembler (zero LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .domain import TriageTask
from .input import TriageInput
from .second_hop_domain import RelatedFunctionMaterial, SecondHopPackage
from .second_hop_recipes import build_recipe


@dataclass(frozen=True)
class SecondHopBudget:
    """Supplement budget. SUBJECT full code is intentionally excluded."""

    max_chars: int = 48_000
    max_related_functions: int = 8
    max_code_chars_per_related_function: int = 12_000

    def __post_init__(self) -> None:
        if self.max_chars < 1:
            raise ValueError("second_hop max_chars 必须 >= 1")
        if self.max_related_functions < 1:
            raise ValueError("second_hop max_related_functions 必须 >= 1")
        if self.max_code_chars_per_related_function < 1:
            raise ValueError("second_hop max_code_chars_per_related_function 必须 >= 1")


def assemble_second_hop_packages(
    snapshot: TriageInput,
    tasks: list[TriageTask],
    budget: SecondHopBudget,
) -> list[SecondHopPackage]:
    requests: list[tuple[str, str]] = []
    seen: set[str] = set()
    for task in tasks:
        if task.round != 1 or task.status != "OK":
            continue
        for verdict in task.verdicts:
            if verdict.decision != "UNCERTAIN" or verdict.ref in seen:
                continue
            seen.add(verdict.ref)
            requests.append((verdict.ref, verdict.reason))

    packages: list[SecondHopPackage] = []
    for ref, reason in requests:
        try:
            packages.append(assemble_second_hop_package(snapshot, ref, reason, budget))
        except Exception as exc:
            fn = snapshot.functions_by_ref.get(ref)
            packages.append(
                SecondHopPackage(
                    subject_ref=ref,
                    reason=reason,
                    status="FAILED",
                    subject_name=fn.name if fn else "?",
                    subject_address=fn.address if fn else "?",
                    subject_code="",
                    error="%s: %s" % (type(exc).__name__, exc),
                )
            )
    return packages


def assemble_second_hop_package(
    snapshot: TriageInput,
    subject_ref: str,
    reason: str,
    budget: SecondHopBudget,
) -> SecondHopPackage:
    fn = snapshot.functions_by_ref[subject_ref]
    subject_code = _read_subject_code(snapshot.code_dir / (subject_ref + ".txt"))
    plan = build_recipe(snapshot, subject_ref, reason)

    selected_specs = list(plan.related_functions[: budget.max_related_functions])
    omitted_refs = [item.ref for item in plan.related_functions[budget.max_related_functions :]]
    remaining = budget.max_chars
    materials: list[RelatedFunctionMaterial] = []
    truncated = bool(omitted_refs)

    for spec in selected_specs:
        related_fn = snapshot.functions_by_ref[spec.ref]
        code_path = snapshot.code_dir / (spec.ref + ".txt")
        status, raw_code = _read_related_code(code_path)
        original_chars = len(raw_code)
        code = raw_code
        code_truncated = False

        if status == "OK":
            per_function_limit = min(budget.max_code_chars_per_related_function, remaining)
            if per_function_limit <= 0:
                status = "OMITTED_BY_BUDGET"
                code = ""
                code_truncated = original_chars > 0
            elif len(code) > per_function_limit:
                code = code[:per_function_limit]
                code_truncated = True
            remaining -= len(code)
            if code_truncated:
                truncated = True
        materials.append(
            RelatedFunctionMaterial(
                ref=spec.ref,
                name=related_fn.name,
                address=related_fn.address,
                traces=spec.traces,
                code_status=status,
                code=code,
                code_truncated=code_truncated,
                original_code_chars=original_chars,
            )
        )

    supplement_code_chars = sum(len(item.code) for item in materials)
    missing = list(plan.missing_sections)
    # Missing related code is explicit partial data, not an assembler failure.
    for item in materials:
        if item.code_status in ("NOT_FOUND", "READ_ERROR", "EMPTY"):
            missing.append("code:%s" % item.ref)

    return SecondHopPackage(
        subject_ref=subject_ref,
        reason=reason,
        status="PARTIAL" if missing else "COMPLETE",
        subject_name=fn.name,
        subject_address=fn.address,
        subject_code=subject_code,
        facts=list(plan.facts),
        related_functions=materials,
        missing_sections=_stable_unique(missing),
        truncated=truncated,
        omitted_related_refs=omitted_refs,
        supplement_code_chars=supplement_code_chars,
    )


def _read_subject_code(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError("SUBJECT 完整代码不存在: %s" % path)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise OSError("读取 SUBJECT 完整代码失败 %s: %s" % (path, exc)) from exc


def _read_related_code(path: Path) -> tuple[str, str]:
    if not path.is_file():
        return "NOT_FOUND", ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "READ_ERROR", ""
    if not text:
        return "EMPTY", ""
    return "OK", text


def _stable_unique(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result

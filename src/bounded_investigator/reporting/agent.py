"""The report-only role and its deterministically loaded HTML capability."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING

import yaml
from pydantic_ai.usage import RunUsage, UsageLimits

from ..config import AppConfig
from ..evidence.inventory import InputInventory
from ..execution.llm import build_stage_agent, build_text_stage_agent, stage_instructions
from ..execution.stage_runner import RepairRuntime, StageOutcome, StageRunner
from ..logging_utils import get_logger
from ..prompts import format_repair_instructions
from ..schemas import DeliveryStatus, GlobalReconciliationResult
from ..execution.usage_utils import usage_delta, usage_summary
from .composition import (ReportComposition, ReportValidationError, build_composition_input,
                          collect_composition_issues, validate_composition)
from .revision import ReportRevisionPatch, apply_revision_patch, build_revision_input


if TYPE_CHECKING:
    from .html_skill import HtmlReportSkill


_SKILL_REFERENCE = re.compile(r'\{\{skill:([a-z0-9]+(?:-[a-z0-9]+)*)\}\}')


def _role_markdown(text: str) -> tuple[str, str]:
    lines = text.splitlines()
    if not lines or lines[0] != '---':
        raise ValueError('报告角色必须包含 YAML frontmatter')
    try:
        end = lines.index('---', 1)
        metadata = yaml.safe_load('\n'.join(lines[1:end]))
    except (ValueError, yaml.YAMLError) as exc:
        raise ValueError('报告角色 frontmatter 无效') from exc
    version = metadata.get('metadata', {}).get('version') if isinstance(metadata, dict) \
        and isinstance(metadata.get('metadata'), dict) else None
    if not isinstance(metadata, dict) or metadata.get('name') != 'report' \
            or not isinstance(version, str) or not version.strip():
        raise ValueError('报告角色必须声明 name: report 和 metadata.version')
    body = '\n'.join(lines[end + 1:]).strip()
    if not body:
        raise ValueError('报告角色指令不能为空')
    return version, body


def assemble_report_instructions(role_text: str, skill: HtmlReportSkill) -> str:
    """Expand exactly one explicit report skill, without touching A/R/R harnesses."""
    _, body = _role_markdown(role_text)
    if skill.name != 'malware-report-html' or 'report' not in skill.roles:
        raise ValueError('HTML skill 必须匹配 malware-report-html 并归属 report 角色')
    expanded = []
    used = False
    for line in body.split('\n'):
        if '{{skill:' not in line:
            expanded.append(line)
            continue
        reference = _SKILL_REFERENCE.fullmatch(line)
        if reference is None or reference.group(1) != skill.name:
            raise ValueError('报告角色必须在独占行引用现有 HTML skill')
        if used:
            raise ValueError('报告角色不得重复引用 HTML skill')
        instructions = skill.instructions()
        if not isinstance(instructions, str) or not instructions.strip():
            raise ValueError('HTML skill 指令不能为空')
        if '{{skill:' in instructions:
            raise ValueError('HTML skill 不支持嵌套引用')
        expanded.append(instructions)
        used = True
    if not used:
        raise ValueError('报告角色缺少 HTML skill 引用')
    return '\n'.join(expanded)


class ReportAgent:
    """Load the role locally; create model clients only when composition starts."""

    def __init__(self, config: AppConfig, skill: HtmlReportSkill):
        self.config = config
        self.skill = skill
        role_text = files('bounded_investigator.reporting').joinpath('agents', 'report.md').read_text(encoding='utf-8')
        version, _ = _role_markdown(role_text)
        self.expanded_instructions = assemble_report_instructions(role_text, skill)
        self.instructions = self.expanded_instructions
        if 'report_composition' in config.stages:
            _, model_config, _ = config.stage('report_composition')
            self.instructions = stage_instructions(model_config, self.expanded_instructions)
        snapshot = {'name': 'report', 'version': version, 'role': role_text,
                    'skill': skill.snapshot(), 'expanded_instructions': self.expanded_instructions,
                    'instructions': self.instructions}
        fingerprint = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True,
                                                separators=(',', ':')).encode('utf-8')).hexdigest()
        self._role_snapshot = {**snapshot, 'fingerprint': fingerprint}
        self.last_outcome: StageOutcome | None = None
        self.revision_status = {'triggered': False, 'status': 'not_needed', 'rounds': 0}
        self.initial_issues: list[dict] = []
        self.final_issues: list[dict] = []
        self.usage_breakdown: dict = {}
        self.artifacts: dict[str, str] = {}

    @property
    def role_snapshot(self) -> dict:
        return deepcopy(self._role_snapshot)

    async def compose(
        self, *, facts: GlobalReconciliationResult, projection: dict,
        inventory: InputInventory | None = None, packet_name: str,
        artifact_dir: Path, usage: RunUsage, repair_runtime: RepairRuntime,
    ) -> ReportComposition:
        """Compose once, then allow one scoped semantic revision within shared limits."""
        self.last_outcome = None
        self.revision_status = {'triggered': False, 'status': 'not_needed', 'rounds': 0}
        self.initial_issues, self.final_issues, self.usage_breakdown = [], [], {}
        self.artifacts = {}
        artifact_dir = Path(artifact_dir)
        artifact_dir.mkdir(parents=True, exist_ok=True)

        def save(path: Path, value) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            relative = path.relative_to(artifact_dir)
            self.artifacts[relative.with_suffix('').as_posix().replace('/', '_')] = relative.as_posix()

        async def execute(schema, prompt, directory, limit, label):
            stage = build_stage_agent(config=self.config, stage='report_composition', schema=schema,
                instructions=self.expanded_instructions, output_retries=0)
            before = usage_summary(usage)
            try:
                outcome = await runner.run(stage_agent=stage, prompt=prompt, packet_name=packet_name,
                    usage=usage, limits=UsageLimits(request_limit=limit,
                        total_tokens_limit=self.config.html_report.total_tokens_limit),
                    repair_runtime=repair_runtime, artifact_dir=directory)
                self.last_outcome = outcome
                if outcome.output is not None:
                    self.artifacts[label] = (directory / 'report_composition' / 'validated_result.json').relative_to(artifact_dir).as_posix()
                if outcome.status not in {DeliveryStatus.SUCCESS, DeliveryStatus.SUCCESS_AFTER_REPAIR} \
                        or not isinstance(outcome.output, schema):
                    raise ValueError(f'Report composition did not produce a complete result: {outcome.status.value}')
                return outcome.output
            finally:
                self.usage_breakdown[label] = usage_delta(before, usage_summary(usage)).model_dump(mode='json')

        try:
            save(artifact_dir / 'report_agent_snapshot.json', self._role_snapshot)
            composition_input = build_composition_input(facts, projection, inventory)
            save(artifact_dir / 'composition_input.json', composition_input)
            if 'report_composition' not in self.config.stages:
                raise ValueError('Missing stages.report_composition; configure the report model')
            repair = build_text_stage_agent(config=self.config, stage='format_repair',
                instructions=format_repair_instructions()) if self.config.format_repair.enabled else None
            runner = StageRunner(config=self.config, format_repair=repair,
                log=get_logger('report_agent'), repair_limits=UsageLimits(
                    request_limit=self.config.format_repair.max_attempts,
                    total_tokens_limit=self.config.format_repair.total_tokens_limit))
            async with asyncio.timeout(self.config.html_report.wall_clock_seconds):
                document = await execute(ReportComposition,
                    'mode=draft. 依据只读事实生成完整的结构化分析报告 JSON：\n' + json.dumps(composition_input, ensure_ascii=False),
                    artifact_dir, 1, 'draft')
                issues = collect_composition_issues(document, facts, projection)
                self.initial_issues = [i.model_dump(mode='json') for i in issues]
                self.final_issues = self.initial_issues
                save(artifact_dir / 'validation_initial.json', {'issues': self.initial_issues})
                if not issues:
                    validate_composition(document, facts, projection)
                    save(artifact_dir / 'validation_final.json', {'issues': []})
                    return document
                self.revision_status.update(triggered=True, issue_codes=sorted({i.code for i in issues}))
                if self.config.html_report.max_model_requests == 1:
                    self.revision_status['status'] = 'disabled'
                    raise ReportValidationError(issues)
                if usage.requests >= self.config.html_report.max_model_requests \
                        or usage.total_tokens >= self.config.html_report.total_tokens_limit:
                    self.revision_status['status'] = 'skipped_budget'
                    raise ReportValidationError(issues)
                self.revision_status.update(status='running', rounds=1)
                revision_dir = artifact_dir / 'revision_1'
                revision_input = build_revision_input(document, issues, facts, projection)
                save(revision_dir / 'input.json', revision_input)
                prompt = 'mode=revision. 按修订模式只返回 ReportRevisionPatch JSON：\n' + json.dumps(revision_input, ensure_ascii=False)
                (revision_dir / 'prompt.txt').write_text(prompt, encoding='utf-8')
                patch = await execute(ReportRevisionPatch, prompt, revision_dir,
                    self.config.html_report.max_model_requests, 'revision')
                self.artifacts['revision_patch'] = 'revision_1/report_composition/validated_result.json'
                candidate = apply_revision_patch(document, patch, issues, facts)
                save(revision_dir / 'candidate.json', candidate.model_dump(mode='json'))
                final = collect_composition_issues(candidate, facts, projection)
                self.final_issues = [i.model_dump(mode='json') for i in final]
                save(artifact_dir / 'validation_final.json', {'issues': self.final_issues})
                if final:
                    raise ReportValidationError(final)
                self.revision_status['status'] = 'applied'
                return candidate
        except BaseException as exc:
            if self.revision_status['status'] == 'running':
                self.revision_status['status'] = 'failed'
            try:
                if self.initial_issues and not (artifact_dir / 'validation_final.json').exists():
                    save(artifact_dir / 'validation_final.json', {'status': 'not_run',
                        'issues': self.final_issues, 'error': {'type': type(exc).__name__, 'message': str(exc)}})
                save(artifact_dir / 'report_agent_failure.json',
                     {'type': type(exc).__name__, 'message': str(exc)})
            except OSError as diagnostic_error:
                exc.add_note(f'Cannot save report Agent failure diagnostic: {diagnostic_error}')
            raise

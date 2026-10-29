"""Compose a read-only HTML preview and publish it only after validation."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from pydantic_ai.usage import RunUsage

from ..config import AppConfig
from ..evidence.inventory import InputInventory, build_automatic_scope, build_input_inventory, dump_inventory_json
from ..evidence.scope import EvidenceScope, coverage_warnings, load_evidence_scope, packet_coverage_from_directory
from ..execution.stage_runner import RepairRuntime
from ..execution.usage_utils import merge_usage_summaries, usage_summary
from ..logging_utils import get_logger
from ..schemas import GlobalReconciliationEnvelope, RunEnvelope
from .agent import ReportAgent
from .html_skill import HtmlReportSkill, prepare_context
from .html_template import TEMPLATE_VERSION
from .knowledge_report import project_knowledge
from .material_suggestions import build_material_suggestions


_PUBLISHED = ('_final_report.html', '_html_report.json')
_DOCUMENT = '_report_document.json'


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(f'.{path.name}.{uuid4().hex}.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _save_json(path: Path, value: object) -> None:
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def _input_record(path: Path) -> dict:
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def _clear_published(output_dir: Path) -> None:
    for name in _PUBLISHED:
        (output_dir / name).unlink(missing_ok=True)


def clear_html_outputs(output_dir: Path, *, protected_inputs: list[Path] | None = None) -> None:
    """Remove owned report files before a new run; preserve diagnostics and inputs."""
    paths = {output_dir / name for name in (*_PUBLISHED, _DOCUMENT, '_html_report_manifest.json')}
    reserved = {path.resolve() for path in paths}
    if any(source.resolve() in reserved for source in protected_inputs or ()):
        raise ValueError('Input file must not use an HTML output filename')
    for path in paths:
        path.unlink(missing_ok=True)


def _finish_manifest(manifest: dict, usage: RunUsage, runtime: RepairRuntime,
                     agent: ReportAgent | None = None) -> None:
    core, repair = usage_summary(usage), usage_summary(runtime.usage)
    manifest['usage'] = {'composition': core.model_dump(mode='json'),
                         'format_repair': repair.model_dump(mode='json'),
                         'total': merge_usage_summaries(core, repair).model_dump(mode='json'),
                         'repair_attempts': runtime.attempts}
    if agent is not None:
        manifest['revision'] = agent.revision_status
        manifest['validation'].update(initial_issues=agent.initial_issues, final_issues=agent.final_issues)
        empty = usage_summary(RunUsage()).model_dump(mode='json')
        manifest['usage'].update({name: agent.usage_breakdown.get(name, empty)
                                  for name in ('draft', 'revision')})
        manifest['artifacts']['report_agent_diagnostics'] = {
            key: str(Path(manifest['artifacts']['attempt_dir']) / path) for key, path in agent.artifacts.items()}
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()


def _preflight_context(context: dict | None, manifest: dict, attempt: Path, skill: HtmlReportSkill) -> None:
    """Reject unusable context locally before spending a composition request."""
    prepared, fills = prepare_context(context)
    manifest['test_fills'] = fills
    _save_json(attempt / 'context_preflight.json', prepared)
    # Supply only the generated fields required by the original validator. This
    # disposable skeleton is never given to the model or published as a report.
    candidate = {**prepared, 'schema_version': '1.1', 'findings': [],
                 'summary': ['上下文预检'], 'risk_judgement': '上下文预检',
                 'chain': [], 'sections': [], 'gaps': [], 'final': '上下文预检',
                 'detection': [{'type': 'paragraph', 'text': '上下文预检'}],
                 'response': [{'title': '上下文预检', 'text': '上下文预检'}],
                 'ioc': {'host': [], 'network': [], 'host_empty_reason': '无', 'network_empty_reason': '无'}}
    if isinstance(candidate.get('meta'), dict):
        candidate['meta'] = {**candidate['meta'], 'risk': 'info', 'verdict': '上下文预检'}
        candidate['meta'].setdefault('title', '上下文预检')
    try:
        errors, _warnings = skill.validate(candidate)
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError(f'Invalid HTML context: {exc}') from exc
    manifest['validation']['template_errors'] = errors
    if errors:
        raise ValueError('Invalid HTML context: ' + '; '.join(errors))


def _restore_excerpts(path: Path, projection: dict, manifest: dict) -> None:
    """Reuse saved verbatim excerpts only when their canonical facts still match."""
    manifest['inputs']['saved_report_projection'] = _input_record(path)
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        if any(saved.get(key) != projection[key] for key in ('canonical_claims', 'canonical_artifacts')):
            raise ValueError('canonical facts differ')
        excerpts = saved.get('code_excerpts', {})
        if not isinstance(excerpts, dict):
            raise ValueError('code_excerpts is not an object')
        locations = {c['canonical_id']: {r['location'] for r in c['evidence']}
                     for c in projection['canonical_claims']}
        for cid, records in excerpts.items():
            if cid not in locations or not isinstance(records, list):
                raise ValueError('invalid excerpt owner')
            for record in records:
                if not isinstance(record, dict) or record.get('evidence_location') not in locations[cid] \
                        or not isinstance(record.get('code'), str):
                    raise ValueError('invalid excerpt reference or source text')
        projection['code_excerpts'] = excerpts
        projection['source_excerpts_available'] = bool(excerpts)
    except (ValueError, AttributeError) as exc:
        manifest['warnings'].append(f'Saved report excerpts not used: {exc}')


async def create_html_report(
    *,
    global_result: Path,
    output_dir: Path,
    config: AppConfig,
    context_file: Path | None = None,
    packets_dir: Path | None = None,
    packet_results_dir: Path | None = None,
    packet_paths: list[Path] | None = None,
) -> dict:
    """Compose with one optional revision; never rerun investigation or edit inputs."""
    global_result, output_dir = global_result.resolve(), output_dir.resolve()
    if packets_dir is not None and packet_paths is not None:
        raise ValueError('Supply a packet directory or explicit batch packet paths, not both')
    if packet_paths is not None:
        packet_paths = sorted(path.resolve() for path in packet_paths)
    source_results = packet_results_dir.resolve() if packet_results_dir is not None else global_result.parent
    input_dirs = {global_result.parent, source_results}
    if packets_dir is not None:
        input_dirs.add(packets_dir.resolve())
    input_dirs.update(path.parent for path in packet_paths or [])
    if output_dir in input_dirs:
        raise ValueError('HTML output directory must differ from all input result/material directories')
    reserved = {output_dir / name for name in (*_PUBLISHED, _DOCUMENT, '_html_report_manifest.json')}
    if context_file is not None and context_file.resolve() in reserved:
        raise ValueError('Context input must not use an HTML output filename')

    output_dir.mkdir(parents=True, exist_ok=True)
    attempt = output_dir / '_html_report_artifacts' / uuid4().hex
    attempt.mkdir(parents=True)
    manifest_path = output_dir / '_html_report_manifest.json'
    usage, repair_runtime = RunUsage(), RepairRuntime()
    agent: ReportAgent | None = None
    manifest = {
        'status': 'running', 'preview_only': True, 'template_version': TEMPLATE_VERSION,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'inputs': {'global_result': {'path': str(global_result)}},
        'artifacts': {'attempt_dir': str(attempt.relative_to(output_dir))},
        'test_fills': [], 'validation': {'references': 'not_run', 'template_errors': [], 'template_warnings': []},
        'usage': {}, 'model': None, 'warnings': [],
    }
    clear_html_outputs(output_dir)
    _save_json(manifest_path, manifest)
    try:
        manifest['inputs']['global_result'] = _input_record(global_result)
        envelope = GlobalReconciliationEnvelope.model_validate_json(global_result.read_text(encoding='utf-8'))
        if envelope.result is None or not envelope.provenance_valid:
            raise ValueError('Canonical result is missing or has not passed provenance validation')
        if 'report_composition' not in config.stages:
            raise ValueError('Missing stages.report_composition; configure the HTML composition model')
        stage_cfg, model_cfg, _ = config.stage('report_composition')
        manifest['model'] = {'stage': 'report_composition', 'profile': stage_cfg.model,
                             'provider': model_cfg.provider, 'model_name': model_cfg.model_name,
                             'max_output_tokens': stage_cfg.max_output_tokens}
        skill = HtmlReportSkill()
        manifest['html_skill'] = {key: skill.snapshot()[key] for key in
                                  ('name','version','fingerprint','template_version','template_fingerprint')}
        context = None
        if context_file is not None:
            manifest['inputs']['context_file'] = _input_record(context_file)
            context = json.loads(context_file.read_text(encoding='utf-8'))
            if not isinstance(context, dict):
                raise ValueError('Context file must contain a JSON object')
            _save_json(attempt / 'context_input.json', context)
        _preflight_context(context, manifest, attempt, skill)
        if not source_results.is_dir():
            raise ValueError(f'Packet results directory does not exist: {source_results}')
        manifest['inputs']['packet_results_dir'] = str(source_results)
        result_paths = sorted(source_results.glob('J*.json'))
        manifest['inputs']['packet_results'] = [_input_record(p) for p in result_paths]
        canonical = envelope.result
        packet_coverage = packet_coverage_from_directory(source_results, canonical=canonical)
        inventory = None
        if packets_dir is not None:
            packets_dir = packets_dir.resolve()
            packet_paths = sorted(packets_dir.glob('J*.md'))
            if not packet_paths:
                raise ValueError(f'No J*.md packet files found: {packets_dir}')
            inventory = build_input_inventory(packet_paths)
            completed = {}
            for path in result_paths:
                try:
                    completed[path.stem] = RunEnvelope.model_validate_json(path.read_text(encoding='utf-8'))
                except (ValueError, OSError) as exc:
                    manifest['warnings'].append(f'Cannot read packet result {path.name}: {type(exc).__name__}')
            scope, _requirements = build_automatic_scope(inventory, completed)
            (attempt / 'evidence_inventory.json').write_text(dump_inventory_json(inventory), encoding='utf-8')
        else:
            saved_scope = global_result.parent / '_evidence_scope.json'
            scope = load_evidence_scope(saved_scope) if saved_scope.is_file() else EvidenceScope()
            if saved_scope.is_file():
                manifest['inputs']['evidence_scope'] = _input_record(saved_scope)
            saved_inventory = global_result.parent / '_evidence_inventory.json'
            if saved_inventory.is_file():
                manifest['inputs']['saved_inventory'] = _input_record(saved_inventory)
                try:
                    inventory = InputInventory.model_validate_json(saved_inventory.read_text(encoding='utf-8'))
                except ValueError:
                    manifest['warnings'].append('Saved evidence inventory is invalid; function symbols unavailable')
        if packet_paths is not None:
            manifest['inputs']['packets'] = [_input_record(path) for path in packet_paths]
        projection, audit = project_knowledge(
            canonical, material_suggestions=build_material_suggestions(scope),
            input_coverage_warnings=[item.model_dump(mode='json') for item in coverage_warnings(packet_coverage)],
            source_claim_count=envelope.source_claim_count or None,
            source_artifact_count=envelope.source_artifact_count or None,
            packet_source_dir=packets_dir,
            packet_paths=packet_paths,
        )
        saved_report = global_result.parent / '_report_ready.json'
        if packet_paths is None and saved_report.is_file():
            _restore_excerpts(saved_report, projection, manifest)
        # The existing audit is recorded independently; HTML adds no delivery gate.
        _save_json(attempt / 'knowledge_projection.json', projection)
        _save_json(attempt / 'knowledge_audit.json', audit)
        manifest['validation']['source_audit'] = {
            'passed': audit['passed'], 'issues': audit['issues'],
            'evidence_reference_status': audit['evidence_reference_audit'].get('status', 'completed'),
        }
        agent = ReportAgent(config, skill)
        manifest['report_agent'] = {key: agent.role_snapshot[key] for key in ('name','version','fingerprint')}
        manifest['artifacts']['report_agent_snapshot'] = str((attempt / 'report_agent_snapshot.json').relative_to(output_dir))
        document = await agent.compose(
            facts=canonical, projection=projection, inventory=inventory,
            packet_name=global_result.stem, usage=usage,
            repair_runtime=repair_runtime, artifact_dir=attempt,
        )
        manifest['validation']['stage_status'] = agent.last_outcome.status.value
        manifest['validation']['references'] = 'passed'
        _save_json(output_dir / _DOCUMENT, document.model_dump(mode='json'))
        manifest['artifacts']['report_document'] = _DOCUMENT
        data, fills = skill.compile(document, canonical, context=context, inventory=inventory, projection=projection, audit=audit)
        manifest['test_fills'] = fills
        _save_json(attempt / 'candidate_report.json', data)
        errors, warnings = skill.validate(data)
        manifest['validation'].update(template_errors=errors, template_warnings=warnings)
        if errors:
            raise ValueError('Invalid HTML report data: ' + '; '.join(errors))
        html = skill.render(data)
        _save_json(output_dir / '_html_report.json', data)
        _atomic_text(output_dir / '_final_report.html', html)
        manifest['artifacts'].update(report_json='_html_report.json', html='_final_report.html')
        manifest['status'] = 'completed'
        _finish_manifest(manifest, usage, repair_runtime, agent)
        _save_json(manifest_path, manifest)
    except BaseException as exc:
        # Includes timeout/cancellation: a previous preview must never survive as
        # the apparent output of this attempt. Raw stage diagnostics remain intact.
        _clear_published(output_dir)
        manifest['status'] = 'failed'
        manifest['error'] = {'type': type(exc).__name__, 'message': str(exc) or type(exc).__name__}
        _finish_manifest(manifest, usage, repair_runtime, agent)
        try:
            _save_json(attempt / 'failure.json', manifest['error'])
            _save_json(manifest_path, manifest)
        except OSError:
            get_logger('report_html').exception('Unable to save HTML failure diagnostics')
        raise
    return manifest

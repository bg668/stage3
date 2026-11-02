"""Frozen, two-arm experiments; gold labels never enter the execution path."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from pathlib import Path

from ..config import AppConfig
from ..evidence.inventory import build_input_inventory
from ..harness import load_harness
from ..schemas import RunEnvelope
from .dag import load_manifest


def _json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def _hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob('*')) if p.is_file() and '__pycache__' not in p.parts
            and p.suffix != '.pyc'}


def experiment_fingerprint(inputs: dict[str, Path], config: AppConfig, harness_fingerprint: str,
                           *, total_tokens: int = 200000) -> str:
    payload = {'inputs': {name: _hashes(root) for name, root in inputs.items()},
               'config': config.model_dump(mode='json'), 'harness': harness_fingerprint,
               'total_tokens': total_tokens, 'code': _hashes(Path(__file__).resolve().parents[1])}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _preflight_packets(root: Path) -> tuple[Path, list[Path]]:
    manifest_path = root / 'manifest.json'
    manifest = load_manifest(manifest_path)
    packet_dir = root / 'packets' if (root / 'packets').is_dir() else root
    packets = [packet_dir / f'{job.job_id}.md' for job in manifest.jobs]
    if not packets:
        raise ValueError(f'Empty packet manifest: {root}')
    for packet in packets:
        if not packet.is_file() or not packet.read_text(encoding='utf-8').strip():
            raise ValueError(f'Missing or empty packet: {packet}')
    inventory = build_input_inventory(packets)
    indexed = {section.packet_id for section in inventory.code_sections}
    if any(packet.stem not in indexed for packet in packets):
        raise ValueError(f'Packet has no indexable original code: {root}')
    return manifest_path, packets


def _arm_metrics(root: Path, input_root: Path, status: dict) -> dict:
    manifest_path, packets = _preflight_packets(input_root)
    manifest = load_manifest(manifest_path)
    inventory = build_input_inventory(packets)
    envelopes = {}
    for job in manifest.jobs:
        path = root / f'{job.job_id}.json'
        if path.exists():
            envelopes[job.job_id] = RunEnvelope.model_validate_json(path.read_text(encoding='utf-8'))
    with_results = {j for j, e in envelopes.items() if e.result is not None}
    budget_stops = {j: [stage for stage, errors in e.validation_errors.items()
                       if any(error.get('type') == 'sample_budget_exceeded' for error in errors)]
                    for j, e in envelopes.items()}
    fully_completed = {j for j in with_results if envelopes[j].review_completed
                       and not envelopes[j].revision_failed and not budget_stops[j]}
    summary_path = root / '_dag_summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
    audit = []
    for job in envelopes:
        path = root / '_artifacts' / job / 'evidence_validation' / 'final.json'
        if path.exists():
            audit.append({'job_id': job, **json.loads(path.read_text(encoding='utf-8'))})
    budget_path = root / 'budget.json'
    global_audit_path = root / '_evidence_reference_audit.json'
    return {'execution': status, 'task_count': len(manifest.jobs),
            'tasks_with_validated_result': sorted(with_results),
            'validated_result_rate': len(with_results) / len(manifest.jobs),
            'fully_completed_tasks': sorted(fully_completed),
            'completion_rate': len(fully_completed) / len(manifest.jobs),
            'budget_stopped_stages': {j: stages for j, stages in budget_stops.items() if stages},
            'review_completed_tasks': sorted(j for j, e in envelopes.items() if e.review_completed),
            'partial_tasks': sorted(j for j, e in envelopes.items() if e.partial_result is not None),
            'input_unique_functions': len({s.function_id for s in inventory.code_sections}),
            'functions_supplied_to_tasks_with_validated_result': sorted({s.function_id for s in inventory.code_sections if s.packet_id in with_results}),
            'scope_note': '提供给已产出有效结果任务的函数范围，不代表逐函数均已完整分析。',
            'claims': sum(len(e.result.claims) for e in envelopes.values() if e.result),
            'artifacts': sum(len(e.result.artifacts) for e in envelopes.values() if e.result),
            'reference_audits': audit, 'reference_audit_status': 'recorded' if audit else 'not_run',
            'reference_audit_totals': {key: sum(a.get(key, 0) for a in audit)
                for key in ('valid_segments', 'not_in_current_packet', 'invalid_segments')},
            'whole_material_reference_audit': json.loads(global_audit_path.read_text(encoding='utf-8'))
                if global_audit_path.exists() else {'status': 'not_run'},
            'jobs': summary.get('jobs', {}), 'failures': summary.get('failed', {}),
            'global_reconciliation': summary.get('global_reconciliation', {'status': 'not_run'}),
            'budget': json.loads(budget_path.read_text(encoding='utf-8')) if budget_path.exists() else None}


def write_comparison(output_dir: Path, statuses: dict) -> dict:
    arms = {arm: _arm_metrics(output_dir / arm / 'results', output_dir / arm / 'input', statuses[arm])
            for arm in ('automatic', 'manual')}
    comparison = {'arms': arms, 'evaluation_status': 'pending_human_review',
        'limitations': ['两组同时存在选择范围和任务组织差异，不能单独归因于筛选召回率。',
                       '行为与制品数量不是准确率；业务效果需要人工复核。',
                       '预算按请求边界停止；单次请求可能跨线，未知用量不等于零。']}
    _json(output_dir / 'comparison.json', comparison)
    lines = ['# 样本对照基线', '', '| 指标 | 自动组 | 人工组 |', '| --- | ---: | ---: |']
    for title, key in [('任务数', 'task_count'), ('有效结果任务比例', 'validated_result_rate'),
                       ('完整执行任务比例（含审阅）', 'completion_rate'),
                       ('输入唯一函数数（含支持）', 'input_unique_functions'), ('行为数（非准确率）', 'claims'),
                       ('制品数（非准确率）', 'artifacts')]:
        lines.append(f'| {title} | {arms["automatic"][key]} | {arms["manual"][key]} |')
    for title, key in [('请求数', 'requests'), ('输入 tokens', 'input_tokens'), ('输出 tokens', 'output_tokens'),
                       ('缓存读取 tokens（已计入输入）', 'cache_read_tokens'), ('总 tokens', 'total_tokens'),
                       ('实际超额 tokens', 'excess_tokens'), ('未知用量请求数', 'unknown_usage_requests'),
                       ('请求耗时（秒）', 'elapsed_seconds')]:
        values = [(arms[arm]['budget'] or {}).get(key, '未记录') for arm in ('automatic', 'manual')]
        lines.append(f'| {title} | {values[0]} | {values[1]} |')
    for arm, value in arms.items():
        lines.extend(['', f'## {arm}', '', f'执行：{value["execution"]}',
                      f'预算：`{json.dumps(value["budget"], ensure_ascii=False)}`' if value['budget'] is None else
                      f'预算汇总详见 [{arm}/results/budget.json]({arm}/results/budget.json)。',
                      f'未完成与依赖原因详见 [{arm}/results/_dag_summary.json]({arm}/results/_dag_summary.json)。'])
    lines.extend(['', *comparison['limitations'], '', '业务评价：待人工复核。见 human_review.md。'])
    (output_dir / 'comparison.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    review = ['# 人工复核表', '', '标准答案仅供评价端参考，不进入材料生成或模型上下文。', '',
              '| 组 | 目标 | 遗漏 | 错误确认 | 条件与数据流 | 指标价值 | 证据/复核人 | 状态 |',
              '| --- | --- | --- | --- | --- | --- | --- | --- |']
    for arm in arms:
        for objective in ['恶意判定', '检测', '关联溯源']:
            review.append(f'| {arm} | {objective} | 待评价 | 待评价 | 待评价 | 待评价 | | 待评价 |')
    (output_dir / 'human_review.md').write_text('\n'.join(review) + '\n', encoding='utf-8')
    return comparison


async def run_baseline(*, preprocess: Path, triage: Path, manual_dir: Path, config: AppConfig,
                       output_dir: Path, total_tokens: int = 200000) -> dict:
    if output_dir.exists():
        raise FileExistsError(f'Baseline output must be a new directory: {output_dir}')
    if total_tokens <= 0:
        raise ValueError('total_tokens must be positive')
    preprocess = preprocess / 'preprocess.json' if preprocess.is_dir() else preprocess
    triage = triage / 'triage.json' if triage.is_dir() else triage
    target = output_dir.resolve()
    protected = [manual_dir.resolve(), preprocess.parent.resolve(), triage.parent.resolve()]
    if any(target.is_relative_to(root) or root.is_relative_to(target) for root in protected):
        raise ValueError('Baseline output must not overlap protected input directories')
    from ..upstream import prepare_packets
    from ..execution.sample_budget import SampleBudget
    from .batch import run_batch_dag
    # Complete offline validation before any model construction or requests.
    _preflight_packets(manual_dir)
    harness = load_harness(config.harness_dir)
    settings = config.model_copy(deep=True)
    settings.batch.concurrency = 1
    for provider in settings.providers.values():
        provider.sdk_max_retries = 0  # Every charged request must cross our accounting boundary.
    output_dir.mkdir(parents=True, exist_ok=False)
    frozen = output_dir / 'upstream_input'
    frozen.mkdir()
    shutil.copy2(preprocess, frozen / 'preprocess.json')
    shutil.copytree(preprocess.parent / 'code', frozen / 'code')
    shutil.copy2(triage, frozen / 'triage.json')
    auto_input = output_dir / 'automatic' / 'input'
    prepare_packets(frozen / 'preprocess.json', frozen / 'triage.json', auto_input)
    manual_input = output_dir / 'manual' / 'input'
    shutil.copytree(manual_dir, manual_input)
    inputs = {'upstream': frozen, 'automatic': auto_input, 'manual': manual_input}
    preflights = {arm: _preflight_packets(inputs[arm]) for arm in ('automatic', 'manual')}
    harness.save(output_dir)
    _json(output_dir / 'effective_config.json', settings.model_dump(mode='json'))
    manifest = {'schema_version': '1', 'status': 'running', 'total_tokens_per_arm': total_tokens,
                'reserve_tokens': min(40000, total_tokens // 5), 'order': ['automatic', 'manual'],
                'html_model_composition': False, 'concurrency': 1,
                'fingerprint': experiment_fingerprint(inputs, settings, harness.fingerprint, total_tokens=total_tokens),
                'harness_fingerprint': harness.fingerprint, 'inputs': {k: _hashes(v) for k, v in inputs.items()},
                'models': {k: v.model_dump() for k, v in settings.models.items()},
                'preflight': 'passed', 'evaluation_status': 'pending_human_review'}
    _json(output_dir / 'experiment_manifest.json', manifest)
    statuses = {}
    for arm in manifest['order']:
        result_dir = output_dir / arm / 'results'
        result_dir.mkdir()
        budget = SampleBudget(total_tokens=total_tokens, reserve_tokens=manifest['reserve_tokens'],
                              ledger_path=result_dir / 'budget.json')
        started = time.monotonic()
        manifest_path, packets = preflights[arm]
        try:
            await run_batch_dag(packets=packets, output_dir=result_dir, manifest_path=manifest_path,
                                config=settings, log=logging.getLogger('baseline'), harness=harness,
                                sample_budget=budget)
            statuses[arm] = {'status': 'finished', 'elapsed_seconds': round(time.monotonic() - started, 3)}
        except Exception as exc:
            statuses[arm] = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}',
                             'elapsed_seconds': round(time.monotonic() - started, 3)}
            _json(result_dir / 'arm_error.json', statuses[arm])
        finally:
            budget.save()
        manifest['arms'] = statuses
        _json(output_dir / 'experiment_manifest.json', manifest)
    comparison = write_comparison(output_dir, statuses)
    manifest['status'] = 'recorded'
    _json(output_dir / 'experiment_manifest.json', manifest)
    return comparison

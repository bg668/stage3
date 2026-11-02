"""Replay only global reconciliation from an explore run; never rerun Workers."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from bounded_investigator.config import load_config
from bounded_investigator.evidence.scope import load_evidence_scope, EvidenceScope, packet_coverage_from_results
from bounded_investigator.evidence.snippets import build_code_index
from bounded_investigator.evidence.validation import validate_investigation_result
from bounded_investigator.execution.sample_budget import SampleBudget
from bounded_investigator.harness import load_harness
from bounded_investigator.logging_utils import configure_logging
from bounded_investigator.reporting.service import save_final_outputs
from bounded_investigator.schemas import RunEnvelope
from bounded_investigator.workflow import BoundedInvestigationWorkflow


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str)+'\n', encoding='utf-8')


def redact(value):
    if isinstance(value, dict):
        return {k: '[REDACTED]' if re.search(r'authorization|api.?key|token|password|secret|cookie', str(k), re.I)
                else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    text = str(value) if not isinstance(value, (str, int, float, bool, type(None))) else value
    if isinstance(text, str):
        text = re.sub(r'(?i)Bearer\s+[^\s\"\',}]+', 'Bearer [REDACTED]', text)
        text = re.sub(r'(?i)(api[_-]?key|password|secret|access_token)([\"\s:=]+)[^\s\"\',}]+', r'\1\2[REDACTED]', text)
    return text


def error_details(exc):
    chain, seen = [], set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        response = getattr(exc, 'response', None)
        headers = getattr(response, 'headers', {}) or {}
        chain.append(redact({'type': type(exc).__name__, 'message': str(exc),
            'body': getattr(exc, 'body', None),
            'status_code': getattr(exc, 'status_code', getattr(response, 'status_code', None)),
            'request_id': getattr(exc, 'request_id', None) or headers.get('x-request-id'),
            'error_code': getattr(exc, 'code', None)}))
        exc = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
    return {'exception_chain': chain}


async def verify(source, output, *, config=None, env_file=Path('.env'), total_tokens=None, check_only=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError(f'Output must be a new directory: {output}')
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError('Output must not overlap the source run')
    cfg = load_config(config or source/'effective_config.json', env_file)
    if not cfg.global_reconciliation.enabled:
        raise ValueError('Global reconciliation is disabled in config')
    scope = read(source/'results/exploration_scope.json')
    packets = sorted((source/'input').glob('J*.md'))
    if not packets:
        raise ValueError('Missing saved J*.md material packets')
    index = build_code_index(packets)
    completed, excluded, originals = {}, {}, {}
    for task in scope['history']:
        name = task['task_id']
        if not re.fullmatch(r'J\d+', name):
            raise ValueError(f'Invalid saved task ID: {name}')
        if task['status'] != 'reviewed':
            excluded[name] = 'not a reviewed result'
            continue
        path = source/'results'/f'{name}.json'
        raw = path.read_bytes()
        envelope = RunEnvelope.model_validate_json(raw)
        audit = validate_investigation_result(envelope.result, index=index, scope='full_material') if envelope.result else None
        if (envelope.packet_name != name or not envelope.review_completed or envelope.revision_failed
                or not audit or not audit['passed']):
            raise ValueError(f'Saved task {name} is not a verified reviewed result')
        completed[name], originals[name] = envelope, raw
    if not completed:
        raise ValueError('No verified reviewed results to reconcile')
    old_budget = read(source/'results/budget.json') if (source/'results/budget.json').exists() else None
    limit = total_tokens if total_tokens is not None else old_budget['remaining_total_tokens'] if old_budget else 40000
    if limit <= 0:
        raise ValueError('No positive known remaining budget; specify --total-tokens for an independent experiment')
    harness = load_harness(cfg.harness_dir)
    manifest_path = source/'experiment_manifest.json'
    if manifest_path.exists() and read(manifest_path).get('harness_fingerprint', harness.fingerprint) != harness.fingerprint:
        raise ValueError('Harness changed since the source run')
    output.mkdir(parents=True, exist_ok=False)
    frozen = output/'input'; frozen.mkdir()
    hashes = {}
    for name, raw in originals.items():
        p = frozen/f'{name}.json'; p.write_bytes(raw)
        hashes[p.name] = hashlib.sha256(raw).hexdigest()
    for p in packets:
        raw=p.read_bytes(); (frozen/p.name).write_bytes(raw)
        hashes[p.name] = hashlib.sha256(raw).hexdigest()
    for provider in cfg.providers.values():
        provider.sdk_max_retries = 0
    cfg.logging.file = str(output/'application.log')
    cfg.logging.console = True
    configure_logging(cfg.logging)
    write(output/'effective_config.json', cfg.model_dump(mode='json'))
    write(output/'exploration_scope.json', scope)
    evidence_path = source/'results/_evidence_scope.json'
    evidence_scope = load_evidence_scope(evidence_path) if evidence_path.exists() else EvidenceScope()
    budget = SampleBudget(total_tokens=limit, reserve_tokens=0, ledger_path=output/'budget.json')
    budget.save()
    summary = {'status': 'check_only' if check_only else 'running', 'source_run': str(source),
        'admitted_tasks': sorted(completed), 'excluded_tasks': excluded, 'input_sha256': hashes,
        'harness_fingerprint': harness.fingerprint, 'total_tokens_limit': limit,
        'source_unknown_usage_requests': old_budget['unknown_usage_requests'] if old_budget else None,
        'budget_note': 'Independent replay ledger; default cap is the source known remaining balance. Unknown historical usage is not zero cost.',
        'workers_rerun': False, 'check_only': check_only}
    write(output/'summary.json', summary)
    if check_only:
        return summary
    started = time.monotonic()
    try:
        workflow = BoundedInvestigationWorkflow(cfg, harness=harness, sample_budget=budget)
        result = await workflow.run_global_reconciliation(completed_results=completed, failures=excluded,
            artifact_dir=output/'_global_artifacts')
        write(output/'_global_reconciliation.json', result.model_dump(mode='json'))
        report = save_final_outputs(output, result, evidence_scope=evidence_scope,
            packet_coverage=packet_coverage_from_results(completed, excluded), packet_source_dir=frozen,
            exploration_scope=scope)
        summary.update(status='success' if result.delivery_status.value in {'success', 'success_after_repair'}
                       and result.provenance_valid and report.get('completed') else 'incomplete',
            delivery_status=result.delivery_status.value, provenance_valid=result.provenance_valid, report=report)
    except Exception as exc:
        details = error_details(exc)
        write(output/'error.json', details)
        summary.update(status='failed', error=details)
    finally:
        budget.save()
        summary.update(elapsed_seconds=round(time.monotonic()-started, 3), budget=budget.to_dict())
        write(output/'summary.json', summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-run', required=True, type=Path)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--config', type=Path, help='Default: source-run/effective_config.json')
    parser.add_argument('--env-file', type=Path, default=Path('.env'))
    parser.add_argument('--total-tokens', type=int, help='Default: source known remaining tokens, or 40000 if no ledger')
    parser.add_argument('--check-only', action='store_true', help='Validate and freeze inputs without constructing models')
    args = parser.parse_args()
    output = args.output_dir or Path('results')/('global-check-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    try:
        result = asyncio.run(verify(args.source_run, output, config=args.config, env_file=args.env_file,
            total_tokens=args.total_tokens, check_only=args.check_only))
    except Exception as exc:
        print(json.dumps(error_details(exc), ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    print(f'Output: {output.resolve()}')
    return 0 if result['status'] in {'success', 'check_only'} else 1


if __name__ == '__main__':
    raise SystemExit(main())

"""Serial, evidence-driven exploration of a fixed source catalog under one budget."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
from importlib.resources import files
from pathlib import Path

from pydantic_ai.usage import RunUsage, UsageLimits

from ..config import AppConfig
from ..evidence.inventory import build_input_inventory, build_automatic_scope, dump_inventory_json, _split_function_targets
from ..evidence.scope import packet_coverage_from_results
from ..evidence.snippets import build_code_index, parse_evidence_location
from ..evidence.validation import validate_evidence_refs, validate_investigation_result
from ..execution.llm import build_stage_agent
from ..execution.sample_budget import SampleBudget, SampleBudgetExceeded
from ..execution.stage_runner import RepairRuntime, StageRunner
from ..harness import load_harness
from ..io import write_result, write_error
from ..reporting.service import save_final_outputs
from ..upstream.catalog import load_catalog
from ..upstream.packets import prepare_task_packet
from ..workflow import BoundedInvestigationWorkflow
from .baseline import _hashes
from .exploration_models import Candidate, ExplorationTask, FunctionProgress, PlanDecision


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _write(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def _key(ref: str) -> int:
    return int(ref.split('-')[1])


class PlanningFailed(RuntimeError):
    pass


def planner_instructions() -> str:
    return files('bounded_investigator').joinpath('prompts/exploration_planner.instructions.md').read_text(encoding='utf-8')


async def choose_next(*, candidates: list[Candidate], reviewed_facts: list[dict], drafts: list[dict],
                      config: AppConfig, budget: SampleBudget, artifact_dir: Path, round_index: int,
                      instructions: str | None = None) -> PlanDecision:
    """One planner response; invalid choices and malformed outputs stop exploration."""
    artifact_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {'round': round_index, 'remaining_analysis_tokens': budget.remaining('analysis'),
                'candidates': [c.model_dump(mode='json') for c in candidates],
                'reviewed_facts': reviewed_facts, 'drafts': drafts}
    _write(artifact_dir / 'planning_input.json', snapshot)
    try:
        budget.check('analysis', f'PLAN-{round_index:04d}', 'planner')
        agent = build_stage_agent(config=config, stage='planner', schema=PlanDecision,
                                  instructions=planner_instructions() if instructions is None else instructions, output_retries=0)
        runner = StageRunner(config=config, format_repair=None, log=logging.getLogger('exploration.planner'),
                             sample_budget=budget)
        outcome = await runner.run(stage_agent=agent, prompt=json.dumps(snapshot, ensure_ascii=False),
            packet_name=f'PLAN-{round_index:04d}', usage=RunUsage(), limits=UsageLimits(request_limit=1),
            repair_runtime=RepairRuntime(), artifact_dir=artifact_dir)
        if not isinstance(outcome.output, PlanDecision):
            raise PlanningFailed(f'planner returned {outcome.status.value}; no valid decision')
        decision = outcome.output
        if decision.action == 'analyze' and decision.candidate_id not in {c.candidate_id for c in candidates}:
            raise PlanningFailed(f'planner selected unknown candidate_id: {decision.candidate_id}')
        _write(artifact_dir / 'decision.json', decision.model_dump(mode='json'))
        return decision
    except Exception as exc:
        _write(artifact_dir / 'planning_error.json', {'type': type(exc).__name__, 'message': str(exc)})
        if isinstance(exc, PlanningFailed):
            raise
        raise PlanningFailed(f'{type(exc).__name__}: {exc}') from exc


def candidate_allowed(candidate: Candidate, history: list[dict], *, material_fingerprint: str) -> bool:
    """A task's own facts or an unrelated result cannot reopen its old question."""
    related = [h for h in history if set(h['primary_refs']) & set(candidate.primary_refs)]
    if any(sum(ref in h['primary_refs'] for h in history) >= 2 for ref in candidate.primary_refs):
        return False
    if not related:
        return True
    same_issue = [h for h in related if h['issue_key'] == candidate.issue_key]
    if not same_issue:
        return bool(candidate.source_task_ids)  # A distinct, grounded request, never reworded overview.
    previous = same_issue[-1]
    return (previous['material_fingerprint'] != material_fingerprint or
            bool(set(candidate.relevant_evidence_ids) - set(previous.get('post_evidence_ids', []))))


def _fact_refs(fact: dict) -> set[str]:
    return set(fact['refs'])


def _relevant_ids(facts: list[dict], refs: set[str], *, exclude_task: str | None = None) -> list[str]:
    return sorted(f['evidence_id'] for f in facts if _fact_refs(f) & refs and f['task_id'] != exclude_task)


def _material_fingerprint(catalog, primary: list[str], support: list[str]) -> str:
    return _digest({'primary': [(r, catalog.functions[r]['code_sha256']) for r in primary],
                    'support': [(r, catalog.functions[r]['code_sha256']) for r in support]})


def _targets(raw: str, catalog) -> list[str]:
    try:
        return [catalog.resolve(raw)]
    except ValueError as exact_error:
        if '/' in raw or '\\' in raw or '..' in raw:
            raise ValueError('function targets cannot be file paths') from exact_error
        targets = _split_function_targets(raw)
        if not targets:
            raise exact_error
        return list(dict.fromkeys(catalog.resolve(t.identifier) for t in targets))


def build_candidates(*, catalog, records: list[dict], completed: dict, facts: list[dict], index: dict,
                     rejected: list[dict]) -> list[Candidate]:
    """Normalize source-bound requests into executable SCC-sized candidate choices."""
    candidates: dict[str, Candidate] = {}
    visited = {r for h in records for r in h['primary_refs']}

    def add(target: str, *, question: str, origin: str | None = None, evidence: list[str] | None = None,
            source_refs: list[str] | None = None, kind: str = 'unvisited'):
        source_refs, evidence = source_refs or [], evidence or []
        fn = catalog.functions[target]
        if not fn['available']:
            rejected.append({'target': target, 'source_task_id': origin, 'reason': 'source_unavailable'})
            return
        if target in catalog.selected_refs:
            primary, support = catalog.components(target), []
        elif source_refs:
            # Unretained code is only support, keeping the original denominator fixed.
            primary, support = catalog.components(source_refs[0]), [target]
            if len(catalog.code(target)) > 12000:
                rejected.append({'target': target, 'source_task_id': origin, 'reason': 'support_size_boundary'})
                return
        else:
            return
        related_refs = set(primary + support + source_refs)
        call_sites = sorted({(ref, start, end) for loc in evidence for ref, start, end in parse_evidence_location(loc)})
        # Description text and source task IDs do not define question identity.
        issue = _digest({'primary': primary, 'support': support, 'call_sites': call_sites,
                         'source_refs': sorted(source_refs), 'objectives': ['maliciousness', 'detection', 'attribution'],
                         'kind': 'overview' if kind == 'unvisited' else 'requested'})
        relevant = _relevant_ids(facts, related_refs)
        material = _material_fingerprint(catalog, primary, support)
        cid = 'C-' + _digest({'issue': issue, 'material': material, 'facts': relevant})[:20]
        candidate = Candidate(candidate_id=cid, primary_refs=primary, support_refs=support,
            question=question, issue_key=issue, depth='overview' if kind == 'unvisited' else 'question',
            rationale=kind, source_task_ids=[origin] if origin else [], origin_evidence=evidence,
            related_refs=sorted(related_refs, key=_key), relevant_evidence_ids=relevant,
            code_chars=sum(len(catalog.code(r)) for r in primary + support), material_fingerprint=material,
            navigation={'symbol': fn['name'], 'is_entry': fn['is_entry'], 'is_export': fn['is_export'],
                        'selection_reason': str(fn.get('reason', ''))[:240], 'unretained_support': support})
        if candidate_allowed(candidate, records, material_fingerprint=material):
            candidates.setdefault(cid, candidate)
        else:
            rejected.append({'target': target, 'source_task_id': origin, 'issue_key': issue,
                             'reason': 'duplicate_task_or_revisit_limit'})

    for task_record in records:
        task_id = task_record['task_id']
        outcome = completed.get(task_id)
        result = outcome.result if outcome else None
        if result is None:
            continue
        source_refs = task_record['primary_refs']
        for need in result.material_needs:
            if need.kind != 'function_code':
                continue
            checks = validate_evidence_refs(need.evidence, owner_kind='material_need', owner_id=task_id,
                                            index=index, missing_status='invalid_function')
            if not checks or any(c.status != 'valid' for c in checks):
                rejected.append({'source_task_id': task_id, 'target': need.display_targets(), 'reason': 'invalid_request_evidence'})
                continue
            try:
                targets = [catalog.resolve(t.identifier) for t in need.targets] if need.targets else _targets(need.target, catalog)
            except ValueError as exc:
                rejected.append({'source_task_id': task_id, 'target': need.display_targets(), 'reason': str(exc)})
                continue
            for target in targets:
                add(target, question=need.reason, origin=task_id, evidence=[e.location for e in need.evidence],
                    source_refs=source_refs, kind='material_need')
        for handoff in result.handoffs:
            try:
                targets = _targets(handoff.target, catalog)
            except ValueError as exc:
                rejected.append({'source_task_id': task_id, 'target': handoff.target, 'reason': str(exc)})
                continue
            for target in targets:
                add(target, question=handoff.reason, origin=task_id, source_refs=source_refs, kind='handoff')
        for gap in result.gaps:
            # Only explicit function identifiers in a gap become source requests.
            for token in re.findall(r'(?<![\w])F-\d{4,}(?![\w])', gap.question):
                try:
                    target = catalog.resolve(token)
                except ValueError as exc:
                    rejected.append({'source_task_id': task_id, 'target': token, 'reason': str(exc)})
                    continue
                add(target, question=gap.question, origin=task_id, source_refs=source_refs, kind='gap')
    for ref in sorted(catalog.selected_refs - visited, key=_key):
        add(ref, question='Analyze the visible function conditions, source-to-action-to-sink facts and important unresolved links.', kind='unvisited')
    return sorted(candidates.values(), key=lambda c: (not bool(c.source_task_ids), not c.navigation['is_entry'], _key(c.primary_refs[0]), c.candidate_id))


def _reviewed_facts(task_id: str, outcome, index: dict) -> list[dict]:
    if not outcome.review_completed or outcome.revision_failed or outcome.result is None:
        return []
    facts = []
    for kind, items in [('claim', outcome.result.claims), ('artifact', outcome.result.artifacts)]:
        for item in items:
            checks = validate_evidence_refs(item.evidence, owner_kind=kind, owner_id=task_id, index=index,
                                            missing_status='invalid_function')
            if not checks or any(c.status != 'valid' for c in checks):
                continue
            payload = item.model_dump(mode='json')
            facts.append({'evidence_id': f'{task_id}:{kind}:{_digest(payload)[:16]}', 'task_id': task_id,
                          'kind': kind, 'refs': sorted({c.function_id for c in checks}, key=_key), 'finding': payload})
    return facts


def _compact_facts(facts: list[dict]) -> list[dict]:
    compact = []
    for f in facts:
        value = f['finding']
        compact.append({k: f[k] for k in ('evidence_id', 'task_id', 'kind', 'refs')} | {
            'finding': {k: v[:320] if isinstance(v, str) else v for k, v in value.items()
                        if k in {'behavior', 'condition', 'source', 'action', 'sink', 'value', 'role', 'meaning', 'evidence_level'}}})
    return compact


def _scope(catalog, progress: dict[str, FunctionProgress], records: list[dict], completed: dict,
           pending: list[dict], stop_reason: str, model_stop_reason: str) -> dict:
    primary = {r for h in records for r in h['primary_refs']}
    supplied = {r for r, p in progress.items() if p.supplied_task_ids}
    reviewed = {r for r, p in progress.items() if p.reviewed_task_ids}
    support = {r for r, p in progress.items() if p.support_task_ids}
    draft = {r for r, p in progress.items() if p.valid_draft_task_ids and not p.reviewed_task_ids and p.primary_task_ids}
    unvisited = catalog.selected_refs - primary
    revisits = [{'ref': r, 'task_ids': p.primary_task_ids} for r, p in progress.items() if len(p.primary_task_ids) > 1]
    return {'schema_version': '1', 'mode': 'exploration', 'candidate_count': len(catalog.selected_refs),
            'selected_count': len(catalog.selected_refs), 'rounds': len(records),
            'supplied_refs': sorted(supplied & catalog.selected_refs, key=_key), 'primary_refs': sorted(primary, key=_key),
            'reviewed_refs': sorted(reviewed, key=_key), 'draft_refs': sorted(draft, key=_key),
            'support_refs': sorted(support, key=_key), 'selected_support_refs': sorted(support & catalog.selected_refs, key=_key),
            'nonselected_support_refs': sorted(support - catalog.selected_refs, key=_key),
            'unvisited_refs': sorted(unvisited, key=_key),
            'deferred_refs': sorted(unvisited, key=_key) if stop_reason == 'analysis_budget_exhausted' else [],
            'revisits': revisits, 'pending_questions': pending, 'stop_reason': stop_reason,
            'model_stop_reason': model_stop_reason, 'function_progress': {r: p.model_dump(mode='json') for r, p in progress.items()},
            'full_candidate_coverage_implies_business_completion': False, 'business_evaluation': 'pending_human_review',
            'coverage_semantics': 'unvisited means never supplied as primary; support does not establish completed investigation',
            'history': records}


async def run_exploration(*, preprocess: Path, triage: Path, config: AppConfig, output_dir: Path,
                          total_tokens: int = 200000) -> dict:
    preprocess, triage, output_dir = map(Path, (preprocess, triage, output_dir))
    preprocess = preprocess / 'preprocess.json' if preprocess.is_dir() else preprocess
    triage = triage / 'triage.json' if triage.is_dir() else triage
    if 'planner' not in config.stages:
        raise ValueError('explore requires stages.planner before any model calls')
    if total_tokens <= 0:
        raise ValueError('total_tokens must be positive')
    if output_dir.exists():
        raise FileExistsError(f'Exploration output must be a new directory: {output_dir}')
    target = output_dir.resolve()
    if any(target.is_relative_to(root) or root.is_relative_to(target) for root in
           [preprocess.parent.resolve(), triage.parent.resolve()]):
        raise ValueError('Exploration output must not overlap source directories')
    original_catalog = load_catalog(preprocess, triage)  # offline complete source preflight
    code_root = preprocess.parent / 'code'
    if code_root.is_symlink() or (hasattr(code_root, 'is_junction') and code_root.is_junction()):
        raise ValueError('Source code directory links are not allowed')
    if any(p.is_symlink() or (hasattr(p, 'is_junction') and p.is_junction()) for p in code_root.rglob('*')):
        raise ValueError('Source code links are not allowed')
    harness = load_harness(config.harness_dir)
    settings = config.model_copy(deep=True)
    settings.batch.concurrency = 1
    for provider in settings.providers.values():
        provider.sdk_max_retries = 0
    output_dir.mkdir(parents=True, exist_ok=False)
    frozen, input_dir, result_dir = output_dir / 'source', output_dir / 'input', output_dir / 'results'
    frozen.mkdir()
    input_dir.mkdir()
    result_dir.mkdir()
    shutil.copy2(preprocess, frozen / 'preprocess.json')
    shutil.copy2(triage, frozen / 'triage.json')
    shutil.copytree(code_root, frozen / 'code')
    catalog = load_catalog(frozen / 'preprocess.json', frozen / 'triage.json')
    if (original_catalog.preprocess_sha256 != catalog.preprocess_sha256 or
            original_catalog.triage_sha256 != catalog.triage_sha256 or
            any(original_catalog.functions[ref]['code_sha256'] != catalog.functions[ref]['code_sha256']
                for ref in original_catalog.functions)):
        raise ValueError('source changed while freezing')
    _write(output_dir / 'source_catalog.json', catalog.snapshot())
    _write(output_dir / 'effective_config.json', settings.model_dump(mode='json'))
    harness.save(output_dir)
    prompt = planner_instructions()
    (output_dir / 'planner.instructions.md').write_text(prompt, encoding='utf-8')
    policy = {'max_rounds': settings.exploration.max_rounds, 'revisits_per_function': 1,
              'support_max_functions': 8, 'support_max_chars': 12000, 'planner_requests_per_decision': 1,
              'reserve_tokens': min(40000, total_tokens // 5), 'total_tokens': total_tokens,
              'planner_output_retries': 0, 'planner_format_repair': False, 'html': False}
    source_hashes = _hashes(frozen)
    manifest = {'schema_version': '1', 'mode': 'exploration', 'status': 'running', 'sources': source_hashes,
                'original_sources': {'preprocess': str(preprocess.resolve()), 'triage': str(triage.resolve())},
                'source_fingerprint': _digest(source_hashes), 'policy': policy,
                'planner_fingerprint': hashlib.sha256(prompt.encode()).hexdigest(),
                'harness_fingerprint': harness.fingerprint, 'models': {k: v.model_dump(mode='json') for k, v in settings.models.items()},
                'implementation_fingerprint': _digest(_hashes(Path(__file__).resolve().parents[1])),
                'config_fingerprint': _digest(settings.model_dump(mode='json')),
                'input_freezing': 'source catalog frozen at start; each task packet frozen before its model calls',
                'exploration_scope': 'results/exploration_scope.json'}
    manifest['fingerprint'] = _digest(manifest)
    _write(output_dir / 'experiment_manifest.json', manifest)
    budget = SampleBudget(total_tokens=total_tokens, reserve_tokens=policy['reserve_tokens'], ledger_path=result_dir / 'budget.json')
    budget.save()
    progress = {r: FunctionProgress(selected=r in catalog.selected_refs, available=f['available']) for r, f in catalog.functions.items()}
    records, facts, pending, rejected, drafts = [], [], [], [], []
    completed, failures = {}, {}
    stop_reason, model_stop_reason = 'round_limit', ''
    sequence = 0

    def event(kind: str, **payload):
        nonlocal sequence
        sequence += 1
        with (result_dir / 'exploration_events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'sequence': sequence, 'event': kind, **payload}, ensure_ascii=False, default=str) + '\n')

    def save_state():
        scope = _scope(catalog, progress, records, completed, pending, stop_reason, model_stop_reason)
        _write(result_dir / 'exploration_scope.json', scope)
        _write(result_dir / 'exploration_state.json', {'scope': scope, 'reviewed_facts': facts,
                'drafts': drafts, 'candidate_rejections': rejected, 'budget': budget.to_dict()})
        budget.save()
        return scope

    event('started', source_fingerprint=manifest['source_fingerprint'], policy=policy)
    save_state()
    entry = next((r for r in catalog.entry_refs if r in catalog.selected_refs), None)
    for round_index in range(settings.exploration.max_rounds):
        if budget.exhausted('analysis'):
            stop_reason = 'analysis_budget_exhausted'
            break
        index = build_code_index(sorted(input_dir.glob('J*.md')))
        before_rejections = len(rejected)
        candidates = build_candidates(catalog=catalog, records=records, completed=completed, facts=facts, index=index, rejected=rejected)
        event('candidates_built', round=round_index, candidate_ids=[c.candidate_id for c in candidates],
              rejected=rejected[before_rejections:])
        if not candidates:
            stop_reason = 'no_executable_candidates'
            break
        if round_index == 0 and entry is not None:
            candidate = next(c for c in candidates if entry in c.primary_refs and c.depth == 'overview')
            reason = 'entry metadata seed'
        else:
            try:
                decision = await choose_next(candidates=candidates, reviewed_facts=_compact_facts(facts), drafts=drafts,
                    config=settings, budget=budget, artifact_dir=result_dir / '_planning' / f'{round_index:04d}',
                    round_index=round_index, instructions=prompt)
                event('planning_decision', round=round_index, decision=decision.model_dump(mode='json'))
                if decision.action == 'stop':
                    stop_reason, model_stop_reason = 'planner_stop', decision.reason
                    break
                candidate = next((c for c in candidates if c.candidate_id == decision.candidate_id), None)
                if candidate is None:
                    raise PlanningFailed('unknown candidate_id')
                reason = decision.reason
            except Exception as exc:
                stop_reason = 'analysis_budget_exhausted' if budget.exhausted('analysis') else 'planning_failed'
                event('planning_failed', error=f'{type(exc).__name__}: {exc}')
                break
        if budget.exhausted('analysis'):
            stop_reason = 'analysis_budget_exhausted'
            break
        task_id = f'J{len(records):04d}'
        task = ExplorationTask(task_id=task_id, overview=candidate.question, question=candidate.question,
            primary_refs=candidate.primary_refs, support_refs=candidate.support_refs, goals=candidate.goals,
            rationale=reason, depth=candidate.depth, context_task_ids=sorted({f['task_id'] for f in facts
                if f['evidence_id'] in candidate.relevant_evidence_ids}), relevant_evidence_ids=candidate.relevant_evidence_ids,
            issue_key=candidate.issue_key, material_fingerprint=candidate.material_fingerprint,
            task_fingerprint=_digest({'candidate': candidate.model_dump(mode='json'), 'policy': policy}))
        packet = prepare_task_packet(catalog, task_id=task_id, primary_refs=task.primary_refs, support_refs=task.support_refs,
            question=task.question, depth=task.depth, output_dir=input_dir)
        _write(result_dir / f'{task_id}.task.json', task.model_dump(mode='json'))
        record = task.model_dump(mode='json') | {'status': 'running', 'input_sha256': packet['mapping']['input_sha256'],
            'primary_refs': packet['primary_refs'], 'support_refs': packet['support_refs'], 'boundaries': packet['boundaries']}
        records.append(record)
        for ref in record['primary_refs'] + record['support_refs']:
            progress[ref].supplied_task_ids.append(task_id)
            (progress[ref].primary_task_ids if ref in record['primary_refs'] else progress[ref].support_task_ids).append(task_id)
            progress[ref].pending_questions.append(task.question)
        event('task_selected', task=record)
        save_state()
        try:
            context = {'reviewed_facts': _compact_facts([f for f in facts if f['evidence_id'] in task.relevant_evidence_ids]),
                       'unresolved_drafts': drafts, 'instruction': 'Drafts and supplied material do not establish resolved questions.'}
            workflow = BoundedInvestigationWorkflow(settings, harness=harness, sample_budget=budget)
            outcome = await workflow.run(packet_name=task_id, packet_text=packet['text'],
                dependency_context=json.dumps(context, ensure_ascii=False), dependency_ids=task.context_task_ids,
                artifact_dir=result_dir / '_artifacts' / task_id)
            write_result(result_dir / f'{task_id}.json', outcome)
            completed[task_id] = outcome
            index = build_code_index(sorted(input_dir.glob('J*.md')))
            facts.extend(_reviewed_facts(task_id, outcome, index))
            full = outcome.result
            audit = validate_investigation_result(full, index=index, scope='full_material') if full is not None else None
            reviewed = full is not None and outcome.review_completed and not outcome.revision_failed and audit['passed']
            record['status'] = 'reviewed' if reviewed else 'draft' if full is not None else outcome.delivery_status.value
            if full is not None or outcome.partial_result is not None:
                for ref in record['primary_refs']:
                    progress[ref].valid_draft_task_ids.append(task_id)
                if not reviewed:
                    drafts.append({'task_id': task_id, 'primary_refs': record['primary_refs'], 'resolved': False,
                                   'status': record['status'], 'summary': (full.summary if full else outcome.partial_result.summary or '')[:600]})
            if reviewed:
                for ref in record['primary_refs']:
                    progress[ref].reviewed_task_ids.append(task_id)
            if full is not None:
                for gap in full.gaps:
                    pending.append({'source_task_id': task_id, 'kind': 'gap', 'question': gap.question,
                                    'reason': gap.reason, 'status': 'pending'})
                for need in full.material_needs:
                    pending.append({'source_task_id': task_id, 'kind': 'material_need', 'target': need.display_targets(),
                                    'question': need.reason, 'evidence': [e.model_dump() for e in need.evidence], 'status': 'pending'})
                for handoff in full.handoffs:
                    pending.append({'source_task_id': task_id, 'kind': 'handoff', 'target': handoff.target,
                                    'question': handoff.reason, 'status': 'pending'})
            event('task_result', task_id=task_id, status=record['status'], review_completed=outcome.review_completed,
                  usage=outcome.usage.model_dump(mode='json'))
        except Exception as exc:
            failures[task_id] = f'{type(exc).__name__}: {exc}'
            write_error(result_dir / f'{task_id}.error.json', task_id, exc)
            record['status'], record['error'] = 'failed', failures[task_id]
            event('task_failed', task_id=task_id, error=failures[task_id])
        record['post_evidence_ids'] = _relevant_ids(facts, set(candidate.related_refs or candidate.primary_refs + candidate.support_refs))
        save_state()
    else:
        stop_reason = 'round_limit'
    event('stopped', reason=stop_reason, model_reason=model_stop_reason, rounds=len(records))
    scope = save_state()
    packets = sorted(input_dir.glob('J*.md'))
    inventory = build_input_inventory(packets)
    (result_dir / '_evidence_inventory.json').write_text(dump_inventory_json(inventory), encoding='utf-8')
    evidence_scope, material_requests = build_automatic_scope(inventory, completed)
    _write(result_dir / '_evidence_scope.json', evidence_scope.model_dump(mode='json'))
    _write(result_dir / '_material_requests.json', [m.model_dump(mode='json') for m in material_requests])
    unavailable = dict(failures)
    available = {r['task_id']: completed[r['task_id']] for r in records if r['status'] == 'reviewed'}
    for task_id in completed.keys() - available.keys():
        unavailable[task_id] = 'draft or partial result has not completed review and reference checks; not admitted to global facts'
    global_status = {'status': 'skipped', 'reason': 'no_reviewed_results'}
    if available and not settings.global_reconciliation.enabled:
        global_status['reason'] = 'disabled_by_config'
    elif available and budget.exhausted('global'):
        global_status['reason'] = 'sample_budget_exhausted'
    elif available:
        try:
            workflow = BoundedInvestigationWorkflow(settings, harness=harness, sample_budget=budget)
            global_result = await workflow.run_global_reconciliation(completed_results=available, failures=unavailable,
                artifact_dir=result_dir / '_global_artifacts')
            _write(result_dir / '_global_reconciliation.json', global_result.model_dump(mode='json'))
            report = save_final_outputs(result_dir, global_result, evidence_scope=evidence_scope,
                packet_coverage=packet_coverage_from_results(completed, failures), packet_source_dir=input_dir,
                exploration_scope=scope)
            global_status = {'status': global_result.delivery_status.value, 'report': report}
        except Exception as exc:
            write_error(result_dir / '_global_reconciliation.error.json', 'GLOBAL', exc)
            global_status = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}'}
    event('global_reconciliation', **global_status)
    budget.save()
    manifest.update(status='finished', stop_reason=stop_reason, global_reconciliation=global_status,
                    actual_task_inputs={p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in packets},
                    budget=budget.to_dict(), rounds=len(records))
    _write(output_dir / 'experiment_manifest.json', manifest)
    return scope

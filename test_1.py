import asyncio
from pathlib import Path
from uuid import uuid4
from bounded_investigator.config import load_config
from bounded_investigator.dag import render_dependency_handoffs
from bounded_investigator.io import write_result
from bounded_investigator.schemas import InvestigationResult, RunEnvelope, DeliveryStatus
from bounded_investigator.workflow import BoundedInvestigationWorkflow, _StageOutcome

job = "J13"
source = Path("results_v17")
saved = RunEnvelope.model_validate_json(
    (source / f"{job}.json").read_text(encoding="utf-8")
)
draft = InvestigationResult.model_validate_json(
    (source / "_artifacts" / job / "analyzer" / "validated_result.json")
    .read_text(encoding="utf-8")
)
upstream = {
    dep: RunEnvelope.model_validate_json(
        (source / f"{dep}.json").read_text(encoding="utf-8")
    )
    for dep in saved.dependency_ids
}

config = load_config("config.yaml")
config.workflow.enable_revision = True
config.global_reconciliation.enabled = False
runner = BoundedInvestigationWorkflow(config)
original_run = runner._run_agent

async def reuse_analyzer(**kwargs):
    if kwargs["stage_agent"].stage == "analyzer":
        return _StageOutcome(
            status=DeliveryStatus.SUCCESS,
            output=draft.model_copy(deep=True),
        )
    return await original_run(**kwargs)

runner._run_agent = reuse_analyzer
out = Path(f"results_prompt_check_{job}_{uuid4().hex[:8]}")
result = asyncio.run(runner.run(
    packet_name=job,
    packet_text=Path(f"testdata/packets/{job}.md").read_text(encoding="utf-8"),
    dependency_ids=saved.dependency_ids,
    dependency_context=render_dependency_handoffs(
        saved.dependency_ids, upstream, {}
    ),
    artifact_dir=out / "_artifacts" / job,
))
write_result(out / f"{job}.json", result)
print("output:", out.resolve())
print("review_completed:", result.review_completed)
print("revision_performed:", result.revision_performed)
print("revision_failed:", result.revision_failed)
print("validation_errors:", result.validation_errors)
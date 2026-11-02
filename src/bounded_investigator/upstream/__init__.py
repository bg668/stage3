"""Artifact boundary between deterministic preprocessing, triage and investigation."""

from pathlib import Path

from .packets import prepare_packets


def run_preprocess_pipeline(sqlite: Path, output_dir: Path):
    """Run the preserved upstream Stage-1 implementation into a new directory."""
    from .preprocess.application import PreprocessOptions, run_preprocess

    if output_dir.exists():
        raise FileExistsError(output_dir)
    return run_preprocess(PreprocessOptions(sqlite=sqlite, out=output_dir))


def run_triage_pipeline(preprocess_path: Path, llm_config: Path, output_dir: Path):
    """Run Stage 2 using its existing registry-JSON model configuration contract."""
    from .triage.application import TriageOptions, run_triage
    from .triage.provider_factory import build_llm_provider_pair

    if output_dir.exists():
        raise FileExistsError(output_dir)
    pair = build_llm_provider_pair(llm_config)
    return run_triage(
        TriageOptions(preprocess=preprocess_path, out=output_dir, config=pair.config),
        pair.round1_provider, pair.round2_provider,
    )


__all__ = ["prepare_packets", "run_preprocess_pipeline", "run_triage_pipeline"]

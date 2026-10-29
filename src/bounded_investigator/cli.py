from __future__ import annotations

import asyncio
from pathlib import Path

import typer

from .config import load_config
from .logging_utils import configure_logging
from .orchestration.batch import run_one, run_batch
from .orchestration.replay import reconcile_saved_results
from .reporting.service import report_saved_results
from .reporting.html_report import create_html_report

app = typer.Typer(add_completion=False, no_args_is_help=True)


@app.command("version")
def version() -> None:
    """Print the loaded code, schema and entrypoint paths to detect stale installs."""
    import sys
    from . import __version__
    from .schemas import SCHEMA_VERSION
    typer.echo(f"bounded-investigator version: {__version__}")
    typer.echo(f"schema_version: {SCHEMA_VERSION}")
    typer.echo(f"loaded_module: {Path(__file__).resolve()}")
    typer.echo(f"python: {sys.executable}")


@app.command()
def run(
    packet: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path("result.json"), "--output", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
    harness_dir: Path | None = typer.Option(None, "--harness-dir", exists=True, file_okay=False),
) -> None:
    """Analyze one bounded material packet without DAG dependencies."""
    settings = load_config(config, env_file, harness_dir=harness_dir)
    configure_logging(settings.logging)
    asyncio.run(run_one(packet, output, settings))


@app.command()
def batch(
    input_dir: Path = typer.Argument(..., exists=True, file_okay=False),
    output_dir: Path = typer.Option(Path("results"), "--output-dir", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
    pattern: str = typer.Option("J*.md", "--pattern"),
    manifest: Path | None = typer.Option(
        None,
        "--manifest",
        help="DAG manifest JSON. If omitted, auto-detect manifest.json beside input_dir.",
    ),
    scope_file: Path | None = typer.Option(
        None, "--scope-file", help="Optional expert-only material attestation (never required).",
    ),
    evidence_dir: Path | None = typer.Option(
        None, "--evidence-dir", help="Optional directory containing other actual source/export files to index.",
    ),
    harness_dir: Path | None = typer.Option(None, "--harness-dir", exists=True, file_okay=False),
    html_report: bool = typer.Option(True, '--html-report/--no-html-report',
        help='Compose HTML after valid DAG reconciliation (uses the report model).'),
    context_file: Path | None = typer.Option(None, '--context-file', exists=True, dir_okay=False,
        help='Optional sample identity/context JSON for the HTML report.'),
) -> None:
    """Analyze packets and automatically compose HTML after valid DAG reconciliation."""
    settings = load_config(config, env_file, harness_dir=harness_dir)
    configure_logging(settings.logging)
    html_path = asyncio.run(run_batch(
        input_dir, output_dir, settings, pattern, manifest, scope_file, evidence_dir,
        generate_html=html_report, context_file=context_file,
    ))
    if html_path is not None:
        typer.echo(str(html_path))


@app.command("report")
@app.command("assess")
def assess(
    global_result: Path = typer.Argument(..., exists=True, dir_okay=False,
        help="Existing _global_reconciliation.json; no Worker/DAG re-run"),
    output_dir: Path = typer.Option(Path("assessed_results"), "--output-dir", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
    scope_file: Path | None = typer.Option(
        None, "--scope-file", help="Optional expert-only attestation; defaults to saved automatic scope.",
    ),
    packets_dir: Path | None = typer.Option(
        None, "--packets-dir", help="Optional current-run packet directory to rebuild evidence index if no saved scope exists.",
    ),
    evidence_dir: Path | None = typer.Option(
        None, "--evidence-dir", help="Optional actual nonpacket source files to include when rebuilding index.",
    ),
    packet_results_dir: Path | None = typer.Option(
        None, "--packet-results-dir", help="Folder containing saved Jxx.json envelopes for delivery coverage.",
    ),
) -> None:
    """Produce a three-level evidence report from canonical facts, with no model calls."""
    settings = load_config(config, env_file)
    configure_logging(settings.logging)
    report_saved_results(
        global_result=global_result,
        output_dir=output_dir,
        scope_file=scope_file,
        packets_dir=packets_dir,
        evidence_dir=evidence_dir,
        packet_results_dir=packet_results_dir,
    )


@app.command("report-html")
def report_html(
    global_result: Path = typer.Argument(..., exists=True, dir_okay=False),
    output_dir: Path = typer.Option(Path("html_preview"), "--output-dir", "-o"),
    context_file: Path | None = typer.Option(None, "--context-file", exists=True, dir_okay=False),
    packets_dir: Path | None = typer.Option(None, "--packets-dir", exists=True, file_okay=False),
    packet_results_dir: Path | None = typer.Option(None, "--packet-results-dir", exists=True, file_okay=False),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
) -> None:
    """Compose an HTML preview from saved canonical facts using one model request."""
    settings = load_config(config, env_file)
    configure_logging(settings.logging)
    asyncio.run(create_html_report(
        global_result=global_result, output_dir=output_dir, config=settings,
        context_file=context_file, packets_dir=packets_dir, packet_results_dir=packet_results_dir,
    ))
    typer.echo(str(output_dir / "_final_report.html"))


@app.command("reconcile")
def reconcile(
    packet_results_dir: Path = typer.Argument(..., exists=True, file_okay=False,
        help="Directory containing saved Jxx.json results; does not rerun Workers"),
    output_dir: Path = typer.Option(Path("replayed_results"), "--output-dir", "-o"),
    config: Path = typer.Option(Path("config.yaml"), "--config", "-c", exists=True),
    env_file: Path | None = typer.Option(None, "--env-file"),
    packets_dir: Path | None = typer.Option(None, "--packets-dir",
        help="Optional packet folder to rebuild the material inventory without LLM calls"),
    evidence_dir: Path | None = typer.Option(None, "--evidence-dir",
        help="Optional additional actual input files for material inventory"),
    scope_file: Path | None = typer.Option(None, "--scope-file",
        help="Optional expert attestation only; never required for automatic mode"),
) -> None:
    """Reconcile saved Worker results and regenerate global/final outputs."""
    settings = load_config(config, env_file)
    configure_logging(settings.logging)
    asyncio.run(reconcile_saved_results(
        packet_results_dir=packet_results_dir,
        output_dir=output_dir,
        config=settings,
        packets_dir=packets_dir,
        evidence_dir=evidence_dir,
        scope_file=scope_file,
    ))

# Harness Reorganization Implementation Plan

> Execute task by task in this session using executing-plans; user has authorized implementation.

**Goal:** Add a versioned harness with separate agent definitions and role-loaded skills, preserving current instructions and trusted contracts.

**Architecture:** A trusted loader resolves one immutable instruction snapshot. Workflow receives it, CLI selects a directory and records provenance. Existing schemas, DAG, revision guards and deterministic reports retain their responsibilities. Built-in resources ship in the Python package.

**Tech Stack:** Python, PydanticAI, PyYAML, Typer, pytest.

## Tasks
1. Write failing tests in tests/test_harness.py: baseline equivalence; skill role isolation; deterministic ordering; unsafe paths/frontmatter/unsupported files rejected; immutable snapshots; custom workflow instructions.
2. Implement src/bounded_investigator/harness/ and bundled harnesses/baseline-v1 with shared strategy, agents/*.md and skills/*/SKILL.md. Trusted evidence rules remain package resources.
3. Connect prompts/workflow/config/CLI: explicit harness directory, snapshot per run, sidecar fingerprints and actual instructions. Keep unrelated stages unchanged.
4. Verify targeted and full tests, packaged resource availability, CLI help and offline execution through fakes. Distinguish pre-existing test failures.
5. Review changes and document usage, migration and scope. No model service calls, no harness2 recursion, no autonomous skill tooling or audit bug fix in this change.

## Results

All five implementation tasks completed. Default agent instructions match a pre-migration golden fixture byte-for-byte after the original strip semantics. run/batch support --harness-dir; YAML paths resolve relative to config. Batch freezes one snapshot, source changes cannot alter later workers. Output snapshots reject changed or tampered provenance.

Verification: final full suite 244 passed, 3 pre-existing failures, 1 skipped (Windows symlink permission). The original 3 failures were reproduced before edits: two retired final-assessment config assertions, one old version assertion. Offline wheel build and isolated import of extracted wheel verified bundled baseline resources and all role instructions. Independent review found no blockers; suggested trusted-file override test and wheel verification were added/completed. No paid model execution.

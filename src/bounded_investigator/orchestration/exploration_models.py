"""Contracts for bounded exploration; business findings retain their existing schemas."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Candidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    candidate_id: str
    primary_refs: list[str] = Field(min_length=1)
    support_refs: list[str] = Field(default_factory=list)
    question: str = Field(min_length=1)
    issue_key: str
    depth: Literal['overview', 'question'] = 'question'
    goals: list[str] = Field(default_factory=lambda: ['maliciousness', 'detection', 'attribution'])
    rationale: str = ''
    source_task_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    origin_evidence: list[str] = Field(default_factory=list)
    related_refs: list[str] = Field(default_factory=list)
    code_chars: int = 0
    material_fingerprint: str = ''
    navigation: dict = Field(default_factory=dict)


class PlanDecision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['analyze', 'stop']
    candidate_id: str | None = None
    reason: str = Field(min_length=1)
    expected_value: str = ''

    @model_validator(mode='after')
    def action_contract(self):
        if self.action == 'analyze' and (not self.candidate_id or not self.expected_value.strip()):
            raise ValueError('analyze requires candidate_id and expected_value')
        if self.action == 'stop' and self.candidate_id is not None:
            raise ValueError('stop cannot select a candidate')
        return self


class ExplorationTask(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: str
    overview: str
    question: str
    primary_refs: list[str]
    support_refs: list[str] = Field(default_factory=list)
    goals: list[str]
    rationale: str
    depth: Literal['overview', 'question']
    context_task_ids: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    issue_key: str
    material_fingerprint: str
    task_fingerprint: str


class FunctionProgress(BaseModel):
    selected: bool
    available: bool
    supplied_task_ids: list[str] = Field(default_factory=list)
    primary_task_ids: list[str] = Field(default_factory=list)
    support_task_ids: list[str] = Field(default_factory=list)
    valid_draft_task_ids: list[str] = Field(default_factory=list)
    reviewed_task_ids: list[str] = Field(default_factory=list)
    pending_questions: list[str] = Field(default_factory=list)

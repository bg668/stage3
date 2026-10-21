from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator, model_validator

ProviderType = Literal["openai", "deepseek", "openai_compatible"]
APIMode = Literal["chat", "responses"]
OutputMode = Literal["tool", "native", "prompted"]
ReasoningMode = Literal["omit", "disabled", "minimal", "low", "medium", "high", "xhigh"]
StageName = Literal[
    "analyzer", "reviewer", "reviser", "format_repair",
    "global_reconciliation", "final_evidence_assessment", "gap_reconciliation"
]


class ProviderConfig(BaseModel):
    type: ProviderType = "openai_compatible"
    base_url: str | None = None
    api_key_env: str = "LLM_API_KEY"
    request_timeout_seconds: float = Field(default=180, gt=0)
    sdk_max_retries: int = Field(default=0, ge=0, le=10)


class ModelConfig(BaseModel):
    provider: str
    model_name: str
    api_mode: APIMode = "chat"
    output_mode: OutputMode = "tool"
    reasoning: ReasoningMode = "omit"
    temperature: float | None = None
    stream_output: bool = True

    @field_validator("reasoning", mode="before")
    @classmethod
    def normalize_reasoning(cls, value):
        # `off` means OMIT the parameter entirely, not send thinking=False.
        if value is None or value is False:
            return "omit"
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "off", "none", "null", "omit"}:
                return "omit"
        return value


class StageConfig(BaseModel):
    model: str
    max_output_tokens: int = Field(ge=256)


class FormatRepairConfig(BaseModel):
    enabled: bool = True
    max_attempts: int = Field(default=3, ge=1, le=3)
    total_tokens_limit: int | None = Field(default=120000, ge=1000)
    deterministic_cleanup: bool = True


class GlobalReconciliationConfig(BaseModel):
    enabled: bool = False
    max_model_requests: int = Field(default=1, ge=1, le=3)
    total_tokens_limit: int | None = Field(default=160000, ge=1000)
    wall_clock_seconds: float = Field(default=300, gt=0)


class FinalEvidenceAssessmentConfig(BaseModel):
    enabled: bool = False
    max_model_requests: int = Field(default=1, ge=1, le=3)
    total_tokens_limit: int | None = Field(default=60000, ge=1000)
    wall_clock_seconds: float = Field(default=180, gt=0)


class GapReconciliationConfig(BaseModel):
    enabled: bool = False
    max_model_requests: int = Field(default=1, ge=1, le=3)
    total_tokens_limit: int | None = Field(default=60000, ge=1000)
    wall_clock_seconds: float = Field(default=120, gt=0)


class WorkflowConfig(BaseModel):
    # Core Analyzer/Reviewer/Reviser requests only. Format repair has a separate budget.
    max_model_requests: int = Field(default=3, ge=2, le=10)
    total_tokens_limit: int | None = Field(default=130000, ge=1000)
    wall_clock_seconds: float = Field(default=600, gt=0)
    # Keep native PydanticAI output retry at 0: malformed structured output is handled
    # by the clean-context format repair stage instead of replaying the full analysis context.
    output_retries: int = Field(default=0, ge=0, le=3)
    enable_revision: bool = True

    @model_validator(mode="after")
    def validate_request_budget(self) -> "WorkflowConfig":
        if self.enable_revision and self.max_model_requests < 3:
            raise ValueError("enable_revision=true requires max_model_requests >= 3")
        return self


class BatchConfig(BaseModel):
    concurrency: int = Field(default=4, ge=1, le=32)


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    console: bool = True
    file: str | None = "logs/bounded_investigator.log"


class AppConfig(BaseModel):
    providers: dict[str, ProviderConfig]
    models: dict[str, ModelConfig]
    stages: dict[StageName, StageConfig]
    workflow: WorkflowConfig = WorkflowConfig()
    format_repair: FormatRepairConfig = FormatRepairConfig()
    global_reconciliation: GlobalReconciliationConfig = GlobalReconciliationConfig()
    final_evidence_assessment: FinalEvidenceAssessmentConfig = FinalEvidenceAssessmentConfig()
    # Accepted for legacy v1.0 configs, but intentionally no longer executed.
    gap_reconciliation: GapReconciliationConfig = GapReconciliationConfig()
    batch: BatchConfig = BatchConfig()
    logging: LoggingConfig = LoggingConfig()

    @model_validator(mode="after")
    def validate_references(self) -> "AppConfig":
        for model_key, model in self.models.items():
            if model.provider not in self.providers:
                raise ValueError(
                    f"models.{model_key}.provider={model.provider!r} does not exist in providers"
                )
        required_stages = ["analyzer", "reviewer", "reviser"]
        if self.format_repair.enabled:
            required_stages.append("format_repair")
        if self.global_reconciliation.enabled:
            required_stages.append("global_reconciliation")
        # Legacy final_evidence_assessment configuration is accepted but ignored
        # by the v1.7 automatic report workflow, which does not invoke an LLM.
        for stage in required_stages:
            if stage not in self.stages:
                raise ValueError(f"Missing stages.{stage}")
            model_key = self.stages[stage].model
            if model_key not in self.models:
                raise ValueError(
                    f"stages.{stage}.model={model_key!r} does not exist in models"
                )
        if self.workflow.total_tokens_limit is not None:
            core_stages = [self.stages[s] for s in ("analyzer", "reviewer", "reviser")]
            largest_stage_output = max(stage.max_output_tokens for stage in core_stages)
            if self.workflow.total_tokens_limit < largest_stage_output:
                raise ValueError(
                    "workflow.total_tokens_limit must be >= the largest core stage max_output_tokens "
                    f"({largest_stage_output})"
                )
        if self.format_repair.enabled and self.format_repair.total_tokens_limit is not None:
            repair_max = self.stages["format_repair"].max_output_tokens
            if self.format_repair.total_tokens_limit < repair_max:
                raise ValueError(
                    "format_repair.total_tokens_limit must be >= stages.format_repair.max_output_tokens "
                    f"({repair_max})"
                )
        if self.global_reconciliation.enabled and self.global_reconciliation.total_tokens_limit is not None:
            global_max = self.stages["global_reconciliation"].max_output_tokens
            if self.global_reconciliation.total_tokens_limit < global_max:
                raise ValueError(
                    "global_reconciliation.total_tokens_limit must be >= "
                    "stages.global_reconciliation.max_output_tokens "
                    f"({global_max})"
                )
        return self

    def provider_api_key(self, provider_name: str) -> str:
        provider = self.providers[provider_name]
        value = os.getenv(provider.api_key_env)
        if not value:
            raise RuntimeError(
                f"Missing API key. Put {provider.api_key_env}=... in .env "
                "or export it as an environment variable."
            )
        return value

    def stage(self, stage: StageName) -> tuple[StageConfig, ModelConfig, ProviderConfig]:
        stage_cfg = self.stages[stage]
        model_cfg = self.models[stage_cfg.model]
        provider_cfg = self.providers[model_cfg.provider]
        return stage_cfg, model_cfg, provider_cfg


def load_config(config_path: str | Path, env_file: str | Path | None = None) -> AppConfig:
    config_path = Path(config_path)
    if env_file:
        load_dotenv(Path(env_file), override=False)
    else:
        local_env = config_path.parent / ".env"
        if local_env.exists():
            load_dotenv(local_env, override=False)
        else:
            load_dotenv(override=False)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(raw)

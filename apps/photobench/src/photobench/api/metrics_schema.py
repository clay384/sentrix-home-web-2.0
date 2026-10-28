"""Additive v2 metrics projections; unknown historical fields remain supported."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class OperationalIssue(BaseModel):
    """Versioned execution evidence, separate from answer/architecture quality."""

    model_config = ConfigDict(extra="allow")
    version: Literal[1]
    kind: Literal[
        "output_limit",
        "context_limit",
        "step_limit",
        "time_limit",
        "provider_rejected",
        "resource_error",
        "model_output_invalid",
        "configuration_error",
    ]
    evidence: dict[str, Any]
    scope: Literal["judge"] | None = None


class OperationalCounts(BaseModel):
    model_config = ConfigDict(extra="allow")
    not_evaluable_count: int
    by_kind: dict[str, int]
    judge_incomplete_count: int | None = None
    judge_by_kind: dict[str, int] | None = None


class RunSummaryProjection(BaseModel):
    model_config = ConfigDict(extra="allow")
    operational: OperationalCounts | None = None


class MemoryObservation(BaseModel):
    model_config = ConfigDict(extra="allow")
    memory_accounting: str | None = None
    memory_complete: bool | None = None
    component_validity: dict[str, str] | None = None
    memory_observation_version: int | None = None
    memory_observations: dict[str, dict[str, Any]] | None = None
    memory_dependencies: dict[str, list[str]] | None = None
    sampling_diagnostics: dict[str, Any] | None = None
    observed_at_epoch: float | None = None
    device_observation_age_seconds: float | None = None
    device_observed_at_monotonic: float | None = None
    device_observation_ttl_seconds: float | None = None
    device_freshness_version: int | None = None
    model_process_system_memory_used_mib: float | None = None
    model_process_host_memory_mib: float | None = None
    model_process_gpu_memory_mib: float | None = None
    model_process_footprint_mib: float | None = None
    model_process_mapped_weights_mib: float | None = None
    product_stack_memory_mib: float | None = None
    power_rail: str | None = None
    power_scope: str | None = None
    power_rails_w: dict[str, float] | None = None


class TelemetryProjection(BaseModel):
    model_config = ConfigDict(extra="allow")
    latest: MemoryObservation | None = None
    field_stats: dict[str, Any] | None = None
    rejected_samples: dict[str, int] | None = None
    sampling: dict[str, Any] | None = None


class ImportObservationProjection(BaseModel):
    model_config = ConfigDict(extra="allow")
    operation_id: str | None = None
    scope_id: str | None = None
    total: int | None = None
    completed: int | None = None
    imported: int | None = None
    deduplicated: int | None = None
    failed: int | None = None
    stage: str | None = None
    status: str | None = None
    current_file: str | None = None
    updated_at: str | None = None
    observed_at: str | None = None
    sequence: int | None = None
    error: str | None = None


class JudgeRequestMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")
    version: int | None = None
    requests_started: int | None = None
    responses_received: int | None = None
    request_errors: int | None = None
    http_429: int | None = None
    in_flight: int | None = None
    rate_waiting: int | None = None
    rate_wait_ms: float | None = None


class PhaseProgressProjection(BaseModel):
    model_config = ConfigDict(extra="allow")
    operational: OperationalCounts | None = None
    total: int | None = None
    completed: int | None = None
    agent_completed: int | None = None
    agent_total: int | None = None
    agent_waiting: int | None = None
    agent_running: int | None = None
    judge_waiting: int | None = None
    judge_running: int | None = None
    outcome_unknown: int | None = None
    judge_completed: int | None = None
    judge_total: int | None = None
    judge_skipped: int | None = None
    qa_concurrency: int | None = None
    judge_concurrency: int | None = None
    observation: ImportObservationProjection | None = None
    operation_id: str | None = None
    availability: str | None = None
    last_received_at: str | None = None
    last_checked_at: str | None = None
    query_error: str | None = None
    request_status: str | None = None
    request_error: str | None = None
    observer_stop_pending: bool | None = None


class PhaseMetricsProjection(BaseModel):
    model_config = ConfigDict(extra="allow")
    operational: OperationalCounts | None = None
    judge_requests: JudgeRequestMetrics | None = None
    agent_completed: int | None = None
    agent_total: int | None = None
    judge_completed: int | None = None
    judge_total: int | None = None
    sampling_integrity: dict[str, Any] | None = None
    progress: PhaseProgressProjection | None = None
    scanned_count: int | None = None


class RunMetricsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    summary: RunSummaryProjection | None = None
    read_revision: str | None = None
    history_mode: str | None = None
    persistence: dict[str, Any] | None = None
    metrics_contract_version: int | None = None
    build_revision: str | None = None
    runtime_snapshot: dict[str, Any] | None = None
    run_valid: bool | None = None
    model_identity: dict[str, Any] | None = None
    execution_budgets: dict[str, Any] | None = None
    snapshot_source: str | None = None
    qa_count: int | None = None
    telemetry_live: TelemetryProjection | None = None
    phases: dict[str, PhaseMetricsProjection | None] | None = None


class JudgeObservation(BaseModel):
    model_config = ConfigDict(extra="allow")
    operational_issue: OperationalIssue | None = None
    # Preserve legacy raw values; validation belongs to the versioned domain rule.
    score: Any = None
    score_normalization_version: int | None = None
    score_normalization: dict[str, Any] | None = None
    score_derivation: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] | None = None
    consistency_version: int | None = None
    consistency_status: str | None = None


class RunItemObservation(BaseModel):
    model_config = ConfigDict(extra="allow")
    operational_issue: OperationalIssue | None = None
    judge_issue: OperationalIssue | None = None
    evaluation_status: str | None = None
    execution_status: str | None = None
    judge_status: str | None = None
    judge_dispatch_status: str | None = None
    judge: JudgeObservation | None = None


class RunItemResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    item: RunItemObservation | None = None


class RunItemsResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    items: list[RunItemObservation] | None = None


class RuntimeConnectionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    sentrix_url: str | None = None
    model_base_url: str | None = None
    endpoint_model: str | None = None
    embedding: dict[str, Any] | None = None
    product: list[dict[str, Any]] | None = None
    product_settings: dict[str, Any] | None = None


class RuntimeConfigResponse(BaseModel):
    model_config = ConfigDict(extra="allow")
    runtime_config: RuntimeConnectionResponse

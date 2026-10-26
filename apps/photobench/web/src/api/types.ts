/**
 * PhotoBench 前端的领域形状。
 *
 * 契约出处：`packages/contracts/openapi/photobench.yaml`。**那份 openapi 对响应体只声明
 * `additionalProperties: true`**（它证明「有这条路径」，不证明形状），所以这里的字段
 * 逐个来自两处实证，注释里指到具体来源：
 *
 *   - 2.0 后端：`apps/photobench/src/photobench/`，以及 8772 上跑出来的真实响应；
 *   - 1.0 前端消费处：`services/photobench/frontend/src/App.vue`（只读权威）。
 *
 * **纪律**：形状不确定就不编——大对象一律留 `[key: string]: unknown` 兜住未来切片要读的
 * 字段，而不是凭 1.0 的用法猜一个必填。后端是权威，前端类型跟着它，不反过来。
 *
 * 状态码/文案类字段（`status`、`mode`、`reason`…）**故意用 `string` 而不是联合类型**：
 * 后端会加取值，收窄成联合类型会让一个新取值变成一次编译失败，而它不是错误。
 */

// ---------------------------------------------------------------------------
// 媒体
// ---------------------------------------------------------------------------

export type MediaType = "image" | "video";

/** 一处媒体引用。1.0 `mediaRefs`（`App.vue:394`）与 run item 的 `*_media_refs` 同形。 */
export interface MediaRef {
  media_type: MediaType;
  media_id: string;
  /** `GET /api/assets/{id}/file` 需要的 id；解析不到时为缺省（1.0 `imageUrl` `:553`）。 */
  asset_id?: string;
  file_name?: string;
  /** resolve 之后的绝对地址（qa-dataset 的 `*_media_refs` 带它）。 */
  media_url?: string;
  /** `ok` / `missing` / `ambiguous`（`resolve_qa_rows`）。 */
  mapping_status?: string;
  /** 视频关键帧取证据的秒数（`pauseVideoAtEvidenceFrame` `:639`）。 */
  source_timestamp_sec?: number | null;
  source_keyframe_asset_id?: string | null;
  /** 1.0 `itemMedia` 附的召回标记。 */
  matched?: boolean;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// 阶段与进度
// ---------------------------------------------------------------------------

/** `phases.<key>.progress`；键取自一次真实 run 的 `qa_eval`（`/api/v1/runs` 实测）。 */
export interface ImportObservation {
  operation_id?: string;
  scope_id?: string;
  status?: string;
  stage?: string;
  total?: number | null;
  completed?: number | null;
  imported?: number | null;
  deduplicated?: number | null;
  failed?: number | null;
  current_file?: string | null;
  updated_at?: string | null;
  observed_at?: string | null;
  sequence?: number;
  error?: string | null;
}

export interface PhaseProgress {
  operation_id?: string;
  observation?: ImportObservation | null;
  availability?: string;
  query_error?: string | null;
  last_received_at?: string;
  last_checked_at?: string;
  request_status?: string;
  request_error?: string | null;
  observer_stop_pending?: boolean;
  current_directory?: string;
  request_started_at?: string;
  directories_done?: number;
  directories_total?: number;

  total?: number | null;
  processed?: number | null;
  failed?: number | null;
  skipped?: number | null;
  completed?: number | null;
  agent_completed?: number | null;
  agent_total?: number | null;
  agent_in_flight?: number;
  /** Judge 已结束的题数（含评分失败）；有效评分数另见 summary.judge_valid_count。 */
  judge_completed?: number | null;
  judge_total?: number | null;
  judge_submitted?: number;
  judge_skipped?: number | null;
  judge_in_flight?: number;
  qa_concurrency?: number | null;
  judge_concurrency?: number | null;
  [key: string]: unknown;
}

/**
 * 一个执行阶段。阶段键见 1.0 `EXECUTION_PHASES`（`App.vue:5`）：`model_deploy` /
 * `scope_setup` / `identity_seed` / `photo_import` / `pipeline_processing` / `qa_eval`，
 * 另有 `gpu_metrics` 这种非执行阶段。
 */
export interface Phase {
  scanned_count?: number | null;
  sampling_integrity?: {
    callback_failures?: number;
    last_callback_failure?: string | null;
    publisher_failures?: number;
    last_publisher_failure?: string | null;
    collection_failures?: number;
    last_collection_failure?: string | null;
    missed_intervals?: number;
    stop_pending_threads?: string[];
  };
  key?: string;
  label?: string;
  status?: string;
  progress?: PhaseProgress;
  /** `qa_eval` 上的计时字段，`effectiveRunSummary` 读它（`App.vue:426`）。 */
  agent_phase_wall_ms?: number | null;
  agent_completed?: number | null;
  agent_total?: number | null;
  /** 兼容历史存放在阶段外层的 Judge 计数。 */
  judge_completed?: number | null;
  judge_total?: number | null;
  judge_phase_wall_ms?: number | null;
  judge_concurrency?: number | null;
  [key: string]: unknown;
}

export type RunPhases = Record<string, Phase | undefined>;

// ---------------------------------------------------------------------------
// 遥测
// ---------------------------------------------------------------------------

/**
 * `run.telemetry_live`（1.0 `telemetryLiveState` `:992`）。`latest` / `peak` 的键是各类
 * `*_mib` / `*_pct` / `*_c` 指标名（见 `liveTelemetryRows` `:942`），取值随 source 变，
 * 所以按 `Record<string, unknown>` 收——**不逐指标建模**，那会是几十个只在特定 source
 * 下存在的可空字段。
 */
export interface TelemetryLive {
  memory_observation_version?: number;
  memory_observations?: Record<string, MemorySourceObservation>;
  memory_dependencies?: Record<string, string[]>;
  sampling_diagnostics?: Record<string, unknown>;
  device_freshness_version?: number;
  device_observation_ttl_seconds?: number | null;
  metrics_contract_version?: number;
  memory_accounting?: "nvidia_pss_and_vram" | "jetson_pss_plus_nvmap_estimate" | "apple_capacity_estimate";
  memory_complete?: boolean;
  field_stats?: Record<string, { peak?: number; mean?: number; p95?: number; valid_count?: number }>;
  rejected_samples?: Record<string, number>;
  sampling?: Record<string, unknown>;
  source?: string;
  status?: string;
  samples_count?: number;
  current_phase?: string;
  latest?: Record<string, unknown>;
  peak?: Record<string, unknown>;
  history?: Array<Record<string, unknown>>;
  all_processes?: Array<Record<string, unknown>>;
  phase_snapshots?: Record<string, Record<string, unknown>>;
  [key: string]: unknown;
}

export interface MemorySourceObservation {
  source?: string;
  status?: string;
  reason?: string | null;
  observed_at_monotonic?: number | null;
  finished_at_monotonic?: number | null;
  ttl_seconds?: number | null;
  age_seconds?: number | null;
  fresh?: boolean;
  cache_hit?: boolean;
  pid?: number;
  start_ticks?: string | null;
}

// ---------------------------------------------------------------------------
// 运行
// ---------------------------------------------------------------------------

/** run 指标汇总。后端 `summary` 的字段是开放的，前端 `effectiveRunSummary` 现算 60+ 项。 */
export interface RunSummary {
  run_valid?: boolean | null;
  completed?: number;
  total?: number;
  judge_valid_count?: number;
  judge_distribution?: Record<string, number>;
  answer_quality_mean?: number | null;
  exact_accuracy?: number | null;
  retrieval_recall_macro?: number | null;
  retrieval_recall_mean?: number | null;
  media_retrieval_recall_macro?: number | null;
  [key: string]: unknown;
}

export interface GraphReferenceMetrics {
  available?: boolean;
  reference_mode?: string;
  reference_edge_count?: number;
  evaluable_predicted_edge_count?: number;
  reference_edge_true_positive_count?: number;
  reference_edge_precision?: number | null;
  reference_edge_recall?: number | null;
  reference_edge_f1?: number | null;
  precision?: number | null;
  recall?: number | null;
  f1?: number | null;
  gt_asset_count?: number;
  identified_image_count?: number;
  valid_edge_rate?: number | null;
  edge_consistency_rate?: number | null;
  edge_attribute_support_rate?: number | null;
  edge_evidence_support_rate?: number | null;
  duplicate_edge_rate?: number | null;
  node_connected_rate?: number | null;
  unverifiable_edge_ratio?: number | null;
  node_count?: number;
  edge_count?: number;
  independent_ground_truth_available?: boolean;
  note?: string;
  reason?: string;
  [key: string]: unknown;
}

export interface FaceClusteringMetrics {
  available?: boolean;
  metric_definition_version?: number;
  identity_gallery?: GraphReferenceMetrics;
  image_identity_links?: GraphReferenceMetrics;
  gt_single_image_count?: number;
  single_face_detection_rate?: number | null;
  asset_name_coverage_count?: number;
  identity_gallery_seed_count?: number;
  verified_detection_image_count?: number;
  gt_multi_identity_image_count?: number;
  reason?: string;
  [key: string]: unknown;
}

export interface GraphQaByTypeRow {
  type?: string;
  label?: string;
  sample_count?: number;
  graph_routed_count?: number;
  graph_enabled_count?: number;
  graph_effect_count?: number;
  graph_recall_delta?: number | null;
  graph_gt_improved_count?: number;
  graph_gt_worsened_count?: number;
  graph_rank_changed_count?: number;
  graph_candidate_set_changed_count?: number;
  telemetry_status?: string;
  retrieval_recall?: number | null;
  retrieval_precision?: number | null;
  retrieval_f1?: number | null;
  answer_quality_mean?: number | null;
  evidence_supported_rate?: number | null;
  [key: string]: unknown;
}

export interface GraphQuality {
  status?: string;
  run_id?: string;
  scope_id?: string;
  graph?: GraphReferenceMetrics;
  face_clustering?: FaceClusteringMetrics;
  graph_qa_by_type?: GraphQaByTypeRow[];
  [key: string]: unknown;
}

/** 重评分任务（`activeRejudge` `App.vue:317`）。 */
export interface RejudgeTask {
  status?: string;
  rejudge_id?: string;
  total?: number;
  completed?: number;
  [key: string]: unknown;
}

/** 一条 run。字段来自 `/api/v1/runs` 实测响应（2026-10-06，8771）。 */
export interface Run {
  read_revision?: string | null;
  history_mode?: "full" | "checkpoint" | null;
  persistence?: { status?: string; error?: string } | null;
  metrics_contract_version?: number;
  build_revision?: string;
  runtime_snapshot?: Readonly<Record<string, unknown>> | null;
  run_id: string;
  /** `full` / `reuse` / `build`（`RUN_MODES_UI` `:2540`）。 */
  mode?: string;
  scope_source?: string;
  scope_id?: string;
  scope_name?: string;
  album_id?: string;
  model_profile?: string;
  model_name?: string | null;
  endpoint_model?: string;
  model_identity?: Record<string, unknown> | null;
  execution_budgets?: Record<string, unknown> | null;
  snapshot_source?: string;
  model_source?: string;
  model_backend?: string;
  qa_set?: string;
  qa_name?: string;
  qa_count?: number | null;
  status?: string;
  created_at?: string;
  started_at?: string;
  finished_at?: string;
  current_phase?: string;
  telemetry_source?: string;
  telemetry_live?: TelemetryLive;
  runtime_framework?: string;
  item_count?: number;
  judge_concurrency?: number;
  summary?: RunSummary;
  phases?: RunPhases;
  rejudge?: RejudgeTask | null;
  /** 详情响应的可选内嵌条目；`effectiveRunSummary` 会读它（`App.vue:430`）。 */
  items?: RunItemSummary[];
  [key: string]: unknown;
}

/** `GET /api/runs` 的信封（`loadRuns` `App.vue:2233`）。 */
export interface RunPage {
  runs: Run[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
  has_previous: boolean;
  has_next: boolean;
  active_count: number;
  /** 后台扫描 270 个 run 时的进度字段（`/api/v1/runs` 的 `scan`）。 */
  scan?: Record<string, unknown>;
  [key: string]: unknown;
}

/** `GET /api/runs` 的查询参数。 */
export interface RunListParams {
  page?: number;
  pageSize?: number;
}

// ---------------------------------------------------------------------------
// 逐题结果
// ---------------------------------------------------------------------------

/** Judge 评分（`judge`）。列表信封里只有前四格，详情里有 `reason`（实测）。 */
export interface Judge {
  score?: number | null;
  status?: string | null;
  rejudge_id?: string | null;
  consistency_status?: string | null;
  consistency_version?: number | null;
  score_normalization_version?: number | null;
  score_normalization?: { rule_version: number; input_type: string; status: string; converted: boolean; score: number | null } | null;
  score_derivation?: { rule_version: number; source: string; score: number | null; source_sha256?: string } | null;
  attempts?: Array<Record<string, unknown>> | null;
  reason?: string | null;
  [key: string]: unknown;
}

/** 任务行为判定（`task_judge` / `task_judges[]`，`taskDecisionLabel` `:600`）。 */
export interface TaskJudge {
  expected_action?: string;
  actual_action?: string;
  correct?: boolean;
  execution_failure?: boolean;
  reason?: string;
  [key: string]: unknown;
}

/** 一行逐题摘要（`/api/v1/runs/{id}/items` 的 `items[]`）。 */
export interface RunItemSummary {
  index: number;
  qa_id?: string;
  question?: string;
  task_type?: string;
  question_type?: string;
  tags?: string[];
  angle?: string;
  difficulty?: string;
  answerability?: string;
  agent_status?: string;
  judge?: Judge;
  task_judge?: TaskJudge;
  task_judges?: TaskJudge[];
  [key: string]: unknown;
}

/** 一次对话轮（`conversationTurns` `:617`）。 */
export interface ConversationTurn {
  conversation_id?: string;
  context_turn_count?: number;
  message?: string;
  expected_action?: string;
  reference_answer?: string;
  [key: string]: unknown;
}

/** 检索计数三元组（`microMetrics` `:402` 读 `<prefix>_retrieval_counts`）。 */
export interface RetrievalCounts {
  gt?: number;
  predicted?: number;
  matched?: number;
  [key: string]: unknown;
}

/** 一题的完整记录（`/api/v1/runs/{id}/items/{index}` 的 `item`）。 */
export interface RunItemDetail extends RunItemSummary {
  conversation?: ConversationTurn[];
  conversation_id?: string;
  runtime_turns?: Array<Record<string, unknown>>;
  agent_stability?: Record<string, unknown>;
  guard_debug?: Record<string, unknown>;
  timing_breakdown?: Record<string, unknown>;
  termination_reason?: string;
  turn_outcome?: string;
  retrieval_media_refs?: MediaRef[];
  retrieval_image_ids?: string[];
  retrieval_video_ids?: string[];
  answer_evidence_media_refs?: MediaRef[];
  predicted_media?: MediaRef[];
  retrieved_candidate_media?: MediaRef[];
  evidence_source_media?: MediaRef[];
  gt_media?: MediaRef[];
  gt_images?: MediaRef[];
  matched_file_names?: string[];
  retrieved_file_names?: string[];
  retrieval_image_retrieval_counts?: RetrievalCounts;
  media_retrieval_counts?: RetrievalCounts;
  image_retrieval_counts?: RetrievalCounts;
  video_retrieval_counts?: RetrievalCounts;
  [key: string]: unknown;
}

/** `GET /api/runs/{id}/items` 的信封（`loadQaPage` `App.vue:2254`）。 */
export interface ItemPage {
  items: RunItemSummary[];
  page: number;
  page_size: number;
  total: number;
  pages: number;
  [key: string]: unknown;
}

/** `GET /api/runs/{id}/items/{index}` 的信封（`loadQaDetail` `App.vue:2348`）。 */
export interface ItemDetailResponse {
  index: number;
  item: RunItemDetail;
  [key: string]: unknown;
}

/** `GET /api/runs/{id}/reviews` 的信封（`loadActiveRun` `App.vue:2296`）。 */
export interface ReviewsResponse {
  reviews?: Record<string, { verdict?: string; note?: string }>;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// 关键帧分析
// ---------------------------------------------------------------------------

/** 关键帧分析（`GET /api/runs/{id}/keyframe-analysis`，1.0 `:7724`；2.0 `reports.py`）。 */
export interface KeyframeAnalysis {
  /** `ready` 才渲染面板（1.0 `memoryValidityArchitecture` `:41`）。 */
  status?: string;
  run_id?: string;
  scope_id?: string;
  asset_source_note?: string;
  metric_scope?: string;
  assets?: {
    total?: number;
    source_videos?: number;
    keyframes?: number;
    videos_with_keyframes?: number;
    keyframes_per_video_mean?: number | null;
    [key: string]: unknown;
  };
  processing?: {
    source_duration_sec?: number | null;
    raw_frame_count?: number | null;
    processing_seconds_total?: number | null;
    processing_seconds_mean?: number | null;
    realtime_factor?: number | null;
    retained_frame_ratio?: number | null;
    compression_ratio?: number | null;
    timing_source?: string;
    [key: string]: unknown;
  };
  retrieval?: {
    candidate_keyframes?: number;
    candidate_relevant_keyframes?: number;
    candidate_precision?: number | null;
    video_parent_recall?: number | null;
    video_gt_targets?: number;
    video_gt_targets_hit_by_keyframe?: number;
    candidate_items?: number;
    candidate_hit_items?: number;
    candidate_item_hit_rate?: number | null;
    predicted_keyframes?: number;
    predicted_relevant_keyframes?: number;
    predicted_precision?: number | null;
    predicted_items?: number;
    [key: string]: unknown;
  };
  temporal?: {
    videos_sampled?: number;
    scene_count?: number;
    mean_gap_sec?: number | null;
    mean_span_sec?: number | null;
    worldmm_score_mean?: number | null;
    selection_reasons?: Record<string, unknown>;
    [key: string]: unknown;
  };
  usefulness?: {
    qa_with_keyframe?: number;
    qa_without_keyframe?: number;
    answer_quality_with_keyframe?: number | null;
    answer_quality_without_keyframe?: number | null;
    answer_quality_delta?: number | null;
    event_context_count?: number;
    [key: string]: unknown;
  };
  definitions?: Record<string, string>;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// 记忆有效性
// ---------------------------------------------------------------------------

/** 记忆层级（L0…L4+）的一行（`memoryValidityArchitecture` `:32`）。 */
export interface MemoryLevel {
  key?: string;
  name?: string;
  created?: number;
  effective?: number;
  ineffective?: number;
  rate?: number | null;
  created_detail?: string;
  effective_detail?: string;
  ineffective_detail?: string;
  ineffective_reasons?: Array<{ label?: string; count?: number; meaning?: string }>;
  [key: string]: unknown;
}

/** 命中矩阵：命中/未命中 × Judge 三档（`memoryRecallMatrices` `:80`）。 */
export interface MemoryMatrix {
  hit_correct?: number;
  hit_partial?: number;
  hit_incorrect?: number;
  miss_correct?: number;
  miss_partial?: number;
  miss_incorrect?: number;
  unjudged?: number;
  [key: string]: unknown;
}

/** 归因分支（`tool` / `usage` 及其 `*_all_with_gt` 变体）。 */
export interface MemoryAttributionScope {
  eligible?: number;
  hit_items?: number;
  miss_items?: number;
  matrix?: MemoryMatrix;
  [key: string]: unknown;
}

/**
 * 证据有效性的一块。后端 `evidence_effectiveness_row()` / `event_effectiveness_row()`
 * 的实际形状（`domain/runs/memory_effectiveness.py:611-666`）。
 */
export interface EvidenceEffectiveness {
  key?: string;
  name?: string;
  returned_items?: number;
  effective_items?: number;
  ineffective_items?: number;
  unjudged_items?: number;
  effective_rate_on_returned?: number | null;
  ineffective_rate_on_returned?: number | null;
  definition?: string;
  [key: string]: unknown;
}

/**
 * `evidence_effectiveness.tool` / `.usage`：按媒体类型分的两块
 * （`memory_effectiveness.py:661-669`）。**不是**扁平结构，消费方按
 * `evidenceBlocks[dim.key].image` / `.video_keyframe` 取（1.0 `App.vue:112-116`）。
 */
export interface EvidenceEffectivenessGroup {
  image?: EvidenceEffectiveness;
  video_keyframe?: EvidenceEffectiveness;
  [key: string]: unknown;
}

/** 记忆有效性（`GET /api/runs/{id}/memory-effectiveness`，1.0 `:7728`）。 */
export interface MemoryEffectiveness {
  status?: string;
  error?: string;
  run_id?: string;
  scope_id?: string;
  metric_scope?: string;
  data_quality?: {
    graph_ready?: boolean;
    tool_trace_count?: number;
    tool_success_count?: number;
    preview_count?: number;
    event_context_count?: number;
    note?: string;
    hit_match_source?: string;
    [key: string]: unknown;
  };
  levels?: MemoryLevel[];
  details?: {
    raw_media_returned_assets?: number;
    raw_media_final_evidence_assets?: number;
    visual_calls?: { total?: number; effective?: number; [key: string]: unknown };
    ocr_calls?: { total?: number; effective?: number; [key: string]: unknown };
    requirement_status_counts?: Record<string, number>;
    answer_status_complete?: number;
    answer_correctness?: {
      correct?: number;
      partial?: number;
      incorrect?: number;
      unjudged?: number;
      exact_accuracy?: number | null;
      [key: string]: unknown;
    };
    memory_answer_attribution?: {
      tool?: MemoryAttributionScope;
      usage?: MemoryAttributionScope;
      tool_all_with_gt?: MemoryAttributionScope;
      usage_all_with_gt?: MemoryAttributionScope;
      definitions?: Record<string, string>;
      [key: string]: unknown;
    };
    /** 错误链诊断：形状未逐字段核实（实测响应被截断），留给消费它的切片收窄。 */
    error_chain_diagnosis?: Record<string, unknown>;
    evidence_effectiveness?: {
      tool?: EvidenceEffectivenessGroup;
      usage?: EvidenceEffectivenessGroup;
      event?: EvidenceEffectiveness;
      /** 后端返回的口径说明（`memory_effectiveness.py:672`）。 */
      note?: string;
      [key: string]: unknown;
    };
    [key: string]: unknown;
  };
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// 数据集
// ---------------------------------------------------------------------------

/** `GET /api/manifests` 的一条（`manifest_summary`，`domain/datasets/catalog.py`）。 */
export interface ManifestSummary {
  album_id: string;
  album_name: string;
  face_count: number;
  photo_count: number;
  video_count: number;
  /** 只含题集名字（后端 `list(manifest.get("qa_sets", {}).keys())`）。 */
  qa_sets: string[];
  [key: string]: unknown;
}

/** `GET /api/manifests` 的信封。 */
export interface ManifestPage {
  manifests: ManifestSummary[];
  [key: string]: unknown;
}

/**
 * `GET /api/qa-dataset` 的一行（原始 jsonl 行，`qa_dataset` 裸读，
 * `application/datasets/service.py:93`）。QA 浏览页只读其中少数几列，其余原样带过。
 */
export interface QaRow {
  qa_id?: string;
  question?: string;
  answer?: string;
  tags?: string[];
  question_type?: string;
  task_type?: string;
  angle?: string;
  difficulty?: string;
  answerability?: string;
  expected_action?: string;
  conversation?: ConversationTurn[];
  /** 单题视频题的目标视频 id（`qaIsVideoEvidence` `:2795`）。 */
  video_id?: string;
  retrieval_image_ids?: string[];
  retrieval_video_ids?: string[];
  retrieval_media_refs?: MediaRef[];
  answer_evidence_media_refs?: MediaRef[];
  answer_claims?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

/**
 * `qa-dataset` 的媒体解析状态（`_resolve_qa_media` `application/datasets/service.py:103`）。
 * 前端按 `status` 决定提示「没有媒体」还是「Sentrix 连不上」，所以四种取值都要认。
 */
export interface MediaResolution {
  status: "no_media" | "sentrix_url_missing" | "sentrix_unavailable" | "resolved" | string;
  resolved_count: number;
  missing_count: number;
  ambiguous_count: number;
  scope_id?: string | null;
  total_refs?: number;
  error?: string;
  [key: string]: unknown;
}

/** `GET /api/qa-dataset` 的信封（`loadQaBrowser` `App.vue:2778`）。 */
export interface QaDatasetResponse {
  album_id: string;
  qa_set: string;
  items: QaRow[];
  media_resolution?: MediaResolution | null;
  media_source?: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// 记忆空间 / 可复用基座
// ---------------------------------------------------------------------------

/** 一个 Sentrix memory space（`/api/v1/memory-spaces` 转发，`spaces/service.py`）。 */
export interface MemorySpace {
  id: string;
  name?: string;
  kind?: string;
  status?: string;
  created_at?: string;
  source_path?: string | null;
  updated_at?: string;
  include_in_people?: number;
  [key: string]: unknown;
}

/** 可复用基座（`build_reuse_bases`，`domain/runs/reuse.py`）。 */
export interface ReuseBase {
  base_id: string;
  album_id: string;
  model_profile: string;
  scope_id: string;
  scope_name: string;
  created_at: string;
  source_run_ids: string[];
  ready: boolean;
  [key: string]: unknown;
}

/** `GET /api/memory-spaces` 的信封。 */
export interface MemorySpacesResponse {
  spaces: MemorySpace[];
  reuse_bases: ReuseBase[];
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// 配置
// ---------------------------------------------------------------------------

/** 模型服务的能力探测（`current_model_snapshot` / `currentModelInfo`）。 */
export interface CurrentModelInfo {
  model_id?: string;
  served_model_name?: string;
  served_models?: string[];
  model_base_url?: string;
  manager_available?: boolean;
  manager_error?: string | null;
  selection_required?: boolean;
  verified_at?: string;
  state?: Record<string, unknown>;
  capabilities?: Record<string, unknown>;
  [key: string]: unknown;
}

/**
 * `GET /api/config`。只列 1.0 `init()` / `saveConnectionConfig()`（`App.vue:2418` /
 * `:2733`）确实读到的键，其余原样带过。
 */
export interface BenchConfig {
  cloud_model_profile?: Record<string, unknown>;
  runtime_config?: Record<string, unknown>;
  vllm_targets?: Record<string, Record<string, unknown>>;
  default_vllm_target_id?: string;
  default_vllm_api_url?: string;
  default_vllm_model_base_url?: string;
  default_sentrix_url?: string;
  default_judge_url?: string;
  default_judge_provider_id?: string;
  judge_model?: string;
  judge_prompt?: string;
  custom_judge_prompt?: string;
  judge_providers?: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

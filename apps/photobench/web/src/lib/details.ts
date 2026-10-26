/**
 * run 详情页的派生表。逐条移植 1.0 `services/photobench/frontend/src/App.vue`
 * （只读，唯一权威）的纯函数：
 *
 *   `toolPerformanceRows`(:2065) / `deliveryBreakdown`(:2069) / `shortHash`(:2079) /
 *   `snapshotSummary`(:2080) / `guardSummary`(:2092) / 局部 `normalizedTerminationReason`(:2107) /
 *   `retrievalChannelRows`(:2134) / `phaseSummary`(:2153) /
 *   `phaseErrorDetails`(:2182) / `phaseErrorSummary`(:2188) / `openImage` 的纯半(:2194) /
 *   `pipelineMetricRows`(:2199)。
 *
 * **组件状态显式化**（逻辑一字未改，只把「从哪读」提成参数）：
 *   - 1.0 的 `toolPerformanceRows` / `deliveryBreakdown` 闭包读
 *     `effectiveRunSummary(activeRun.value)`；这里改由调用方把那份 summary 传进来。
 *     `effectiveRunSummary` 属 `lib/runSummary.ts`，本模块**不重复实现**，只读它的
 *     `.tool_performance` / `.delivery_breakdown`（它 `return { ...saved, ...}`，两个字段
 *     都来自 `run.summary`）。
 *   - 1.0 `phaseSummary` 的 qa_eval 兜底读 `activeRun.item_count`，提成第三个参数。
 *   - 1.0 `openImage` 的 DOM 半（写 `lightbox.value`）归组件切片；这里只留纯的 URL/名字计算。
 *
 * **`retrievalChannelRows` 依赖 1.0 的 `itemToolTrace`(:1503)**：权威实现在 `lib/trace.ts`，
 * 这里 import 复用，不再放文件私有副本。
 *
 * **`itemTimingBreakdown`(:2122-2133) 不在本模块**：它的权威实现已移到 `lib/trace.ts`
 * （trace 的时延归因直接消费它），本模块若需要则从那里 import，保证依赖单向
 * `details.ts → trace.ts`。
 */
import { fmtDate, fmtMemory, fmtSeconds, phaseSeconds } from "./format.ts";
import { itemToolTrace } from "./trace.ts";
import { imageUrl } from "../api/media.ts";
import type { MediaLike } from "../api/media.ts";
import type { Phase, RunSummary } from "../api/types.ts";

type Dict = Record<string, unknown>;

function dict(value: unknown): Dict {
  return typeof value === "object" && value !== null ? (value as Dict) : {};
}

// ── 1.0 :2065-2078 工具表现 / 交付拆分 ─────────────────────────────────

/** 1.0 `:2065-2068`。读 `effectiveRunSummary(...).tool_performance`（调用方传入 summary）。 */
export function toolPerformanceRows(
  summary: RunSummary | null | undefined,
): Array<Record<string, unknown> & { name: string }> {
  const performance = dict(summary?.tool_performance);
  return Object.entries(performance).map(([name, metrics]) => ({ name, ...dict(metrics) }));
}

/** `deliveryBreakdown` 的返回形状（1.0 是匿名对象字面量）。 */
export interface DeliveryBreakdown {
  detCount: number;
  detKinds: Array<[string, unknown]>;
  ocrPartial: number;
  ocrReasons: Array<[string, unknown]>;
}

/**
 * 1.0 `:2069-2078`。
 *
 * 口径：存在性判据是「两类交付计数**任一**非零」——`!det && !ocr` 才回 `null`，
 * 不是某个固定分母（1.0 没有分母字段），也不是 `and`。两类计数各自 `|| 0` 兜底。
 */
export function deliveryBreakdown(summary: RunSummary | null | undefined): DeliveryBreakdown | null {
  const raw = summary?.delivery_breakdown;
  if (!raw) return null;
  const bd = dict(raw);
  if (!bd.deterministic_delivery_count && !bd.ocr_partial_count) return null;
  return {
    detCount: (bd.deterministic_delivery_count as number) || 0,
    detKinds: Object.entries(dict(bd.deterministic_delivery_kinds)),
    ocrPartial: (bd.ocr_partial_count as number) || 0,
    ocrReasons: Object.entries(dict(bd.ocr_partial_reasons)),
  };
}

// ── 1.0 :2079-2091 hash / 快照 ─────────────────────────────────────────

/** 1.0 `:2079`。假值回 `-`，否则取前 12 字符。 */
export function shortHash(value: unknown): string {
  return value ? String(value).slice(0, 12) : "-";
}

/** 1.0 `:2080-2091`。 */
export function snapshotSummary(snapshot: unknown): {
  time: string;
  model: string;
  gpu: string;
  memory: string;
  processMemory: string;
} {
  const s = dict(snapshot);
  const gpu = dict(Array.isArray(s.gpu) ? (s.gpu as unknown[])[0] : undefined);
  const process = dict(s.process_memory);
  const state = dict(s.manager);
  return {
    time: s.captured_at ? fmtDate(s.captured_at) : "未记录",
    model: (state.served_model_name as string) || (state.profile as string) || "-",
    gpu: (gpu.name as string) || "-",
    memory: fmtMemory(gpu.memory_used_mib),
    processMemory: fmtMemory(process.process_memory_used_mib),
  };
}

// ── 1.0 :2092-2121 守卫 ────────────────────────────────────────────────

/**
 * 1.0 `:2107-2112`。**任务清单未列，但 `guardSummary` 依赖它**，故一并照搬：
 * `agent_reason` 命中 token 预检 502 正则时给固定文案，否则回 `guard_debug.termination_reason`
 * → `termination_reason` → `-`。（`format.ts` 的 `turnTerminationLabel` 是另一个函数，
 * 走的是不同正则与映射表，**不合并**。）
 */
export function normalizedTerminationReason(item: unknown): string {
  const reason = String(dict(item).agent_reason || "");
  if (/token budget preflight failed|tokenize-current|502 Bad Gateway/i.test(reason)) {
    return "上下文 token 预检失败（tokenize 接口 502）";
  }
  const i = dict(item);
  return (dict(i.guard_debug).termination_reason as string) || (i.termination_reason as string) || "-";
}

/** `guardSummary` 的返回形状。 */
export interface GuardSummary {
  recorded: boolean;
  status: string;
  termination: string;
  recoveries: unknown;
  codes: string;
  deterministic: string;
  ocrPartial: string;
}

/**
 * 1.0 `:2092-2106`。`recoveries` 原样透传（`?? "-"`），`codes` 用 `、` 连接，无则 `无`；
 * `deterministic` 只在 `det.rendered` 为真时给 `kind || 未知`，否则空串（与 `ocrPartial`
 * 的 `unknown` 兜底不同）。
 */
export function guardSummary(item: unknown): GuardSummary {
  const guard = dict(dict(item).guard_debug);
  const codes = Array.isArray(guard.l1_codes) ? (guard.l1_codes as unknown[]) : [];
  const det = dict(guard.deterministic_delivery);
  const delivery = dict(dict(item).delivery_status);
  return {
    recorded: Boolean(Object.keys(guard).length || dict(item).termination_reason || dict(item).agent_status),
    status: (guard.status as string) || (dict(item).agent_status as string) || "-",
    termination: normalizedTerminationReason(item),
    recoveries: guard.recovery_attempts ?? "-",
    codes: codes.length ? codes.join("、") : "无",
    deterministic: det.rendered ? ((det.kind as string) || "未知") : "",
    ocrPartial: delivery.ocr_partial ? ((delivery.ocr_partial_reason as string) || "unknown") : "",
  };
}

// ── 1.0 :2134-2152 检索通道 ────────────────────────────────────────────

/** `retrievalChannelRows` 的一行。 */
export interface RetrievalChannelRow {
  key: string;
  tool_round: number;
  tool_name: string;
  channel: string;
  latency_ms: unknown;
  embedding_ms: unknown;
  candidate_count: unknown;
  status: unknown;
}

/** 1.0 `:2134-2152`。对每个工具轮次的 `retrieval_timing.channels` 展开成一行。 */
export function retrievalChannelRows(item: unknown): RetrievalChannelRow[] {
  const rows: RetrievalChannelRow[] = [];
  itemToolTrace(item).forEach((trace, toolIndex) => {
    const timing = dict(trace.retrieval_timing);
    Object.entries(dict(timing.channels)).forEach(([channel, value]) => {
      const v = dict(value);
      rows.push({
        key: `${toolIndex}-${channel}`,
        tool_round: toolIndex + 1,
        tool_name: (trace.tool as string) || "未知工具",
        channel,
        latency_ms: v.latency_ms,
        embedding_ms: v.embedding_ms,
        candidate_count: v.candidate_count,
        status: v.status,
      });
    });
  });
  return rows;
}

// ── 1.0 :2153-2193 阶段摘要 / 阶段错误 ─────────────────────────────────

/**
 * 1.0 `:2153-2181`。不同阶段的回退各不相同：
 *   - `model_deploy` 只在 `unload_seconds != null` 时走卸载分支，否则落到通用「耗时」；
 *   - `scope_setup` / `identity_seed` / `photo_import` 各有偏好键（`create_seconds` /
 *     `upload_seconds`），取不到就 `-`；
 *   - `pipeline_processing` 只在有 `progress` 时展开；
 *   - `qa_eval` 分「有 progress」与「无 progress 用 itemCount」两条；
 *   - 其余键有墙钟就 `耗时 X`，否则**空串**。
 *
 * 第三个参数 `itemCount` 即 1.0 的 `activeRun.value?.item_count`。
 */
/** Judge 终态计数与有效评分数是不同口径；兼容旧记录的阶段外层字段。 */
export function judgeProgressLabel(phase: unknown): string {
  const p = dict(phase);
  const pr = dict(p.progress);
  if (pr.judge_concurrency === 0) return "Judge 未配置";
  const count = (value: unknown): string =>
    typeof value === "number" && Number.isInteger(value) && value >= 0 ? String(value) : "?";
  const completed = count(pr.judge_completed ?? p.judge_completed);
  const total = count(pr.judge_total ?? p.judge_total ?? pr.total);
  const skipped = typeof pr.judge_skipped === "number" && pr.judge_skipped > 0
    ? ` · 跳过 ${pr.judge_skipped}` : "";
  return `Judge 已结束 ${completed}/${total}${skipped}`;
}

export function phaseSummary(key: string, phase: unknown, itemCount: unknown = null): string {
  if (!phase) return "";
  const p = dict(phase);
  if (key === "model_deploy" && p.unload_seconds != null) {
    return `卸载 ${fmtSeconds(p.unload_seconds)} · 加载 ${fmtSeconds(p.load_seconds)} · 健康检查 ${fmtSeconds(p.health_check_seconds)}`;
  }
  if (key === "scope_setup") return `创建 ${fmtSeconds(phaseSeconds(phase, "create_seconds"))}`;
  if (key === "identity_seed") {
    const relationshipImport = dict(p.family_relationship_import);
    const relationshipText = relationshipImport.requested
      ? ` · 关系 ${relationshipImport.imported ?? 0}/${relationshipImport.requested}`
      : "";
    return `预置 ${p.seeded_count ?? 0} 人${relationshipText} · ${fmtSeconds(phaseSeconds(phase, "upload_seconds"))}`;
  }
  if (key === "photo_import") {
    // **导入墙钟 ≠ 处理时间。** 2.0 的导入是同步的，VLM 观察与事件归属都在这个请求里，
    // 不拆的话 64 分钟全叫「导入」，读起来像复制文件花了 64 分钟。
    const timings = dict(p.timings);
    const parts: string[] = [];
    if (timings.asset_import != null) parts.push(`资产落库 ${fmtSeconds(timings.asset_import)}`);
    if (timings.prepare != null) parts.push(`观察 ${fmtSeconds(timings.prepare)}`);
    if (timings.finish != null) parts.push(`归属 ${fmtSeconds(timings.finish)}`);
    if (timings.summarize != null) parts.push(`总结 ${fmtSeconds(timings.summarize)}`);
    if (timings.stage != null) parts.push(`落库 ${fmtSeconds(timings.stage)}`);
    const breakdown = parts.length ? ` · ${parts.join(" · ")}` : "";
    // Backend timings cover internal work and may exclude request waiting/queueing.
    // Keep the measured phase wall clock authoritative and label the subtotal separately.
    const total = fmtSeconds(phaseSeconds(phase) ?? phaseSeconds(phase, "upload_seconds"));
    const backendTotal = timings.total != null ? ` · 后端计时 ${fmtSeconds(timings.total)}` : "";
    // The synchronous import endpoint does not report media counts until it returns.
    // Keep that interval visibly unknown instead of presenting missing values as zero.
    const count = (value: unknown): string =>
      typeof value === "number" && Number.isInteger(value) && value >= 0 ? String(value) : "?";
    const observation = dict(dict(p.progress).observation);
    if (p.status === "running" && Object.keys(observation).length) {
      return `当前目录已处理 ${count(observation.completed)}/${count(observation.total)} 个媒体 · 新增 ${count(observation.imported)} · 去重 ${count(observation.deduplicated)} · 失败 ${count(observation.failed)} · 阶段墙钟 ${total}`;
    }
    if (typeof p.scanned_count === "number") {
      const completed = typeof p.accepted_count === "number" && typeof p.deduplicated_count === "number" &&
        typeof p.failed_count === "number" ? p.accepted_count + p.deduplicated_count + p.failed_count : null;
      return `已处理 ${count(completed)}/${count(p.scanned_count)} 个媒体 · 新增 ${count(p.accepted_count)} · 去重 ${count(p.deduplicated_count)} · 失败 ${count(p.failed_count)} · 阶段墙钟 ${total}${backendTotal}${breakdown}`;
    }
    const accepted = count(p.accepted_count);
    const totalMedia = count(p.total_media ?? p.total_photos);
    const failed = count(p.failed_count);
    return `导入 ${accepted}/${totalMedia} 个媒体 · 失败 ${failed} · 阶段墙钟 ${total}${backendTotal}${breakdown}`;
  }
  if (key === "pipeline_processing" && p.progress) {
    const progress = dict(p.progress);
    const timings = dict(p.timings);
    const parts: string[] = [];
    if (timings.video_keyframe_extraction != null) parts.push(`关键帧 ${fmtSeconds(timings.video_keyframe_extraction)}`);
    if (timings.video_keyframe_memory != null) parts.push(`帧记忆 ${fmtSeconds(timings.video_keyframe_memory)}`);
    if (timings.memory_graph != null) parts.push(`图构建 ${fmtSeconds(timings.memory_graph)}`);
    const breakdown = parts.length ? ` · ${parts.join(" · ")}` : "";
    return `${progress.processed || 0}/${progress.total || 0} 资产完成 · ${progress.failed || 0} 失败 · ${progress.skipped || 0} 跳过 · 阶段墙钟 ${fmtSeconds(phaseSeconds(phase))}${breakdown}`;
  }
  if (key === "qa_eval") {
    const progress = p.progress;
    if (progress && (dict(progress).completed != null || dict(progress).in_flight != null)) {
      const pr = dict(progress);
      const agentText = `Agent ${pr.agent_completed ?? pr.completed ?? 0}/${pr.agent_total ?? pr.total ?? "?"}`;
      const judgeText = judgeProgressLabel(p);
      const concurrencyText =
        Number(pr.qa_concurrency) > 1 || Number(pr.judge_concurrency) > 1
          ? ` · 并发 Agent ${pr.qa_concurrency ?? "-"} / Judge ${pr.judge_concurrency ?? "-"}`
          : "";
      const failedText = p.failed_count ? ` · 失败 ${p.failed_count}` : "";
      return `${agentText} · ${judgeText}${failedText}${concurrencyText} · ${fmtSeconds(phaseSeconds(phase))}`;
    }
    return `${itemCount || 0} 题 · ${fmtSeconds(phaseSeconds(phase))}`;
  }
  const elapsed = phaseSeconds(phase);
  if (elapsed != null) return `耗时 ${fmtSeconds(elapsed)}`;
  return "";
}

/**
 * 1.0 `:2182-2187`。优先 `error_details` 数组；空则用单条 `error` 合成
 * （`sample_id` 固定 `阶段错误`，`status` 回退 `failed`）。
 */
export function phaseErrorDetails(phase: unknown): unknown[] {
  if (!phase) return [];
  const details = dict(phase).error_details;
  if (Array.isArray(details) && details.length) return details;
  const error = dict(phase).error;
  return error ? [{ sample_id: "阶段错误", status: dict(phase).status || "failed", reason: error }] : [];
}

/** 1.0 `:2188-2193`。跳过数取自 `skipped_asset_count` → `progress.skipped` → 0。 */
export function phaseErrorSummary(phase: unknown): string {
  const count = phaseErrorDetails(phase).length;
  if (!count) return "";
  const skipped = Number(dict(phase).skipped_asset_count || dict(dict(phase).progress).skipped || 0);
  return skipped ? `${count} 条错误记录，${skipped} 个样本已跳过` : `${count} 条错误记录`;
}

// ── 1.0 :2194 打开图片（纯半） ──────────────────────────────────────────

/**
 * 1.0 `:2194` 的纯半：算出 `{ url, name }`，无 URL 时回 `null`（不打开）。
 * DOM 半（写 `lightbox.value`）归组件切片。
 */
export function openImageTarget(image: MediaLike | null | undefined): { url: string; name: string } | null {
  const url = imageUrl(image);
  if (!url) return null;
  return {
    url,
    name: (image?.file_name as string) || (image?.image_id as string) || "图片",
  };
}

// ── 1.0 :2199-2232 管线指标 ────────────────────────────────────────────

type MetricRow = [string, string, string];

/**
 * 1.0 `:2199-2232`。
 *
 * 回退口径（不同字段不一样）：
 *   - 墙钟均值：有有效 `image_count` 且墙钟有限 → `墙钟 ÷ 张数`；否则回
 *     `phase.average_seconds_per_photo`；再没有才 `未记录`（并换第二列文案）。
 *   - 阶段计时行：`stage_timings[key]` 为假值 → **整行消失**（`filter` 掉），不是占位。
 *   - `effective_workers == null` → `-`；`event_count == null` → `-`。
 * 六个计时段里 `vlm_image_description_seconds` 走「累计调用」文案，其余走 `row`。
 */
export function pipelineMetricRows(phase: Phase = {}): MetricRow[] {
  const p = dict(phase);
  const metrics = dict(p.pipeline_metrics);
  const timings = dict(metrics.stage_timings);
  const pipelineTimings = dict(p.timings);
  const imageCount = Number(metrics.image_count);
  const wallSeconds = phaseSeconds(phase);
  const hasImageCount = Number.isFinite(imageCount) && imageCount > 0;
  const averageWallSeconds =
    hasImageCount && Number.isFinite(wallSeconds) ? (wallSeconds as number) / imageCount : p.average_seconds_per_photo;
  const row = (key: string, label: string): MetricRow | null => {
    const value = timings[key];
    if (!value) return null;
    const v = dict(value);
    return [label, fmtSeconds(v.mean_seconds), `总计 ${fmtSeconds(v.sum_seconds)} · P95 ${fmtSeconds(v.p95_seconds)}`];
  };
  const cumulativeCallRow = (key: string, label: string): MetricRow | null => {
    const value = timings[key];
    if (!value) return null;
    const v = dict(value);
    return [
      label,
      `单次均值 ${fmtSeconds(v.mean_seconds)}`,
      `${v.count ?? "-"} 次调用累计 ${fmtSeconds(v.sum_seconds)} · P95 ${fmtSeconds(v.p95_seconds)}；并发调用可使累计耗时大于阶段墙钟`,
    ];
  };
  const explicitTimingRows = (
    [
      ["源媒体模型准备", "source_memory_prepare", "并发 worker 累计耗时"],
      ["源媒体记忆提交", "source_memory_commit", "按目录顺序串行提交"],
      ["关键帧提取", "video_keyframe_extraction", "ffmpeg 抽帧墙钟累计"],
      ["关键帧记忆构建", "video_keyframe_memory", "VLM/OCR/人脸/实体/事实累计"],
      ["事件总结", "event_summarize", "批末事件摘要"],
      ["参与者投影", "participant_projection", "已确认身份投影到事件"],
      ["记忆图构建", "memory_graph", "结构化图节点与边重建"],
      ["因果边判定", "causal_graph", "规则信号筛选与文本模型因果判定；包含在记忆图构建耗时内"],
      ["家庭图谱", "family_graph", "家庭关系推断"],
      ["人物画像", "person_insights", "人物画像收尾"],
    ] as Array<[string, string, string]>
  )
    .filter(([, key]) => pipelineTimings[key] != null)
    .map(([label, key, note]) => [label, fmtSeconds(pipelineTimings[key]), note] as MetricRow);
  const legacyRows = ([
    [
      "评测图片墙钟均值",
      averageWallSeconds == null ? "未记录" : fmtSeconds(averageWallSeconds),
      hasImageCount
        ? `阶段墙钟 ${fmtSeconds(wallSeconds)} ÷ ${imageCount} 张评测图片；不含身份参考图`
        : "历史运行未记录评测图片数，按旧口径展示",
    ],
    [
      "实际图片并发",
      metrics.effective_workers == null ? "-" : `${metrics.effective_workers} 路`,
      `配置 ${metrics.configured_workers ?? "-"} · vLLM 上限 ${metrics.vllm_max_num_seqs ?? "-"}`,
    ],
    [
      "事件总结",
      metrics.event_count == null ? "-" : `${metrics.event_summary_call_count ?? 0}/${metrics.event_count} 次`,
      `批次耗时 ${fmtSeconds(metrics.event_summary_wall_seconds)}`,
    ],
    cumulativeCallRow("vlm_image_description_seconds", "VLM 图片描述调用"),
    row("face_detection_seconds", "人脸检测"),
    row("image_clip_seconds", "图片 CLIP"),
    row("face_clustering_seconds", "人脸归类"),
    row("event_clustering_seconds", "事件聚类"),
    row("text_embedding_seconds", "文本 embedding"),
  ] as Array<MetricRow | null>).filter((r): r is MetricRow => r !== null);
  return [...explicitTimingRows, ...legacyRows];
}

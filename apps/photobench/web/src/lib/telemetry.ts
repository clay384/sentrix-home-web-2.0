// 移植自 1.0 `services/photobench/frontend/src/App.vue`（只读，唯一权威）的遥测与结果指标构造。
//
// 纯函数层：不 import echarts，也不碰 DOM —— `renderTelemetryChart`（1.0 `:1027-1125`）那种
// 命令式生命周期归组件切片；本模块只产出它依赖的数据（形状见 `telemetryChart` 的注释）。
//
// 口径纪律：1.0 用 `||` / `??` / truthiness 区分「缺字段」与「0 / 空串」，每处回退都不一样
// （`-` / `0` / `null` / `未记录` / 未计算 …）。**不要统一**，逐函数尾注 1.0 行号。
//
// 与 1.0 的差异（隐式依赖显式化，沿用 `lib/format.ts` 的既有做法）：
//   - 1.0 直接读组件状态 `activeRun.value`，并调用 `effectiveRunSummary(run)`（`:426-547`，
//     属 `lib/runSummary.ts` 切片，本模块不重写）。这里把它变成显式参数 `run` / `summary`，
//     函数自己不做汇总 —— 调用方负责传入 `effectiveRunSummary(run)` 的结果。
//   - `EXECUTION_PHASES`（1.0 `App.vue:5`）已提到公共常量模块 `lib/constants.ts`，这里 import。
//   - 参数一律收 `unknown` + 运行时 `dict()` 收窄，不 import `../api/types`：与 `format.ts`
//     的模块约定一致（本层只吃运行时 JSON 形状）。

import { EXECUTION_PHASES } from "./constants.ts";
import { fmtMemory, fmtMs, fmtNumber, fmtPct, fmtTokens, operationalCountNote, statusLabel } from "./format.ts";

/** `itemCallMetrics` 的权威实现在 `lib/trace.ts`；这里 re-export 保持本模块对外接口不变。 */
export { itemCallMetrics } from "./trace.ts";

/**
 * 1.0 的 rows 是 `[label, value, note]` 三元组；可选第 4 位 `true` 表示「主指标」高亮。
 * `aggregateMetricRows` / `keyMetricRows` 的行把这一位当 flag 用。
 */
export type MetricRow = [string, string, string, true?];

/** `keyMetricRows` 返回的是对象行，不是元组。 */
export interface KeyMetric {
  label: string;
  value: string;
  note: string;
}

type Dict = Record<string, any>;

/** 1.0 的可选链语义：非对象一律当空对象，取属性得 `undefined`。 */
function dict(value: unknown): Dict {
  return typeof value === "object" && value !== null ? (value as Dict) : {};
}

/** Numeric telemetry never coerces null, booleans or strings into observations. */
export function telemetryNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function telemetryPlatform(raw: unknown): string {
  const value = dict(raw);
  const accounting = value.memory_accounting || dict(value.latest).memory_accounting;
  if (accounting === "apple_capacity_estimate") return "apple_unified";
  if (accounting === "jetson_pss_plus_nvmap_estimate") return "jetson_local_pss";
  if (accounting === "nvidia_pss_and_vram") return "nvidia";
  return value.source || ""; // Compatibility for historical records without a metric definition.
}

// ── 1.0 :785-856 GPU / 内存指标表 ────────────────────────────────────────

/** 1.0 `:785-856`。四套口径：macOS 内存压力 / apple_unified / orin PSS / 默认 NVIDIA。 */
export function gpuMetricRows(rawPhase: unknown = {}): MetricRow[] {
  const phase = dict(rawPhase);
  if (phase.memory_pressure) {
    const mp = dict(phase.memory_pressure);
    const used = dict(phase.memory_used_gib);
    const comp = dict(phase.compressed_gib);
    const swap = dict(phase.swap_used_gib);
    const thermal = dict(phase.thermal_state);
    const cpu = dict(phase.cpu_percent);
    const modelMem = dict(phase.model_process_memory_used_mib);
    const arb = dict(phase.arbiter_summary);
    const arbDist = dict(arb.state_distribution);
    const arbLabel = Object.keys(arbDist).length ? Object.entries(arbDist).map(([k, v]) => `${k}=${v}`).join(" ") : "-";
    const thermalLabel = (v: unknown) => v == null ? "-" : (["nominal", "fair", "serious", "critical"][Math.round(Number(v))] ?? `${v}`);
    return [
      ["内存压力", fmtNumber(mp.mean), `macOS 内存压力等级（0 正常，越高越吃紧），比「用了多少」更能反映内存够不够 · 峰值 ${fmtNumber(mp.peak)} · P95 ${fmtNumber(mp.p95)}`, true],
      ["整机内存占用", used.mean == null ? "-" : `${Number(used.mean).toFixed(2)} GiB`, `整机所有进程合计占用 · 峰值 ${used.peak == null ? "-" : `${Number(used.peak).toFixed(2)} GiB`} · P95 ${used.p95 == null ? "-" : `${Number(used.p95).toFixed(2)} GiB`}`],
      ["压缩内存", comp.mean == null ? "-" : `${Number(comp.mean).toFixed(2)} GiB`, `被 macOS 内存压缩器压住的内存，涨起来说明物理内存已经不够 · 峰值 ${comp.peak == null ? "-" : `${Number(comp.peak).toFixed(2)} GiB`}`],
      ["Swap 用量", swap.mean == null ? "-" : `${Number(swap.mean).toFixed(2)} GiB`, `已换出到磁盘的内存，一开始换页就是内存不足的硬信号 · 峰值 ${swap.peak == null ? "-" : `${Number(swap.peak).toFixed(2)} GiB`}`],
      ["散热状态", thermal.mean == null ? "-" : thermalLabel(thermal.mean), `系统散热降频状态（nominal/fair/serious/critical），进 serious 说明已在降频 · 峰值 ${thermal.peak == null ? "-" : thermalLabel(thermal.peak)} · NSProcessInfo.thermalState`, true],
      ["CPU 占用", cpu.mean == null ? "-" : fmtNumber(cpu.mean, "%"), `整机 CPU 占用（含非测评进程），用于判断是否被其他负载抢核 · 峰值 ${cpu.peak == null ? "-" : fmtNumber(cpu.peak, "%")}`],
      ["模型进程内存", modelMem.mean == null ? "-" : fmtMemory(modelMem.mean), `mlx 推理进程的 RSS；Metal 分配不在 RSS 里，所以这个数偏低 · 峰值 ${modelMem.peak == null ? "-" : fmtMemory(modelMem.peak)}`],
      ["调度状态", arbLabel, `VLM 调度器的状态分布与 worker 扩缩，并发上不去时先看这里是否被调度器限住 · worker_scale 均值 ${arb.worker_scale_mean == null ? "-" : arb.worker_scale_mean} · 预占峰值 ${arb.preempt_count_max ?? 0}`, true],
      ["Import/Agent 活跃峰值", `import ${arb.import_active_peak ?? 0} · agent ${arb.agent_vlm_active_peak ?? 0}`, "采样期内导入与 Agent 同时持有 VLM 令牌的峰值，用于判断两者是否互相抢占"],
      ["采样数量", phase.samples_count == null ? "-" : `${phase.samples_count} 次`, "macOS 系统采样点"],
    ];
  }
  const temp = dict(phase.temperature_c);
  const util = dict(phase.gpu_utilization_pct);
  const memory = dict(phase.memory_used_mib);
  const modelMemory = dict(phase.model_process_memory_used_mib);
  const kvCache = dict(phase.kv_cache_usage_pct);
  const power = dict(phase.power_draw_w);
  const clock = dict(phase.sm_clock_mhz);
  const processLimit = phase.model_process_memory_limit_mib;
  const processLimitLabel = processLimit == null ? "模型进程显存上限" : `${fmtMemory(processLimit)} 上限告警`;
  if (telemetryPlatform(phase) === "apple_unified") return [
    ["主模型占用（footprint+权重）", fmtMemory(dict(phase.model_process_system_memory_used_mib).peak), `均值 ${fmtMemory(dict(phase.model_process_system_memory_used_mib).mean)} · 容量估计（footprint+映射权重），映射不保证驻留，非 GPU 专属显存`, true],
    ["主模型 footprint", fmtMemory(dict(phase.model_process_footprint_mib).peak), "phys_footprint 账本，含压缩等分量"],
    ["主模型映射权重", fmtMemory(dict(phase.model_process_mapped_weights_mib).peak), "映射大小，不能视为精确驻留"],
    ["全机 GPU 在用统一内存", fmtMemory(dict(phase.gpu_in_use_unified_memory_mib).peak), "AGXAccelerator 设备级；包含其他进程，不能归因给主模型"],
    ["全机 GPU 已分配统一内存", fmtMemory(dict(phase.gpu_allocated_unified_memory_mib).peak), "AGXAccelerator 设备级；分配量不等于实际在用量"],
    ["整套产品内存", fmtMemory(dict(phase.product_stack_memory_mib).peak), "Sentrix 周边 + 主模型的占用（footprint+权重映射）加总，即容量估计，PID 去重；共享权重映射可能重复"],
    ["Sentrix 周边", fmtMemory(dict(phase.sentrix_stack_pss_mib).peak), "除主模型外所有 Sentrix 相关进程的占用（footprint+权重映射）加总"],
    ["整机内存", fmtMemory(dict(phase.system_memory_used_mib).peak), "active + wired + compressor；平台特定统计，与 Linux 非同口径"],
    ["GPU 利用率", fmtNumber(util.mean, "%"), `片上 GPU Device Utilization 峰值 ${fmtNumber(util.peak, "%")}`],
    ["GPU 温度", fmtNumber(temp.mean, "°C"), `峰值 ${fmtNumber(temp.peak, "°C")} · macmon，未安装时留空`],
    ["CPU 温度", fmtNumber(dict(phase.cpu_temperature_c).mean, "°C"), `峰值 ${fmtNumber(dict(phase.cpu_temperature_c).peak, "°C")} · macmon，未安装时留空`],
    [phase.power_scope === "macmon_sys_power" ? "系统功耗" : "功耗（范围未记录）", fmtNumber(power.mean, "W"), `采样峰值 ${fmtNumber(power.peak, "W")} · ${phase.power_scope || "历史范围未记录"} · macmon`],
    ["采样数量", phase.samples_count == null ? "-" : `${phase.samples_count} 次`, "统一内存采样；缺失项不补 0"],
  ];
  if (["orin_ssh_pss", "jetson_local_pss"].includes(telemetryPlatform(phase))) {
    // 1.0 `:786-799`：口径由 `memory_profile.scope` 决定。同一台 Orin 上旧 run 是
    // VmRSS-only，新 run 是「主机 smaps Rss + GPU/UMA 页」，写死其中一种都会读错。
    const unified =
      dict(phase.memory_profile).scope === "host_smaps_rss_plus_gpu_uma" ||
      phase.model_process_host_memory_mib != null;
    return [
    [unified ? "主模型占用（smaps Rss + GPU）" : "Orin 模型进程占用（VmRSS）", fmtMemory(dict(phase.model_process_system_memory_used_mib).peak), `均值 ${fmtMemory(dict(phase.model_process_system_memory_used_mib).mean)} · P95 ${fmtMemory(dict(phase.model_process_system_memory_used_mib).p95)} · ${unified ? "smaps RSS + nvmap；nvmap 不可读时仅正残差可作标明来源的估计；未知分量使合计不可用" : "进程驻留，含部分统一内存"}`, true],
    ["整机 RAM 相对本 run 基线", fmtMemory(dict(phase.system_memory_delta_mib).peak), "首个采样为 0；测评中会掺其他进程"],
    ["KV Cache 已用 token", fmtNumber(phase.kv_cache_used_tokens?.mean), `峰值 ${fmtNumber(phase.kv_cache_used_tokens?.peak)}`],
    ["KV Cache 使用率", fmtNumber(kvCache.mean, "%"), `峰值 ${fmtNumber(kvCache.peak, "%")}`],
    ["GPU 利用率", fmtNumber(util.mean, "%"), `tegrastats GR3D 峰值 ${fmtNumber(util.peak, "%")}`],
    [phase.power_scope === "board" ? "整板功耗" : "电源轨功耗", fmtNumber(power.mean, "W"), `${phase.power_rail || "历史未记录电源轨"} · 采样峰值 ${fmtNumber(power.peak, "W")}`],
    ["CPU 温度", fmtNumber(dict(phase.cpu_temperature_c).mean, "°C"), `峰值 ${fmtNumber(dict(phase.cpu_temperature_c).peak, "°C")} · tegrastats cpu@`],
    ["GPU 温度", fmtNumber(temp.mean, "°C"), `峰值 ${fmtNumber(temp.peak, "°C")} · tegrastats gpu@`],
    ["采样数量", phase.samples_count == null ? "-" : `${phase.samples_count} 次`, `实际间隔均值 ${fmtNumber(dict(dict(phase.sampling).interval_seconds).mean, "s")}；缺口 ${dict(phase.sampling).gaps_over_3_seconds ?? "未记录"}；均值为样本均值，峰值为采样峰值`],
    ];
  }
  return [
    ["模型进程显存", fmtMemory(modelMemory.mean), `主模型进程实际占用的显存（NVML 按 PID 归因），不是整卡占用 · 峰值 ${fmtMemory(modelMemory.peak)} · P95 ${fmtMemory(modelMemory.p95)}`, true],
    ["采样数量", phase.samples_count == null ? "-" : `${phase.samples_count} 次`, "GPU 原始采样点数量；采不到的项留空，不补 0"],
    ["GPU 利用率", fmtNumber(util.mean, "%"), `采样期内 GPU 计算单元的忙碌比例均值 · 峰值 ${fmtNumber(util.peak, "%")} · P95 ${fmtNumber(util.p95, "%")}`],
    ["整卡显存", fmtMemory(memory.mean), `整张卡上所有进程的显存总和，含非本次测评的进程 · 峰值 ${fmtMemory(memory.peak)} · P95 ${fmtMemory(memory.p95)}`],
    [processLimitLabel, phase.model_process_over_limit_samples == null ? "-" : `${phase.model_process_over_limit_samples} 次`, processLimit == null ? "Manager 未返回告警阈值" : `模型进程 NVML 占用超过 ${fmtMemory(processLimit)} 的采样次数`],
    ["KV Cache 使用率", fmtNumber(kvCache.mean, "%"), `vLLM 逻辑 KV 池的占用比例，逼近 100% 会开始排队 · 峰值 ${fmtNumber(kvCache.peak, "%")} · P95 ${fmtNumber(kvCache.p95, "%")}`],
    ["GPU 温度", fmtNumber(temp.mean, "°C"), `显卡核心温度（nvidia-smi temperature.gpu）· 峰值 ${fmtNumber(temp.peak, "°C")} · P95 ${fmtNumber(temp.p95, "°C")}`],
    ["CPU 温度", fmtNumber(dict(phase.cpu_temperature_c).mean, "°C"), `CPU 封装温度（k10temp/coretemp），不是显卡温度 · 峰值 ${fmtNumber(dict(phase.cpu_temperature_c).peak, "°C")}`],
    ["GPU 功耗", fmtNumber(power.mean, "W"), `显卡功耗均值 · 峰值 ${fmtNumber(power.peak, "W")} · P95 ${fmtNumber(power.p95, "W")}`],
    ["SM 时钟", fmtNumber(clock.mean, "MHz"), `流处理器运行频率均值，掉频通常意味着撞到功耗墙或温度墙 · 峰值 ${fmtNumber(clock.peak, "MHz")} · P95 ${fmtNumber(clock.p95, "MHz")}`],
  ];
}

// ── 1.0 :857-935 gpu_metrics 相位视图聚合 ────────────────────────────────

/**
 * 1.0 `:857-935`。把 `phases.gpu_metrics` 与实时采样历史合成为一个可渲染的相位对象。
 *
 * 优先采用后端完整样本统计。仅当历史完整时，以有限样本重算均值、采样峰值和 nearest-rank P95；
 * 降采样历史或缺失历史不能恢复均值、分位数。
 */
export function gpuMetricsView(run: unknown): Dict {
  const r = dict(run);
  const phase = dict(dict(r.phases).gpu_metrics);
  const live = telemetryLiveState(run);
  const history = telemetryHistory(run);
  const finished = ["completed", "completed_with_errors", "failed", "cancelled", "interrupted"].includes(r.status);
  const stats = (key: string): Dict => {
    if (live.field_stats && Object.hasOwn(live.field_stats, key)) return dict(live.field_stats[key]);
    // A downsampled curve cannot reconstruct full-resolution statistics.
    if (Number(live.samples_count) > history.length) return {};
    const values = history.map((item) => telemetryNumber(item?.[key])).filter((value): value is number => value !== null);
    if (!values.length) {
      const peak = telemetryNumber(live.peak?.[key]);
      return peak === null ? {} : { peak };
    }
    const sorted = [...values].sort((left, right) => left - right);
    const count = sorted.length;
    return {
      peak: sorted[count - 1],
      mean: values.reduce((sum, value) => sum + value, 0) / count,
      p95: sorted[Math.max(0, Math.ceil(count * 0.95) - 1)],
    };
  };
  const fill = (phaseObj: Dict): Dict => {
    const keys = [
      "model_process_footprint_mib", "model_process_mapped_weights_mib", "product_stack_footprint_mib", "product_stack_mapped_weights_mib",
      "model_process_memory_used_mib",
      "model_process_system_memory_used_mib",
      "gpu_allocated_unified_memory_mib",
      "gpu_in_use_unified_memory_mib",
      "system_memory_delta_mib",
      "sentrix_stack_pss_mib",
      "product_stack_memory_mib",
      "system_memory_used_mib",
      "temperature_c",
      "cpu_temperature_c",
      "gpu_utilization_pct",
      "power_draw_w",
      "kv_cache_usage_pct",
      "kv_cache_used_tokens",
    ];
    const out: Dict = { memory_accounting: live.latest?.memory_accounting, memory_complete: live.latest?.memory_complete,
      power_scope: live.latest?.power_scope, power_rail: live.latest?.power_rail,
      sampling: live.sampling, memory_observation_version: live.latest?.memory_observation_version,
      memory_observations: live.latest?.memory_observations, memory_dependencies: live.latest?.memory_dependencies,
      sampling_diagnostics: live.latest?.sampling_diagnostics, ...phaseObj };
    for (const key of keys) {
      const current = out[key];
      const hasPeak = current && typeof current === "object" && current.peak != null;
      if (!hasPeak) {
        const computed = stats(key);
        if (computed.peak != null) out[key] = computed;
      }
    }
    if (!out.llama_server_args) out.llama_server_args = r.llama_server_args || {};
    return out;
  };
  if (phase.status === "done" || phase.status === "skipped" || (phase.samples_count && !phase.partial)) {
    return fill({ ...phase, partial: phase.partial === true });
  }
  if (phase.samples_count) {
    return fill({
      ...phase,
      status: finished ? (phase.status || "partial") : "running",
      partial: finished ? phase.partial === true || phase.status === "partial" : true,
    });
  }
  if (!history.length && !live.samples_count) return fill(phase);
  return fill({
    status: finished ? "partial" : "running",
    partial: !finished,
    source: live.source || r.telemetry_source || phase.source,
    samples_count: live.samples_count || history.length,
    temperature_c: stats("temperature_c"),
    gpu_utilization_pct: stats("gpu_utilization_pct"),
    memory_used_mib: stats("memory_used_mib"),
    model_process_memory_used_mib: stats("model_process_memory_used_mib"),
    model_process_system_memory_used_mib: stats("model_process_system_memory_used_mib"),
    system_memory_delta_mib: stats("system_memory_delta_mib"),
    kv_cache_usage_pct: stats("kv_cache_usage_pct"),
    kv_cache_used_tokens: stats("kv_cache_used_tokens"),
    power_draw_w: stats("power_draw_w"),
    sm_clock_mhz: stats("sm_clock_mhz"),
  });
}

// ── 1.0 :941-991 实时遥测行 ──────────────────────────────────────────────

/** Recorded source freshness and collection integrity, without synthesizing legacy facts. */
export function telemetryObservationRows(run: unknown): MetricRow[] {
  const live = telemetryLiveState(run);
  const latest = dict(live.latest);
  const sources = Object.entries(dict(latest.memory_observations));
  const rows: MetricRow[] = [];
  if (latest.memory_observation_version === 1) {
    const ages = sources.map(([, value]) => telemetryNumber(dict(value).age_seconds))
      .filter((age): age is number => age !== null && age >= 0);
    const unavailable = sources.filter(([, value]) => dict(value).fresh !== true).map(([key]) => key);
    rows.push(["内存观测值龄", ages.length ? `${Math.max(...ages).toFixed(3)} s` : "不可用",
      "采样时最老来源的值龄；缓存命中不刷新原观测时间"]);
    rows.push(["内存来源有效性", `${sources.length - unavailable.length}/${sources.length}`,
      unavailable.length ? `不可用或过期：${unavailable.join("、")}` : "所有已记录来源均新鲜；各合计另按必要依赖判定"]);
  }
  const diagnostics = dict(latest.sampling_diagnostics);
  const collection = telemetryNumber(diagnostics.collection_seconds);
  const lateness = telemetryNumber(diagnostics.schedule_lateness_seconds);
  if (Object.keys(diagnostics).length) {
    rows.push(["采集 / 调度延迟", `${collection === null ? "不可用" : collection.toFixed(3) + " s"} / ${lateness === null ? "不可用" : lateness.toFixed(3) + " s"}`,
      "单次采集耗时 / 相对单调时钟计划的延迟；不包含后续写盘回调"]);
  }
  const integrity = dict(gpuMetricsView(run).sampling_integrity);
  if (Object.keys(integrity).length) {
    const counts = ["callback_failures", "publisher_failures", "collection_failures"].map((key) => telemetryNumber(integrity[key]));
    const pending = Array.isArray(integrity.stop_pending_threads) ? integrity.stop_pending_threads : [];
    rows.push(["采样记录 / 发布 / 采集异常", counts.map((value) => value === null ? "未记录" : String(value)).join(" / "),
      pending.length ? `停止超时，线程待退出：${pending.join("、")}` : "异常计数独立于被测任务成败；跳过的采样周期另见覆盖统计"]);
  }
  return rows;
}

/**
 * 1.0 `:941-991`。apple_unified 与「其余（NVIDIA / Orin）」两套行；
 * `effectivePeak` 对「老记录只有 latest 没有 peak」做回退 —— 这是本函数最容易漏的回退。
 */
export function liveTelemetryRows(run: unknown): MetricRow[] {
  const r = dict(run);
  const live = dict(r.telemetry_live);
  const latest = dict(live.latest);
  const peak = dict(live.peak);
  // Older persisted runs may have latest values but no peak object.
  const effectivePeak = Object.keys(peak).length ? peak : latest;
  const platform = telemetryPlatform(live);
  const unit = ["jetson_local_pss", "orin_ssh_pss"].includes(platform) ? "PSS" : "显存";
  const fmtLive = (value: unknown, suffix = "") => {
    const number = telemetryNumber(value);
    return number === null ? "-" : `${number.toFixed(2)}${suffix}`;
  };
  const fmtGiB = (value: unknown) => {
    const number = telemetryNumber(value);
    return number === null ? "-" : `${(number / 1024).toFixed(2)} GiB`;
  };
  const scope = latest.benchmark_process_memory_scope || effectivePeak.benchmark_process_memory_scope;
  const productFormulaNote = latest.memory_accounting === "apple_capacity_estimate"
    ? "容量估计：相关进程 footprint + 映射权重，PID 去重；共享映射可能重复，映射不保证驻留"
    : latest.memory_accounting === "nvidia_pss_and_vram"
    ? "相关 PID 的 PSS 合计；GPU 显存单列"
    : scope === "apple_phys_footprint_plus_mapped_weights_related"
    ? "相关进程占用（phys_footprint + 映射的模型权重）加总（含共享页重复，为上界）；不再加 UMA 补偿，非 GPU 专属显存"
    : scope === "apple_phys_footprint_related"
    ? "旧口径（仅 phys_footprint，未计映射权重），系统性偏低；重跑后按新口径显示"
    : scope === "all_related_pss_plus_model_uma_extra"
    ? "所有相关进程 PSS 加总 + 主模型/辅助模型 GPU/UMA 页"
    : "历史记录未标记新公式 scope；该值不能按当前公式解释，需重跑";
  if (platform === "apple_unified") {
    const stage = ["completed", "completed_with_errors", "failed", "cancelled", "interrupted"].includes(r.status)
      ? statusLabel(r.status)
      : (EXECUTION_PHASES.find((item) => item.key === (live.current_phase || r.current_phase))?.label || "运行中");
    return [
      ["当前阶段", stage, `已采样 ${live.samples_count || 0} 次`],
      ["主模型占用（footprint+权重）", fmtGiB(latest.model_process_system_memory_used_mib), `峰值 ${fmtGiB(effectivePeak.model_process_system_memory_used_mib)} · 容量估计（footprint+映射权重），映射不保证驻留，非 GPU 专属显存`],
      ["主模型 footprint", fmtGiB(latest.model_process_footprint_mib), "phys_footprint 账本"],
      ["主模型映射权重", fmtGiB(latest.model_process_mapped_weights_mib), "映射容量，不保证驻留"],
      ["全机 GPU 在用统一内存", fmtGiB(latest.gpu_in_use_unified_memory_mib), `峰值 ${fmtGiB(effectivePeak.gpu_in_use_unified_memory_mib)} · 设备级，非主模型归因`],
      ["全机 GPU 已分配统一内存", fmtGiB(latest.gpu_allocated_unified_memory_mib), `峰值 ${fmtGiB(effectivePeak.gpu_allocated_unified_memory_mib)} · 设备级，不等于实际在用`],
      ["整套产品内存", fmtGiB(latest.product_stack_memory_mib), `峰值 ${fmtGiB(effectivePeak.product_stack_memory_mib)} · ${productFormulaNote}；输入缺失则不记录`],
      ["Sentrix 周边", fmtGiB(latest.sentrix_stack_pss_mib), `峰值 ${fmtGiB(effectivePeak.sentrix_stack_pss_mib)} · 不含主模型，footprint+权重映射加总`],
      ["整机内存", fmtGiB(latest.system_memory_used_mib), `采样峰值 ${fmtGiB(effectivePeak.system_memory_used_mib)} · active + wired + compressor；平台特定统计，与 Linux 非同口径`],
      ["GPU 利用率", fmtLive(latest.gpu_utilization_pct, "%"), `峰值 ${fmtLive(effectivePeak.gpu_utilization_pct, "%")} · macmon gpu_active_ratio`],
      ["GPU 温度", fmtLive(latest.temperature_c, " °C"), `峰值 ${fmtLive(effectivePeak.temperature_c, " °C")} · macmon gpu_temp_avg`],
      ["CPU 温度", fmtLive(latest.cpu_temperature_c, " °C"), `峰值 ${fmtLive(effectivePeak.cpu_temperature_c, " °C")} · macmon cpu_temp_avg`],
      ["系统功耗", fmtLive(latest.power_draw_w, " W"), `峰值 ${fmtLive(effectivePeak.power_draw_w, " W")} · macmon sys_power`],
    ];
  }
  if (unit === "PSS" && (latest.memory_accounting === "jetson_pss_plus_nvmap_estimate" || jetsonUnifiedFromRun(run))) {
    const stage = ["completed", "completed_with_errors", "failed", "cancelled", "interrupted"].includes(r.status)
      ? statusLabel(r.status)
      : (EXECUTION_PHASES.find((item) => item.key === (live.current_phase || r.current_phase))?.label || "运行中");
    return [
      ["当前阶段", stage, `已采样 ${live.samples_count || 0} 次`],
      ["主模型占用（smaps RSS + nvmap）", fmtGiB(latest.model_process_system_memory_used_mib), `采样峰值 ${fmtGiB(effectivePeak.model_process_system_memory_used_mib)} · 归属/分配估计，必要分量未知则合计不可用`],
      ["主模型 smaps RSS", fmtGiB(latest.model_process_host_memory_mib), "主模型相关 PID 的 smaps RSS 合计"],
      ["主模型 GPU/UMA 分量", fmtGiB(latest.model_process_gpu_memory_mib), `${latest.model_process_gpu_memory_source || "来源未记录"}；仅正 VmRSS 残差可作为标明来源的后备估计`],
      ["主模型 PSS", fmtGiB(latest.model_process_memory_used_mib), "进程比例分摊内存，不能替代 smaps RSS + nvmap 合计"],
      ["整套产品内存", fmtGiB(latest.product_stack_memory_mib), `采样峰值 ${fmtGiB(effectivePeak.product_stack_memory_mib)} · 相关 PID 的 PSS + 主/辅助模型 nvmap，PID 去重；归属/分配估计`],
      ["Sentrix 周边 PSS", fmtGiB(latest.sentrix_stack_pss_mib), "不含主模型，包含本地辅助模型进程 PSS"],
      ["辅助模型 GPU/UMA 分量", fmtGiB(latest.sentrix_auxiliary_model_uma_mib), "确认无本地辅助服务时为 0；未知保持不可用"],
      ["整机 RAM", fmtGiB(latest.system_memory_used_mib), "MemTotal − MemAvailable；包括本机其他进程"],
      ["GPU 利用率", fmtLive(latest.gpu_utilization_pct, "%"), `采样峰值 ${fmtLive(effectivePeak.gpu_utilization_pct, "%")} · tegrastats GR3D`],
      ["GPU 温度", fmtLive(latest.temperature_c, " °C"), "tegrastats gpu@"],
      ["CPU 温度", fmtLive(latest.cpu_temperature_c, " °C"), "tegrastats cpu@"],
      [latest.power_scope === "board" ? "整板功耗" : "电源轨功耗", fmtLive(latest.power_draw_w, " W"), `${latest.power_rail || "电源轨未记录"} · ${latest.power_scope || "范围未记录"} · 采样峰值 ${fmtLive(effectivePeak.power_draw_w, " W")}`],
    ];
  }
  return [
    ["当前阶段", ["completed", "completed_with_errors", "failed", "cancelled", "interrupted"].includes(r.status) ? statusLabel(r.status) : (EXECUTION_PHASES.find((item) => item.key === (live.current_phase || r.current_phase))?.label || "运行中"), `已采样 ${live.samples_count || 0} 次`],
    ["主模型 RAM", fmtGiB(latest.model_process_system_memory_used_mib), `峰值 ${fmtGiB(effectivePeak.model_process_system_memory_used_mib)} · 进程系统内存，不代表权重又完整占一份`],
    [unit === "PSS" ? "主模型 PSS（Orin UMA）" : "主模型 GPU 显存", fmtGiB(latest.model_process_memory_used_mib), `峰值 ${fmtGiB(effectivePeak.model_process_memory_used_mib)} · ${unit === "PSS" ? "主模型进程的比例分摊物理内存，不是独立显存" : "nvidia-smi 按主模型 PID 归因"}`],
    ["整套产品 RAM", fmtGiB(latest.product_stack_memory_mib), `峰值 ${fmtGiB(effectivePeak.product_stack_memory_mib)} · ${productFormulaNote}；输入缺失则不记录`],
    ["Sentrix 周边 PSS", fmtGiB(latest.sentrix_stack_pss_mib), `峰值 ${fmtGiB(effectivePeak.sentrix_stack_pss_mib)} · 不含 llama-server，PSS 加总不重叠共享库`],
    ["测评系统 GPU 显存", fmtGiB(latest.benchmark_process_gpu_memory_mib), `峰值 ${fmtGiB(effectivePeak.benchmark_process_gpu_memory_mib)} · nvidia-smi 按 PhotoBench/Sentrix 相关 GPU 进程 PID 汇总；不等于主模型显存`],
    ["整卡显存", fmtGiB(latest.memory_used_mib), `峰值 ${fmtGiB(effectivePeak.memory_used_mib)} · nvidia-smi device memory.used，包含整卡所有进程及驱动/上下文`],
    ["整机 RAM", fmtGiB(latest.system_memory_used_mib), `峰值 ${fmtGiB(effectivePeak.system_memory_used_mib)} · ${latest.system_memory_scope === "host_all_processes" ? "宿主机全部进程" : "未标注范围"}`],
    ["全部 GPU 进程", fmtGiB(latest.all_processes_memory_mib), `峰值 ${fmtGiB(effectivePeak.all_processes_memory_mib)} · nvidia-smi 可见进程总和`],
    ["其他 GPU 进程", fmtGiB(latest.other_processes_memory_used_mib ?? latest.other_processes_memory_mib), `峰值 ${fmtGiB(effectivePeak.other_processes_memory_mib)} · 除模型进程外`],
    ["GPU 利用率", fmtLive(latest.gpu_utilization_pct, "%"), `峰值 ${fmtLive(effectivePeak.gpu_utilization_pct, "%")}`],
    ["GPU 温度", fmtLive(latest.temperature_c, " °C"), `峰值 ${fmtLive(effectivePeak.temperature_c, " °C")} · 153 为独显温度，118 为 tegrastats gpu@`],
    ["CPU 温度", fmtLive(latest.cpu_temperature_c, " °C"), `峰值 ${fmtLive(effectivePeak.cpu_temperature_c, " °C")} · 153 为 k10temp Tctl，118 为 tegrastats cpu@`],
    ["功耗", fmtLive(latest.power_draw_w, " W"), `峰值 ${fmtLive(effectivePeak.power_draw_w, " W")}`],
    ["KV Cache", latest.kv_cache_usage_pct == null ? "未提供" : fmtLive(latest.kv_cache_usage_pct, "%"), latest.kv_cache_used_tokens == null ? "当前框架未暴露运行时 KV 指标" : `峰值 token ${fmtLive(peak.kv_cache_used_tokens)}`],
  ];
}

// ── 1.0 :992-1008 telemetry_live 访问器 ──────────────────────────────────

/** 1.0 `:992-995` —— 非对象（含数组）一律当 `{}`。 */
export function telemetryLiveState(run: unknown): Dict {
  const value = dict(run).telemetry_live;
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

/** 1.0 `:996-999` —— 非数组一律当 `[]`。 */
export function telemetryHistory(run: unknown): Dict[] {
  const history = telemetryLiveState(run).history;
  return Array.isArray(history) ? history : [];
}

/** 1.0 `:1000-1003` —— 不在任务清单里，但属 `:785-1150` 同一遥测块，组件切片要用。 */
export function telemetryProcessList(run: unknown): Dict[] {
  const processes = telemetryLiveState(run).all_processes;
  return Array.isArray(processes) ? processes : [];
}

/** 1.0 `:1004-1008` —— 取 `samples_count`（>0 才认）与历史长度中的较大者。 */
export function telemetrySampleCount(run: unknown): number {
  const value = Number(telemetryLiveState(run).samples_count);
  return Math.max(
    Number.isFinite(value) && value > 0 ? value : 0,
    telemetryHistory(run).length,
  );
}

// ── 1.0 :1011-1026 图表数据 ──────────────────────────────────────────────

/**
 * `telemetryChart` 的返回形状。**这是给组件切片里 `renderTelemetryChart`（1.0 `:1027-1125`）用的**。
 */
export interface TelemetryChartData {
  /** 采样点（不足 2 个时函数返回 `null`，不会给出这个对象）。 */
  history: Dict[];
}

/**
 * 1.0 `:1011-1026`。把采样历史算成图表数据；历史不足 2 点时退回 `phase_snapshots` 的相位快照，
 * 仍不足 2 点返回 `null`（组件据此 dispose 实例）。
 *
 * **输出形状（下一个切片必须照此消费）**：`{ history: Array<Item> }`，`Item` 字段：
 *   - `t`：采样时间戳（回退分支里是数组下标 `i`）。`renderTelemetryChart` 用 `history[0].t` 当
 *     基准算 `+N.N min` 标签；`t` 缺失/非数时退回 `#序号`。
 *   - `phase`：所属执行阶段的 key（缺省 `"unassigned"`）；`phase_label`：可选的中文标签。
 *     两者用于 tooltip 的「阶段：」与 x 轴 markArea 的分段着色。
 *   - 各内存指标键（以 `_mib` 结尾）：`memory_used_mib` / `system_memory_used_mib` /
 *     `benchmark_process_gpu_memory_mib` / `product_stack_memory_mib` / `sentrix_stack_pss_mib` /
 *     `model_process_memory_used_mib` / `model_process_system_memory_used_mib` 等。
 *     值是 **MiB**；`renderTelemetryChart` 逐点 `/ 1024` 转 GiB，并对非有限数取 `null`（断线不连）。
 *     `phase_snapshots` 回退分支只并入 `latest || peak`，且**只保留含 `_mib` 键的快照**。
 *   - `renderTelemetryChart` 自己另取 `source`（`telemetryLiveState(run).source` →
 *     `run.telemetry_source` → `run.phases.gpu_metrics.source`）决定画哪几条线，**不来自本返回值**。
 */
export function telemetryChart(run: unknown): TelemetryChartData | null {
  let history = telemetryHistory(run);
  // Legacy runs may only have phase snapshots. Render a compact phase trend
  // instead of leaving the chart area blank.
  if (history.length < 2) {
    const phases = telemetryLiveState(run).phase_snapshots;
    const phaseEntries = phases && typeof phases === "object" && !Array.isArray(phases) ? Object.entries(phases) : [];
    history = phaseEntries.map(([phaseKey, phase], i) => ({
      t: i,
      phase: phaseKey,
      ...(dict(phase).latest || dict(phase).peak || {}),
    })).filter((item) => Object.keys(item).some((key) => key.endsWith("_mib")));
  }
  if (history.length < 2) return null;
  return { history };
}

/**
 * 这次 Orin/Jetson run 的主模型内存口径是不是「主机 smaps Rss + GPU/UMA 页」。
 *
 * 1.0 `:789-797` 的 `jetsonUnifiedFromPhase` / `jetsonUnifiedFromRun`。**作用域来自
 * 后端**（`memory_profile.scope === "host_smaps_rss_plus_gpu_uma"`），不是前端的
 * 猜测：同一台 Orin 上旧的 VmRSS-only run 与新 run 并存，硬编码其中一种都会把另一
 * 种的历史读错。
 *
 * 2.0 此前三处标签全部写死成 VmRSS，等于把所有 run 都当成旧口径。
 */
export function jetsonUnifiedFromRun(run: unknown): boolean {
  const r = dict(run);
  if (dict(r.memory_profile).scope === "host_smaps_rss_plus_gpu_uma") return true;
  const gpu = dict(dict(r.phases).gpu_metrics);
  if (dict(gpu.memory_profile).scope === "host_smaps_rss_plus_gpu_uma") return true;
  // 兼容本次实测已落盘、但 memory_unit 仍是旧值的 run：拆分字段存在就说明
  // 后端采的是 smaps Rss + GPU，不能继续按 VmRSS 解释。
  if (gpu.model_process_host_memory_mib != null) return true;
  const latest = dict(dict(r.telemetry_live).latest);
  return latest.model_process_host_memory_mib != null;
}

// ── 1.0 :1126-1150 相位快照行 ────────────────────────────────────────────

/** 1.0 `:1126-1150`。**故意不含 apple_unified**：Orin 判据是 `isOrin = isApple || ...`。 */
export function liveTelemetryPhaseRows(run: unknown): MetricRow[] {
  const r = dict(run);
  const live = telemetryLiveState(run);
  const source = telemetryPlatform(live) || r.telemetry_source || telemetryPlatform(dict(dict(r.phases).gpu_metrics));
  const isApple = source === "apple_unified";
  const isOrin = isApple || ["orin_ssh_pss", "jetson_local_pss"].includes(source);
  const modelKey = isOrin ? "model_process_system_memory_used_mib" : "model_process_memory_used_mib";
  const modelLabel = isApple
    ? "主模型占用（footprint+权重）"
    : isOrin
      ? jetsonUnifiedFromRun(run)
        ? "主模型占用（smaps Rss + GPU/UMA 页）"
        : "主模型 VmRSS"
      : "主模型 GPU";
  const history = telemetryHistory(run);
  const phases = dict(r.telemetry_live?.phase_snapshots);
  const peakFromHistory = (phaseKey: string) => {
    if (Number(live.samples_count) > history.length) return null;
    const values = history
      .filter((item) => (item.phase || "unassigned") === phaseKey)
      .map((item) => telemetryNumber(item[modelKey]))
      .filter((value): value is number => value !== null);
    return values.length ? Math.max(...values) : null;
  };
  return Object.entries(phases).map(([key, value]) => {
    const label = EXECUTION_PHASES.find((item) => item.key === key)?.label || key;
    const peak = dict(dict(value).peak);
    const modelPeak = telemetryNumber(peak[modelKey]) ?? peakFromHistory(key);
    const model = modelPeak == null ? "-" : `${(Number(modelPeak) / 1024).toFixed(2)} GiB`;
    const cardPeak = telemetryNumber(peak.memory_used_mib);
    const hostPeak = telemetryNumber(peak.system_memory_used_mib);
    const card = cardPeak === null ? "-" : `${(cardPeak / 1024).toFixed(2)} GiB`;
    const host = hostPeak === null ? "-" : `${(hostPeak / 1024).toFixed(2)} GiB`;
    return [label, model, `${modelLabel}峰值 · 整卡 ${card} · 整机 RAM ${host} · ${dict(value).samples_count || 0} 次`];
  });
}

// ── 1.0 :1151-1165 可比显存画像 ──────────────────────────────────────────

/**
 * 1.0 `:1151-1165`。三档 `source`：`replay`（run 自带 `memory_profile`）→ `pending`
 * （无 gpu_metrics 相位 / 相位里没有 `memory_profile`）→ `gpu_metrics`（正式采样）。
 * 可比性由 `memory_method` 与 `source` 共同决定（见 `memoryProfileRows`）。
 */
export function comparableMemoryProfile(run: unknown): Dict | null {
  if (!run) return null;
  const r = dict(run);
  if (r.memory_profile) return { ...r.memory_profile, source: "replay" };
  const gpu = dict(r.phases).gpu_metrics;
  if (!gpu) return { source: "pending", status: "pending", memory_profile: {}, questions_completed: r.summary?.completed, questions_total: r.summary?.total };
  if (!dict(gpu).memory_profile) return { source: "pending", status: gpu.status, memory_profile: {}, questions_completed: r.summary?.completed, questions_total: r.summary?.total };
  const profile = { ...dict(gpu.memory_profile) };
  if (dict(gpu).model_process_host_memory_mib != null && profile.method === "orin_process_pss_uma_v1") {
    // 本次实测已落盘、但聚合时还没写新 method 的 run：按拆分字段纠偏，
    // 否则卡片会继续按 PSS/VmRSS 口径解释 smaps+GPU 的数字。
    profile.method = "orin_smaps_rss_plus_gpu_uma_v1";
    profile.memory_unit = "host_smaps_rss_plus_gpu_mib";
    profile.scope = "host_smaps_rss_plus_gpu_uma";
  }
  return {
    status: gpu.status,
    source: "gpu_metrics",
    memory_accounting: gpu.memory_accounting,
    memory_profile: profile,
    model_process_memory_used_mib: gpu.model_process_memory_used_mib,
    model_process_system_memory_used_mib: gpu.model_process_system_memory_used_mib,
    questions_completed: r.summary?.completed,
    questions_total: r.summary?.total,
  };
}

// ── 1.0 :1176-1211 显存画像行 ────────────────────────────────────────────

/** 1.0 `:1176-1211`。三种 `memory.method` 各一套行：macos / orin / vLLM 估算。 */
export function memoryProfileRows(rawProfile: unknown = {}): MetricRow[] {
  const profile = dict(rawProfile);
  const memory = dict(profile.memory_profile);
  const isBenchmarkGpuProfile = profile.source === "gpu_metrics";
  if (memory.method === "macos_unified_memory_v1") {
    return [
      ["内存占用峰值", memory.memory_used_peak_gib == null ? "-" : `${Number(memory.memory_used_peak_gib).toFixed(2)} GiB`, "16GB 统一内存整机峰值", true],
      ["模型进程空闲占用", memory.idle_model_process_memory_gib == null ? "-" : `${Number(memory.idle_model_process_memory_gib).toFixed(2)} GiB`, "mlx 进程 RSS 采样最小值"],
      ["Swap 峰值", memory.swap_used_peak_gib == null ? "-" : `${Number(memory.swap_used_peak_gib).toFixed(2)} GiB`, "换页压力信号"],
      ["压缩内存峰值", memory.compressed_peak_gib == null ? "-" : `${Number(memory.compressed_peak_gib).toFixed(2)} GiB`, "macOS 内存压缩器峰值"],
      ["复测进度", `${profile.questions_completed ?? 0}/${profile.questions_total ?? 0} 题`, `请求失败 ${profile.failed_requests ?? 0} · 答案不保存`],
      ["原测评数据一致性", profile.items_integrity_ok === true ? "通过" : profile.status === "completed" ? "未通过" : "待完成", profile.answers_persisted === false ? "原答案未写入" : "记录状态异常"],
    ];
  }
  const processMemory = dict(profile.model_process_memory_used_mib);
  if (memory.method === "orin_smaps_rss_plus_gpu_uma_v1") return [
    ["主模型占用峰值（smaps Rss + GPU/UMA）", fmtMemory(dict(profile.model_process_system_memory_used_mib).peak), "主机 smaps RSS + GPU/UMA 分配的归属估计；跨驱动和共享页不保证物理去重", true],
    ["工作负载内存估算", "不可计算", "当前 llama.cpp 未提供可靠的 KV Cache 实际使用量"],
    ["KV Cache 已用峰值", memory.kv_cache_used_peak_tokens == null ? "-" : `${Number(memory.kv_cache_used_peak_tokens).toLocaleString("en-US")} token`, `使用率峰值 ${fmtNumber(memory.kv_cache_usage_peak_pct, "%")}`],
    ["KV 实际字节数", "不可用", "未验证模型每 token KV 字节数；不以 token 数伪造 GiB"],
    ["数据来源", "本机 smaps Rss + nvmap/GPU 页 + tegrastats", profile.memory_accounting === "jetson_pss_plus_nvmap_estimate"
      ? "GPU 优先按 PID 汇总 nvmap；不可读时仅正的 VmRSS − smaps RSS 残差作为注明来源的估计；未知分量使合计不可用"
      : "历史 GPU 归属公式以原始口径说明为准；不按现行公式重算"],
  ];
  if (memory.method === "orin_process_pss_uma_v1") return [
    ["进程物理内存峰值（PSS）", fmtMemory(processMemory.peak), "Orin 统一物理内存平台的进程峰值；不是独立显存，也不是工作负载估算", true],
    ["工作负载内存估算", "不可计算", "当前 llama.cpp 未提供可靠的 KV Cache 实际使用量"],
    ["KV Cache 已用峰值", memory.kv_cache_used_peak_tokens == null ? "-" : `${Number(memory.kv_cache_used_peak_tokens).toLocaleString("en-US")} token`, `使用率峰值 ${fmtNumber(memory.kv_cache_usage_peak_pct, "%")}`],
    ["KV 实际字节数", "不可用", "未验证模型每 token KV 字节数；不以 token 数伪造 GiB"],
    ["数据来源", "118 本机 PSS + tegrastats", "KV 仅在 llama.cpp metrics 确实提供时记录"],
  ];
  return [
    ["估算工作负载显存", memory.comparable_workload_memory_gib == null ? "-" : `${Number(memory.comparable_workload_memory_gib).toFixed(2)} GiB`, "按「固定基础占用 + KV 逻辑使用峰值」推算出的可比显存，用于横向比模型需求；不是实测显存", true],
    ["固定基础占用", memory.fixed_base_memory_gib == null ? "-" : `${Number(memory.fixed_base_memory_gib).toFixed(2)} GiB`, "空载时模型进程显存减去预分配的 KV 容量，即权重与框架的固定开销", true],
    ["KV Cache 容量", memory.kv_cache_capacity_gib == null ? "-" : `${Number(memory.kv_cache_capacity_gib).toFixed(2)} GiB`, memory.kv_cache_capacity_tokens == null ? "预留给 KV 缓存的显存，决定最长上下文与并发槽数 · 未记录 token 容量" : `预留给 KV 缓存的显存，最多可缓存 ${Number(memory.kv_cache_capacity_tokens).toLocaleString("en-US")} token`],
    ["KV Cache 实际峰值", memory.kv_cache_used_peak_gib == null ? "-" : `${Number(memory.kv_cache_used_peak_gib).toFixed(3)} GiB`, `测评中 KV 缓存真正用到的峰值（容量是上限，这个才是实际） · 使用率峰值 ${fmtNumber(memory.kv_cache_usage_peak_pct, "%")}`],
    ["模型权重", memory.weight_gib == null ? "-" : `${Number(memory.weight_gib).toFixed(2)} GiB`, `权重本身占的显存 · 激活峰值 ${memory.peak_activation_gib == null ? "-" : `${memory.peak_activation_gib} GiB`} · CUDA Graph ${memory.cuda_graph_gib == null ? "-" : `${memory.cuda_graph_gib} GiB`}`],
    ["vLLM 进程预留显存", fmtMemory(processMemory.peak), `vLLM 进程向驱动预留的总量（含还没用上的 KV 池），所以偏高，不能当作实际需求 · 空载 ${memory.idle_process_memory_gib == null ? "-" : `${Number(memory.idle_process_memory_gib).toFixed(2)} GiB`}`],
    isBenchmarkGpuProfile
      ? ["评测采样覆盖", `${profile.questions_completed ?? "-"}/${profile.questions_total ?? "-"} 题`, "来自本次正式评测 GPU 采样"]
      : ["复测进度", `${profile.questions_completed ?? 0}/${profile.questions_total ?? 0} 题`, `请求失败 ${profile.failed_requests ?? 0} · 答案不保存`],
    isBenchmarkGpuProfile
      ? ["数据来源", "正式评测采样", "与本次 run 的 QA/GPU 采样同时记录"]
      : ["原测评数据一致性", profile.items_integrity_ok === true ? "通过" : profile.status === "completed" ? "未通过" : "待完成", profile.answers_persisted === false ? "原答案未写入" : "记录状态异常"],
  ];
}

// ── 1.0 :1212-1253 聚合指标行 ────────────────────────────────────────────

/**
 * 1.0 `:1212-1253`。
 *
 * **两处与 1.0 的接口差异（已显式化）**：
 *   - 1.0 的入参 `phase = {}` **在函数体内从未被引用**（1.0 自身的历史遗留，见报告），
 *     真正用的是 `effectiveRunSummary(activeRun.value)` 与 `activeRun.value.qa_concurrency`。
 *     这里改成 `(run, summary)`，`summary` 由调用方传 `effectiveRunSummary(run)` 的结果。
 */
export function aggregateMetricRows(run: unknown, summary: unknown): MetricRow[] {
  const r = dict(run);
  const s = dict(summary);
  const dist = dict(s.judge_distribution);
  const evidenceDist = dict(s.evidence_distribution);
  const throughputSamples = Number(s.agent_throughput_latency_sample_count);
  const throughputTotal = Number(s.agent_throughput_latency_total_count ?? s.total);
  const throughputNote = s.agent_throughput_latency_mode === "measured_agent_phase"
    ? `Agent 阶段实际墙钟 ÷ ${s.agent_phase_completed_count ?? throughputTotal} 题 · 并发 ${r.qa_concurrency ?? "-"} · 不含 Judge，Judge 可与 Agent 并行`
    : Number.isFinite(throughputSamples) && Number.isFinite(throughputTotal)
      ? `历史记录按 Agent/Judge 时间线回退估算 · ${throughputSamples}/${throughputTotal} 题，不能视为实测`
      : "历史记录未保存 Agent 独立阶段墙钟";
  const typedMediaMetrics = s.retrieval_metric_scope === "all_media";
  return [
    [typedMediaMetrics ? "媒体检索 Precision" : "历史图片检索 Precision", fmtPct(s.retrieval_precision_macro), `交付准确率：模型拿出来交付的图里有多少属于 GT · 逐 QA 求值后平均 · ${s.retrieval_metric_count ?? 0} 题有 GT · 排除 ${s.retrieval_excluded_unanswerable_count ?? 0} 道不可回答题`, true],
    [typedMediaMetrics ? "媒体检索 Recall" : "历史图片检索 Recall", fmtPct(s.retrieval_recall_macro), typedMediaMetrics ? "交付覆盖率：该题 GT 里有多少被模型交付出来 · 逐 QA 等权平均；图/视频按类型与稳定标识匹配" : "交付覆盖率：该题 GT 里有多少被模型交付出来 · 逐 QA 等权平均；历史 run 无法补算视频指标", true],
    ["回答质量均分", s.answer_quality_mean == null ? "-" : `${s.answer_quality_mean} / 2`, `Judge 给最终回答打分（2 完全支持 / 1 部分支持 / 0 不支持）的均值，唯一回答「模型能不能用」的端到端结论 · 有效 ${s.judge_valid_count ?? 0}/${s.total ?? 0} · 无效 ${(s.total ?? 0) - (s.judge_valid_count ?? 0)} · 0:${dist["0"] || 0} · 1:${dist["1"] || 0} · 2:${dist["2"] || 0}`, true],
    ["步数内 QA 完成率", fmtPct(s.qa_completion_within_steps_rate), `在限定步数内跑完流程的题占比，衡量 Agent 是否收敛而不是反复绕圈 · 有效记录 ${s.qa_completion_valid_count ?? 0} 题`, true],
    ["工具参数 JSON 解析率", fmtPct(s.tool_argument_json_rate), s.tool_argument_json_total == null ? "未记录或未调用原生工具；普通文本回答不进入分母" : `${s.tool_argument_json_success}/${s.tool_argument_json_total} 次原生工具参数`, true],
    ["旧 JSON 动作解析率", fmtPct(s.json_parse_success_rate), s.json_parse_total == null ? "旧 JSON 动作协议；原生工具模式未适用，旧结果缺失事实时未记录" : `需要结构化输出的调用里一次解析成功的比例，衡量格式稳定性 · ${s.json_parse_success ?? 0}/${s.json_parse_total} 个`, true],
    ["Agent 并发吞吐折算时延", fmtMs(s.agent_throughput_latency_ms), throughputNote, true],
    ["平均调用轮数", s.agent_loop_calls_mean == null ? "未记录" : `${Number(s.agent_loop_calls_mean).toFixed(2)} 轮`, `每道题平均经历几轮模型调用，轮数越高越可能在反复试探 · 仅 Agent/Recovery，不含 L2 Judge、Final Writer 和工具内部模型`, true],
    ["累计输入 token", fmtTokens(s.prompt_tokens_total), "所有主 Agent 调用喂进去的 prompt token 总量，用于估算上下文成本", true],
    ["累计输出 token", fmtTokens(s.completion_tokens_total), "所有主 Agent 调用生成的 token 总量，用于估算生成成本与耗时", true],
    ["平均任务完成时间", fmtMs(s.agent_task_latency_mean_ms), r.qa_concurrency > 1
      ? `每道 QA 各自计时的平均值（输入→最终回答，不含 Judge）；并发 ${r.qa_concurrency} 负载下含排队与批内干扰，勿与串行 run 直接对比`
      : "每道 QA 各自计时的平均值（输入→最终回答，不含 Judge）", true],
    [typedMediaMetrics ? "媒体检索 F1" : "历史图片检索 F1", fmtPct(s.retrieval_f1_macro), "Precision 与 Recall 的调和平均，任一侧失衡都会明显拉低 · 逐 QA 计算后等权平均"],
    ["图片检索 P / R / F1", `${fmtPct(s.image_retrieval_precision_macro)} / ${fmtPct(s.image_retrieval_recall_macro)} / ${fmtPct(s.image_retrieval_f1_macro)}`, typedMediaMetrics ? `只看图片的交付口径 P/R/F1 · ${s.image_retrieval_metric_count ?? 0} 题含图片 GT · 逐 QA 平均` : "只看图片的交付口径 · 历史图片口径 · 逐 QA 平均"],
    ["视频检索 P / R / F1", typedMediaMetrics ? `${fmtPct(s.video_retrieval_precision_macro)} / ${fmtPct(s.video_retrieval_recall_macro)} / ${fmtPct(s.video_retrieval_f1_macro)}` : "未记录", typedMediaMetrics ? `只看视频的交付口径 P/R/F1 · ${s.video_retrieval_metric_count ?? 0} 题含视频 GT · 逐 QA 平均` : "只看视频的交付口径 · 历史 run 无 typed media，禁止推测"],
    ["Judge LLM 平均时延", fmtMs(s.judge_llm_latency_mean_ms), `每题判分那次模型调用的平均耗时，不计入 Agent 时延 · Judge 阶段墙钟 ${fmtMs(s.judge_phase_wall_ms)}`],
    ["任务判断准确率", fmtPct(s.task_decision_accuracy), `Agent 选择「回答 / 拒答 / 追问」的动作与数据集期望一致的比例 · 标注 ${s.task_decision_labeled_count ?? 0} 题 · Judge 有效 ${s.task_decision_valid_count ?? 0} 题`],
    ["证据对应均分", s.evidence_mean == null ? "未记录" : `${s.evidence_mean} / 2`, `Judge 给「回答引用的证据是否真的支持结论」打分（2/1/0）的均值 · 0:${evidenceDist["0"] || 0} · 1:${evidenceDist["1"] || 0} · 2:${evidenceDist["2"] || 0}`],
    ["证据完全支持率", fmtPct(s.evidence_fully_supported_rate), "证据 Judge 给满分（2）的题占比，衡量引用是否扎实"],
    ["端到端测评总时延（不含 Judge）", fmtMs(s.benchmark_e2e_latency_excluding_judge_ms), "身份/关系及图片导入开始至最后一个 Agent 完成；Judge 可并行执行"],
    ["完全准确率", fmtPct(s.exact_accuracy), "Judge 给 2 分（完全支持）的题占比 · 严格口径"],
    ["核心准确率", fmtPct(s.core_accuracy), "Judge 给 1 或 2 分的题占比 · 宽松口径"],
    ["LLM TTFT 均值", fmtMs(s.llm_ttft_ms_mean), "首 token 响应时间均值，衡量并发排队下的响应灵敏度"],
    ["LLM 生成速度", s.llm_tokens_per_second_mean == null ? "-" : `${Number(s.llm_tokens_per_second_mean).toFixed(1)} token/s`, "主 Agent 平均生成速度；流式调用是纯解码速率，非流式是端到端速率，两者不可直接横比"],
  ];
}

// ── 1.0 :1254-1304 关键指标卡 ────────────────────────────────────────────

/**
 * 1.0 `:1254-1304`。`summary` 显式传入（见 `aggregateMetricRows` 的说明）；
 * `run` 只用于 `qa_concurrency` 与 `gpuMetricsView`。
 */
export function keyMetricRows(run: unknown, summary: unknown): KeyMetric[] {
  const r = dict(run);
  const s = dict(summary);
  const dist = dict(s.judge_distribution);
  const gpu = dict(gpuMetricsView(run));
  const ranking = dict(s.image_ranking);
  const pct = (value: unknown) => value == null ? "未计算" : fmtPct(value);
  const platform = telemetryPlatform(gpu);
  const isUnified = platform === "apple_unified";
  const isOrin = ["orin_ssh_pss", "jetson_local_pss"].includes(platform);
  const processMemory = isUnified || isOrin
    ? dict(gpu.model_process_system_memory_used_mib).peak
    : dict(gpu.model_process_memory_used_mib).peak;
  const processMemoryNote = isUnified
    ? "主模型占用采样峰值（footprint+权重映射）· 容量估计，映射不保证驻留，非 GPU 专属显存"
    : isOrin
      ? jetsonUnifiedFromRun(run)
        ? gpu.memory_accounting === "jetson_pss_plus_nvmap_estimate"
          ? "主模型占用采样峰值 · smaps RSS + GPU/UMA 分配估计；优先 nvmap，不可读时仅正残差作为注明来源的估计；未知分量使合计不可用，跨驱动和共享页不保证物理去重"
          : "主模型占用采样峰值 · smaps RSS + GPU/UMA；历史公式见原始记录，不推断物理去重"
        : "主模型 VmRSS 峰值 · Orin 无独立显存，这是进程驻留"
      : "主模型显存峰值 · NVML 按模型 PID 汇总";
  const throughputNote = s.agent_throughput_latency_mode === "measured_agent_phase"
    ? `Agent 阶段实际墙钟 ÷ ${s.agent_phase_completed_count ?? s.total ?? 0} 题 · 并发 ${r.qa_concurrency ?? "-"} · 不含 Judge（Judge 与 Agent 并行）`
    : "历史记录按时间线回退估算，不是实测值";
  const operational = dict(s.operational);
  return [
    {
      label: "不可评估",
      value: operational.not_evaluable_count == null ? "未记录" : `${operational.not_evaluable_count} 题`,
      note: operationalCountNote(operational.by_kind),
    },
    {
      label: "评分未完成",
      value: operational.judge_incomplete_count == null ? "未记录" : `${operational.judge_incomplete_count} 题`,
      note: operationalCountNote(operational.judge_by_kind),
    },
    {
      label: "回答质量均分",
      value: s.answer_quality_mean == null ? "未计算" : `${s.answer_quality_mean} / 2`,
      note: `Judge 有效 ${s.judge_valid_count ?? 0}/${s.total ?? 0} 题 · 0 分 ${dist["0"] || 0} · 1 分 ${dist["1"] || 0} · 2 分 ${dist["2"] || 0}`,
    },
    {
      label: "Agent 并发吞吐折算时延",
      value: fmtMs(s.agent_throughput_latency_ms),
      note: throughputNote,
    },
    {
      label: "工具图片召回率",
      value: ranking.r_at_5 == null ? "未计算" : `R@5 ${pct(ranking.r_at_5)}`,
      note: ranking.question_count == null
        ? "该 run 未落盘候选顺序，无法补算；不补零、不用别的口径顶替"
        : `工具召回的前 5 张里覆盖了该题 GT 的比例 · 统计 ${ranking.r_at_5_question_count ?? 0} 题（正样本 ≤ 5 张）· 明细见下方「召回指标」`,
    },
    {
      label: "模型申明图片准确率",
      value: pct(s.image_retrieval_precision_macro),
      note: `模型显式交付的图片里属于 GT 的比例 · ${s.image_retrieval_metric_count ?? 0} 题含图片 GT · 这是交付口径，与检索口径的 R@K 不可换算`,
    },
    {
      label: isUnified || isOrin ? "主模型内存占用" : "模型进程显存",
      value: fmtMemory(processMemory),
      note: processMemoryNote,
    },
  ];
}

// ── 1.0 :1305-1320 召回指标 ──────────────────────────────────────────────

/** 1.0 `:1305-1320`。`question_count == null` 时返回**空数组**（不是占位行）。 */
export function recallMetricRows(summary: unknown): MetricRow[] {
  const ranking = dict(dict(summary).image_ranking);
  if (ranking.question_count == null) return [];
  const pct = (value: unknown) => value == null ? "未计算" : fmtPct(value);
  return [
    ["R@1", pct(ranking.r_at_1), `${ranking.r_at_1_question_count ?? 0} 题（GT 恰 1 张）· 单正样本口径，此时 R@1 与 Hit@1 必然相等`],
    ["R@5", pct(ranking.r_at_5), `${ranking.r_at_5_question_count ?? 0} 题（GT ≤ 5 张）· 关键指标卡里的主值`],
    ["R@10", pct(ranking.r_at_10), `${ranking.r_at_10_question_count ?? 0} 题（GT ≤ 10 张）`],
    ["Hit@1 / 5 / 10", `${pct(ranking.hit_at_1)} / ${pct(ranking.hit_at_5)} / ${pct(ranking.hit_at_10)}`, "与各自 R@K 同一批题，只问「有没有命中」；多正样本题下与 R@K 不是一回事，两个都要看"],
    ["mAP", pct(ranking.average_precision), `全部 ${ranking.question_count} 题 · 正样本张数不设限，多正样本场景的主指标`],
    ["MRR", pct(ranking.mrr), `全部 ${ranking.question_count} 题 · 第一个正确结果位次的倒数，无命中记 0`],
    ["P@5", pct(ranking.p_at_5), `仅正样本 ≤ 5 的 ${ranking.p_at_5_question_count ?? 0} 题 · 前 5 张里 GT 的占比`],
    ["候选覆盖不足", `${ranking.coverage_short_count ?? 0} 题`, `R@5 口径内工具返回的候选少于该题 GT 张数，其中 ${ranking.no_candidate_count ?? 0} 题一张都没返回。这些题仍计入 R@5，不剔除——工具返回空就是检索失败，不是指标的锅`],
    ["计分题数", `${ranking.question_count} 题`, `多正样本 ${ranking.multi_positive_count ?? 0} 题 · 候选不足 10 张的 ${ranking.short_candidate_count ?? 0} 题 · 已排除不可回答题`],
  ];
}

// ── 1.0 :1321-1334 token 分布 ────────────────────────────────────────────

/** 1.0 `:1321-1330`。`summary` 显式传入（1.0 读 `activeRun.value`）。 */
export function tokenDistributionRows(summary: unknown): MetricRow[] {
  const s = dict(summary);
  return [
    ["最大输入 token", fmtTokens(s.llm_prompt_tokens_max), "单次调用喂进去的最大 prompt 长度，用于判断上下文是否快要撑爆"],
    ["P95 输入 token", fmtTokens(s.llm_prompt_tokens_p95), "95% 的调用输入不超过此值，比最大值更能代表常态"],
    ["最大输出 token", fmtTokens(s.llm_completion_tokens_max), "单次调用生成的最大长度，用于核对 max_tokens / max_new_tokens 是否设够"],
    ["P95 输出 token", fmtTokens(s.llm_completion_tokens_p95), "95% 的调用输出不超过此值"],
    ["最大总上下文", fmtTokens(s.llm_context_tokens_max), "单次调用的输入 + 输出 token 峰值，需小于 max_model_len"],
    ["P95 总上下文", fmtTokens(s.llm_context_tokens_p95), "按此值设置 max_model_len 可覆盖绝大多数调用"],
  ];
}

/** 1.0 `:1332-1334`。缺字段回 `0`（不是 `-`）。 */
export function tokenDistributionCount(summary: unknown): number {
  return dict(summary).llm_context_samples_count ?? 0;
}

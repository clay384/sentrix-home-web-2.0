import { describe, expect, it } from "vitest";

import {
  deliveryBreakdown,
  guardSummary,
  judgeProgressLabel,
  openImageTarget,
  phaseErrorDetails,
  phaseErrorSummary,
  phaseSummary,
  pipelineMetricRows,
  retrievalChannelRows,
  shortHash,
  snapshotSummary,
  toolPerformanceRows,
} from "./details.ts";
// `itemTimingBreakdown` 的权威实现已从 details.ts 移入 trace.ts（消除模块环），用例不变。
import { itemTimingBreakdown } from "./trace.ts";

describe("toolPerformanceRows（1.0 :2065-2068）", () => {
  it("把 summary.tool_performance 摊成带 name 的行", () => {
    const rows = toolPerformanceRows({
      tool_performance: { search: { calls: 3, ok_rate: 0.5 }, ocr: { calls: 2 } },
    });
    expect(rows).toEqual([
      { name: "search", calls: 3, ok_rate: 0.5 },
      { name: "ocr", calls: 2 },
    ]);
  });
  it("缺 tool_performance → 空数组", () => {
    expect(toolPerformanceRows({})).toEqual([]);
    expect(toolPerformanceRows(null)).toEqual([]);
  });
});

describe("deliveryBreakdown（1.0 :2069-2078）", () => {
  it("无 delivery_breakdown，或两类计数**都**为零 → null", () => {
    expect(deliveryBreakdown({})).toBeNull();
    expect(deliveryBreakdown({ delivery_breakdown: { deterministic_delivery_count: 0, ocr_partial_count: 0 } })).toBeNull();
  });

  it("只要 ocr_partial_count 非零（det 为 0）→ 非 null（存在性判据是「任一非零」）", () => {
    const bd = deliveryBreakdown({
      delivery_breakdown: { deterministic_delivery_count: 0, ocr_partial_count: 2, ocr_partial_reasons: { ocr_timeout: 2 } },
    });
    expect(bd).toEqual({
      detCount: 0,
      detKinds: [],
      ocrPartial: 2,
      ocrReasons: [["ocr_timeout", 2]],
    });
  });

  it("只要 deterministic_delivery_count 非零（ocr 为 0）→ 非 null", () => {
    const bd = deliveryBreakdown({
      delivery_breakdown: { deterministic_delivery_count: 3, deterministic_delivery_kinds: { structured: 2, template: 1 } },
    });
    expect(bd).toEqual({
      detCount: 3,
      detKinds: [
        ["structured", 2],
        ["template", 1],
      ],
      ocrPartial: 0,
      ocrReasons: [],
    });
  });
});

describe("shortHash / snapshotSummary（1.0 :2079-2091）", () => {
  it("shortHash 取前 12 字符，假值回 -", () => {
    expect(shortHash("abcdef1234567890")).toBe("abcdef123456");
    expect(shortHash(undefined)).toBe("-");
    expect(shortHash("")).toBe("-");
  });

  it("snapshotSummary 取值与兜底", () => {
    const s = snapshotSummary({
      captured_at: "2026-10-06T10:00:00Z",
      gpu: [{ name: "RTX 4090", memory_used_mib: 12288 }],
      process_memory: { process_memory_used_mib: 2048 },
      manager: { served_model_name: "qwen" },
    });
    expect(s.time).not.toBe("未记录");
    expect(s.model).toBe("qwen");
    expect(s.gpu).toBe("RTX 4090");
    expect(s.memory).toBe("12.00 GiB");
    expect(s.processMemory).toBe("2.00 GiB");
  });

  it("snapshotSummary 缺字段回退（model 再回退 profile）", () => {
    expect(snapshotSummary({})).toEqual({
      time: "未记录",
      model: "-",
      gpu: "-",
      memory: "-",
      processMemory: "-",
    });
    expect(snapshotSummary({ gpu: [], manager: { profile: "p" } }).model).toBe("p");
  });
});

describe("guardSummary（1.0 :2092-2106）", () => {
  it("有守卫拦截：codes 用、连接，rendered/ocr 各给各的", () => {
    const item = {
      agent_status: "blocked",
      guard_debug: {
        status: "blocked",
        termination_reason: "guard_blocked",
        l1_codes: ["L1_no_evidence", "L1_low_conf"],
        recovery_attempts: 2,
        deterministic_delivery: { rendered: true, kind: "structured" },
      },
      delivery_status: { ocr_partial: true, ocr_partial_reason: "ocr_timeout" },
    };
    expect(guardSummary(item)).toEqual({
      recorded: true,
      status: "blocked",
      termination: "guard_blocked",
      recoveries: 2,
      codes: "L1_no_evidence、L1_low_conf",
      deterministic: "structured",
      ocrPartial: "ocr_timeout",
    });
  });

  it("无拦截（空 item）：各字段各自兜底", () => {
    expect(guardSummary({})).toEqual({
      recorded: false,
      status: "-",
      termination: "-",
      recoveries: "-",
      codes: "无",
      deterministic: "",
      ocrPartial: "",
    });
  });

  it("rendered 为真但缺 kind → 未知；rendered 为假 → 空串（与 ocrPartial 的 unknown 不同）", () => {
    expect(guardSummary({ guard_debug: { deterministic_delivery: { rendered: true } } }).deterministic).toBe("未知");
    expect(guardSummary({ guard_debug: { deterministic_delivery: { rendered: false } } }).deterministic).toBe("");
    expect(guardSummary({ delivery_status: { ocr_partial: true } }).ocrPartial).toBe("unknown");
  });

  it("termination 走 normalizedTerminationReason：agent_reason 命中 token 预检 502 特判", () => {
    expect(guardSummary({ agent_reason: "502 Bad Gateway" }).termination).toBe(
      "上下文 token 预检失败（tokenize 接口 502）",
    );
    // 未命中特判才回 guard_debug / termination_reason
    expect(guardSummary({ termination_reason: "complete" }).termination).toBe("complete");
  });
});

describe("itemTimingBreakdown（1.0 :2122-2133）", () => {
  it("saved 齐全时全取 saved", () => {
    expect(
      itemTimingBreakdown({
        timing_breakdown: { wall_clock_ms: 100, agent_wall_ms: 80, model_ms: 50, tool_ms: 20, judge_ms: 10, other_ms: 5 },
      }),
    ).toEqual({ wall_clock_ms: 100, agent_wall_ms: 80, model_ms: 50, tool_ms: 20, judge_ms: 10, other_ms: 5 });
  });

  it("缺 saved 时按字段各自回退（model_ms 取 llm_summary，judge/wall 取 item 平铺）", () => {
    // `agent_wall_ms` 与 `other_ms` 现在由 `wall_clock_ms − judge_ms` 推出：
    // 111 − 33 = 78；`other` 再把 model/tool 减掉，负值钳到 0（模型调用可能是并发的，
    // 串行时长之和会大于墙钟）。此前这两个字段恒为 null，页面上的耗时位因此是空的。
    expect(itemTimingBreakdown({ wall_clock_ms: 111, llm_summary: { total_ms_sum: 222 }, judge_ms: 33 })).toEqual({
      wall_clock_ms: 111,
      agent_wall_ms: 78,
      model_ms: 222,
      tool_ms: null,
      judge_ms: 33,
      other_ms: 0,
    });
  });

  it("0 是有效值（?? 不是 ||）", () => {
    expect(itemTimingBreakdown({ timing_breakdown: { wall_clock_ms: 0, model_ms: 7 } })).toMatchObject({
      wall_clock_ms: 0,
      model_ms: 7,
    });
  });
});

describe("retrievalChannelRows（1.0 :2134-2152）", () => {
  it("按工具轮次展开 channels", () => {
    const item = {
      tool_trace: [
        {
          tool: "search",
          retrieval_timing: {
            channels: {
              text: { latency_ms: 5, embedding_ms: 1, candidate_count: 10, status: "ok" },
              image: { latency_ms: 9 },
            },
          },
        },
        { tool: "ocr" },
      ],
    };
    expect(retrievalChannelRows(item)).toEqual([
      {
        key: "0-text",
        tool_round: 1,
        tool_name: "search",
        channel: "text",
        latency_ms: 5,
        embedding_ms: 1,
        candidate_count: 10,
        status: "ok",
      },
      {
        key: "0-image",
        tool_round: 1,
        tool_name: "search",
        channel: "image",
        latency_ms: 9,
        embedding_ms: undefined,
        candidate_count: undefined,
        status: undefined,
      },
    ]);
  });

  it("无 tool_trace / 无 channels → 空", () => {
    expect(retrievalChannelRows({})).toEqual([]);
    expect(retrievalChannelRows({ tool_trace: [{ tool: "x" }] })).toEqual([]);
  });
});

describe("phaseSummary：部分阶段缺失时各走各的回退（1.0 :2153-2181）", () => {
  it("model_deploy：有 unload_seconds 走卸载分支", () => {
    expect(phaseSummary("model_deploy", { unload_seconds: 2, load_seconds: 3, health_check_seconds: 1 })).toBe(
      "卸载 2s · 加载 3s · 健康检查 1s",
    );
  });

  it("model_deploy：**缺 unload_seconds** → 不回退到「-」，而是落到通用「耗时」", () => {
    expect(phaseSummary("model_deploy", { total_seconds: 5 })).toBe("耗时 5s");
  });

  it("scope_setup / identity_seed / photo_import：偏好键取不到就 -，各自字段兜底", () => {
    expect(phaseSummary("scope_setup", {})).toBe("创建 -");
    expect(
      phaseSummary("identity_seed", {
        seeded_count: 3,
        family_relationship_import: { requested: 5, imported: 4 },
        upload_seconds: 2,
      }),
    ).toBe("预置 3 人 · 关系 4/5 · 2s");
    expect(phaseSummary("identity_seed", {})).toBe("预置 0 人 · -");
    expect(phaseSummary("photo_import", { accepted_count: 8, total_media: 10, failed_count: 2, upload_seconds: 1.5 })).toBe(
      "导入 8/10 个媒体 · 失败 2 · 阶段墙钟 1.5s",
    );
    // Counts are unknown until the synchronous import response returns; never display fake zeroes.
    expect(phaseSummary("photo_import", {})).toBe("导入 ?/? 个媒体 · 失败 ? · 阶段墙钟 -");
    // Real Mac full-run values: backend subtotal must not replace the request wall clock.
    expect(phaseSummary("photo_import", { total_seconds: 1208, timings: { total: 738.1 } })).toBe(
      "导入 ?/? 个媒体 · 失败 ? · 阶段墙钟 1208s · 后端计时 738.1s",
    );
  });

  it("pipeline_processing：有 progress 才展开，否则落到通用「耗时」", () => {
    expect(
      phaseSummary("pipeline_processing", {
        progress: { processed: 3, total: 10, failed: 1, skipped: 2 },
        total_seconds: 60,
      }),
    ).toBe("3/10 资产完成 · 1 失败 · 2 跳过 · 阶段墙钟 60s");
    expect(phaseSummary("pipeline_processing", { total_seconds: 4 })).toBe("耗时 4s");
  });

  it("显式流水线摘要显示关键帧、帧记忆与图构建耗时", () => {
    expect(
      phaseSummary("pipeline_processing", {
        progress: { processed: 2, total: 2, failed: 0, skipped: 0 },
        total_seconds: 20,
        timings: { video_keyframe_extraction: 4, video_keyframe_memory: 12, memory_graph: 0.4 },
      }),
    ).toBe("2/2 资产完成 · 0 失败 · 0 跳过 · 阶段墙钟 20s · 关键帧 4s · 帧记忆 12s · 图构建 0.4s");
  });

  it("qa_eval：有 progress 展开 Agent/Judge，无 progress 用 itemCount 兜底", () => {
    expect(
      phaseSummary(
        "qa_eval",
        {
          progress: {
            completed: 3,
            agent_completed: 3,
            agent_total: 10,
            judge_completed: 2,
            judge_total: 10,
            qa_concurrency: 4,
            judge_concurrency: 2,
          },
          total_seconds: 120,
        },
        99,
      ),
    ).toBe("Agent 3/10 · Judge 已结束 2/10 · 并发 Agent 4 / Judge 2 · 120s");
    expect(phaseSummary("qa_eval", { progress: { in_flight: 1, judge_total: 10 } }, 0)).toBe(
      "Agent 0/? · Judge 已结束 ?/10 · -",
    );
    expect(
      phaseSummary(
        "qa_eval",
        { progress: { completed: 74, agent_completed: 74, agent_total: 74 }, judge_completed: 71, judge_total: 74 },
        0,
      ),
    ).toBe("Agent 74/74 · Judge 已结束 71/74 · -");
    expect(phaseSummary("qa_eval", { total_seconds: 9 }, 7)).toBe("7 题 · 9s");
  });

  it("未知阶段无墙钟 → 空串；phase 为 null → 空串", () => {
    expect(phaseSummary("unknown_key", {})).toBe("");
    expect(phaseSummary("model_deploy", null)).toBe("");
  });

  it("qa_eval：兼容历史外层 Judge 计数，未记录不能显示为零", () => {
    expect(phaseSummary("qa_eval", {
      judge_completed: 70, judge_total: 487,
      progress: { completed: 71, total: 487 },
    })).toContain("Judge 已结束 70/487");
    expect(phaseSummary("qa_eval", { progress: { completed: 71, total: 487 } })).toContain("Judge 已结束 ?/487");
    expect(phaseSummary("qa_eval", {
      judge_completed: 70, judge_total: 487,
      progress: { completed: 71, total: 487, judge_completed: 0, judge_total: 487 },
    })).toContain("Judge 已结束 0/487");
    expect(judgeProgressLabel({ progress: { judge_completed: null, judge_total: 487 } })).toBe("Judge 已结束 ?/487");
    expect(judgeProgressLabel({ progress: { judge_concurrency: 0 } })).toBe("Judge 未配置");
    expect(judgeProgressLabel({ progress: { judge_completed: 69, judge_total: 487, judge_skipped: 1 } })).toBe("Judge 已结束 69/487 · 跳过 1");
  });
});

describe("pipelineMetricRows：缺失阶段走不同回退（1.0 :2199-2232）", () => {
  it("部分缺失：无有效 image_count → 墙钟均值回 average_seconds_per_photo；缺失计时段整行消失", () => {
    const phase = {
      average_seconds_per_photo: 1.5,
      pipeline_metrics: {
        image_count: 0,
        stage_timings: { image_clip_seconds: { mean_seconds: 0.01, sum_seconds: 0.03, p95_seconds: 0.02 } },
      },
    };
    const rows = pipelineMetricRows(phase);
    expect(rows).toHaveLength(4);
    expect(rows[0]).toEqual(["评测图片墙钟均值", "1.5s", "历史运行未记录评测图片数，按旧口径展示"]);
    expect(rows[1]).toEqual(["实际图片并发", "-", "配置 - · vLLM 上限 -"]);
    expect(rows[2]).toEqual(["事件总结", "-", "批次耗时 -"]);
    expect(rows[3]).toEqual(["图片 CLIP", "10ms", "总计 30ms · P95 20ms"]);
    expect(rows.map((r) => r[0])).not.toContain("人脸检测");
  });

  it("齐全：有 image_count 与墙钟 → 墙钟/张数；并发与事件总结走真值", () => {
    const rows = pipelineMetricRows({
      total_seconds: 1000,
      pipeline_metrics: {
        image_count: 100,
        effective_workers: 8,
        configured_workers: 8,
        vllm_max_num_seqs: 16,
        event_count: 20,
        event_summary_call_count: 18,
        event_summary_wall_seconds: 5,
      },
    });
    expect(rows).toHaveLength(3);
    expect(rows[0]).toEqual([
      "评测图片墙钟均值",
      "10s",
      "阶段墙钟 1000s ÷ 100 张评测图片；不含身份参考图",
    ]);
    expect(rows[1]).toEqual(["实际图片并发", "8 路", "配置 8 · vLLM 上限 16"]);
    expect(rows[2]).toEqual(["事件总结", "18/20 次", "批次耗时 5s"]);
  });

  it("墙钟均值两者都缺 → 未记录", () => {
    expect(pipelineMetricRows({ pipeline_metrics: {} })[0][1]).toBe("未记录");
  });

  it("显式流水线把不同阶段耗时列成独立指标", () => {
    const rows = pipelineMetricRows({
      timings: { source_memory_prepare: 10, video_keyframe_extraction: 4, memory_graph: 0.4, causal_graph: 0.2 },
      pipeline_metrics: {},
    });
    expect(rows.slice(0, 4)).toEqual([
      ["源媒体模型准备", "10s", "并发 worker 累计耗时"],
      ["关键帧提取", "4s", "ffmpeg 抽帧墙钟累计"],
      ["记忆图构建", "0.4s", "结构化图节点与边重建"],
      ["因果边判定", "0.2s", "规则信号筛选与文本模型因果判定；包含在记忆图构建耗时内"],
    ]);
  });
});

describe("phaseErrorDetails / phaseErrorSummary（1.0 :2182-2193）", () => {
  it("优先 error_details；空则用单条 error 合成（status 回退 failed）", () => {
    expect(phaseErrorDetails({ error_details: [{ sample_id: "a" }, { sample_id: "b" }] })).toHaveLength(2);
    expect(phaseErrorDetails({ error: "boom", status: "failed" })).toEqual([
      { sample_id: "阶段错误", status: "failed", reason: "boom" },
    ]);
    expect(phaseErrorDetails({ error: "boom" })).toEqual([
      { sample_id: "阶段错误", status: "failed", reason: "boom" },
    ]);
    expect(phaseErrorDetails({})).toEqual([]);
    expect(phaseErrorDetails(null)).toEqual([]);
  });

  it("summary 带上跳过数（skipped_asset_count 优先，再 progress.skipped）", () => {
    expect(phaseErrorSummary({ error_details: [{}, {}, {}], skipped_asset_count: 2 })).toBe("3 条错误记录，2 个样本已跳过");
    expect(phaseErrorSummary({ error_details: [{}], progress: { skipped: 5 } })).toBe("1 条错误记录，5 个样本已跳过");
    expect(phaseErrorSummary({ error_details: [{}] })).toBe("1 条错误记录");
    expect(phaseErrorSummary({})).toBe("");
  });
});

describe("openImageTarget（1.0 :2194 纯半）", () => {
  it("有 asset_id 走 /assets/{id}/file", () => {
    expect(openImageTarget({ asset_id: "abc/def", file_name: "x.jpg" })).toEqual({
      url: "/api/v1/assets/abc%2Fdef/file",
      name: "x.jpg",
    });
  });
  it("无 asset_id 时用 media_url；名字回退 image_id 再回 图片", () => {
    expect(openImageTarget({ media_url: "/api/v1/albums/a/photos/x.jpg" })).toEqual({
      url: "/api/v1/albums/a/photos/x.jpg",
      name: "图片",
    });
    expect(openImageTarget({ media_url: "/m.jpg", image_id: "img-1" })?.name).toBe("img-1");
  });
  it("无任何 URL → null", () => {
    expect(openImageTarget({})).toBeNull();
    expect(openImageTarget({ file_name: "y.jpg" })).toBeNull();
  });
});

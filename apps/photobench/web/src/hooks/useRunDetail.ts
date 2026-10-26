/**
 * 运行详情的数据 hook：迁移 1.0 `App.vue`（只读权威）的 detail 侧状态与编排。
 *
 * 覆盖：`loadKeyframeAnalysis`(:2290) / `loadMemoryEffectiveness`(:2302) /
 * `loadActiveRun`(:2310) / `loadReviews`(:2328，1.0 内联在 `loadActiveRun` 的
 * `resetPage` 分支里) / `reviewFor`(:2333) / `saveReviews`(:2338) /
 * `exportTraces`(:2567) / `exportSftTraces`(最老 backup `codex-macro` 的 `:1469`，
 * 当前 1.0 已删、S10b 加回) / `loadJudgePrompts`(:2467) / `startRejudge`(:2583)
 * 及 `activeRejudge`(:354) / `rejudgePercent`(:365) / `canRejudge`(:369)。
 *
 * **跨 hook 状态说明**（不新建共享 helper，靠 options 注入）：
 *   - `activeRunId` 归 `useRuns`；本 hook 只读，且**读的是 `activeRunIdRef` 而不是
 *     state**——1.0 里 `activeRunId.value` 是同步可读的（`:2314` `if (activeRunId.value
 *     !== runId) return;`），React 的 state 读不到本轮刚 set 的值，故沿用 ref 保语义。
 *   - `updateRunSummary`（:2317 把 `effectiveRunSummary` 合回 runs 列表）、`loadQaPage`
 *     / `resetQaState`（:2324-2325 清空逐题状态、:2331 并发拉当前页，归逐题 hook）、
 *     `setError`（:277 全局 error，归 `useRuns`）都不是本 hook 能独占的，走 options。
 *   - `onRunsReloaded` / `onRejudgeStarted`：1.0 `startRejudge` 成功分支是
 *     `await loadRuns(); await loadActiveRun(); startPolling();`（:2598），后两者归
 *     `useRuns`；顺序拆成「先 onRunsReloaded → 本 hook 的 loadActiveRun →
 *     再 onRejudgeStarted」以保持 1.0 的先后。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api, post } from "../api/client.ts";
import { effectiveRunSummary } from "../lib/runSummary.ts";
import type {
  KeyframeAnalysis,
  MemoryEffectiveness,
  GraphQuality,
  RejudgeTask,
  ReviewsResponse,
  Run,
  RunSummary,
} from "../api/types.ts";

/** 1.0 `activeRunId` ref 的同步视图；由 `useRuns` 持有并传入。 */
export interface ActiveRunIdRef {
  current: string | null;
}

export interface ReviewDraft {
  verdict?: string;
  note?: string;
}

export type ReviewDrafts = Record<string, ReviewDraft>;

/** `/api/v1/judge-prompts` 返回的 `kinds[]` 一条（1.0 `promptKindMeta` `:2466`）。 */
export interface PromptKindMeta {
  kind?: string;
  custom?: string;
  default?: string;
  [key: string]: unknown;
}

/** `startRejudge` POST 体里的 judge 连接参数（1.0 `judgeUrl/judgeModel/judgeProviderId/judgeApiKey*`）。 */
export interface RejudgeJudgeConfig {
  judge_url?: string;
  judge_model?: string;
  judge_provider_id?: string;
  /** 只有用户改过 key 才给（1.0 `judgeApiKeyDirty` 的条件展开 `:2593`）。 */
  judge_api_key?: string;
}

export interface RunDetailOptions {
  /** 当前的 run id（state 值；与 ref 同源）。 */
  activeRunId: string | null;
  /** 同步可读的 run id；所有 await 后的陈旧判断都用它（1.0 `:2314`）。 */
  activeRunIdRef: ActiveRunIdRef;
  /** 1.0 `:2317-2319`：把现算 summary 合回 runs 列表行。 */
  updateRunSummary?: (runId: string, summary: RunSummary) => void;
  /** 1.0 `:2331` 并发拉逐题当前页；`page` 缺省表示「当前页」（1.0 `qaPage.value.page`）。 */
  loadQaPage?: (page?: number) => Promise<unknown>;
  /** 1.0 `:2324-2326`：清空 qaPage / qaDetails / openQaItems，归逐题 hook。 */
  resetQaState?: () => void;
  /** 1.0 `:277` 的共享 error 写入口。 */
  setError?: (message: string) => void;
  /** 1.0 `window.alert` 的注入点，缺省就是 `window.alert`。 */
  notify?: (message: string) => void;
  /** 1.0 `:2598` 的 `loadRuns()`。 */
  onRunsReloaded?: () => Promise<unknown> | unknown;
  /** 1.0 `:2598` 的 `startPolling()`（必须在 loadActiveRun 之后）。 */
  onRejudgeStarted?: () => void;
  /** 配置里的 `judge_prompt`：`/judge-prompts` 没给 `default` 时的兜底（1.0 `:2770`）。 */
  judgePromptFallback?: string;
}

export type RunDetailApi = ReturnType<typeof useRunDetail>;

const EMPTY_REVIEW: ReviewDraft = { verdict: "", note: "" };

function defaultNotify(message: string): void {
  window.alert(message);
}

export function useRunDetail(options: RunDetailOptions) {
  const {
    activeRunId,
    activeRunIdRef,
    updateRunSummary,
    loadQaPage,
    resetQaState,
    setError,
    notify,
    onRunsReloaded,
    onRejudgeStarted,
    judgePromptFallback = "",
  } = options;

  const [activeRun, setActiveRun] = useState<Run | null>(null);
  const [keyframeAnalysis, setKeyframeAnalysis] = useState<KeyframeAnalysis | null>(null);
  const [keyframeAnalysisLoading, setKeyframeAnalysisLoading] = useState(false);
  const [keyframeAnalysisError, setKeyframeAnalysisError] = useState("");
  const [memoryEffectiveness, setMemoryEffectiveness] = useState<MemoryEffectiveness | null>(null);
  const [graphQuality, setGraphQuality] = useState<GraphQuality | null>(null);
  const [reviewDrafts, setReviewDrafts] = useState<ReviewDrafts>({});
  const [reviewSaving, setReviewSaving] = useState(false);
  // 1.0 `:2487` `exportScores = ref(["0","1","2"])`。
  const [exportScores, setExportScores] = useState<string[]>(["0", "1", "2"]);
  const [judgePromptKinds, setJudgePromptKinds] = useState<PromptKindMeta[]>([]);
  const [activePromptKind, setActivePromptKind] = useState("answer_quality");
  // 1.0 `:253` `promptDrafts = reactive({ answer_quality:"", task_decision:"", evidence:"" })`。
  const [promptDrafts, setPromptDrafts] = useState<Record<string, string>>({
    answer_quality: "",
    task_decision: "",
    evidence: "",
  });
  const [rejudgePrompt, setRejudgePrompt] = useState("");
  const [rejudgeSubmitting, setRejudgeSubmitting] = useState(false);
  /** 1.0 `startRejudge` 的 `window.confirm` 抽成「待确认动作」，让 UI 自己弹。 */
  const [rejudgeConfirmation, setRejudgeConfirmation] = useState<{ message: string } | null>(null);

  // 1.0 用 ref 值做同步防重入（`reviewSaving.value` / `rejudgeSubmitting.value` 在
  // 同一 tick 内阻塞二次调用），state 只负责渲染。
  const detailRequest = useRef<{ controller: AbortController; runId: string; full: boolean } | null>(null);
  const analysisOpen = useRef(false);
  useEffect(() => () => detailRequest.current?.controller.abort(), []);
  const reviewSavingRef = useRef(false);
  const rejudgeSubmittingRef = useRef(false);

  const emit = notify ?? defaultNotify;

  // standalone 使用时（不经过 useRuns 的同步 ref）保证 ref 跟着 state 走。
  useEffect(() => {
    activeRunIdRef.current = activeRunId;
  }, [activeRunId, activeRunIdRef]);

  const loadKeyframeAnalysis = useCallback(
    async (runId: string) => {
      setKeyframeAnalysisLoading(true);
      setKeyframeAnalysisError("");
      try {
        const payload = await api<KeyframeAnalysis>(
          `/runs/${encodeURIComponent(runId)}/keyframe-analysis`,
        );
        if (activeRunIdRef.current === runId) setKeyframeAnalysis(payload);
      } catch (error) {
        if (activeRunIdRef.current === runId) {
          setKeyframeAnalysisError((error as Error).message || "关键帧分析失败");
        }
      } finally {
        // 1.0 `:2299` 无条件复位（runId 已变也复位）。
        setKeyframeAnalysisLoading(false);
      }
    },
    [activeRunIdRef],
  );

  const loadMemoryEffectiveness = useCallback(
    async (runId: string) => {
      try {
        const payload = await api<MemoryEffectiveness>(
          `/runs/${encodeURIComponent(runId)}/memory-effectiveness`,
        );
        if (activeRunIdRef.current === runId) setMemoryEffectiveness(payload);
      } catch (error) {
        if (activeRunIdRef.current === runId) {
          setMemoryEffectiveness({
            status: "unavailable",
            error: (error as Error).message || "记忆有效性分析失败",
          });
        }
      }
    },
    [activeRunIdRef],
  );

  const loadGraphQuality = useCallback(
    async (runId: string) => {
      try {
        const payload = await api<GraphQuality>(
          `/runs/${encodeURIComponent(runId)}/graph-quality`,
          { timeoutMs: 60000 },
        );
        if (activeRunIdRef.current === runId) setGraphQuality(payload);
      } catch (error) {
        if (activeRunIdRef.current === runId) {
          setGraphQuality({ status: "unavailable", reason: (error as Error).message || "图质量分析失败" });
        }
      }
    },
    [activeRunIdRef],
  );

  /** 1.0 `:2328`（内联在 `loadActiveRun` 的 resetPage 分支）。 */
  const loadReviews = useCallback(
    async (runId?: string) => {
      const target = runId || activeRunIdRef.current;
      if (!target) return;
      const payload = await api<ReviewsResponse>(`/runs/${encodeURIComponent(target)}/reviews`);
      if (activeRunIdRef.current !== target) return;
      setReviewDrafts({ ...(payload.reviews || {}) });
    },
    [activeRunIdRef],
  );

  /** Ordinary polling reads only a checkpoint; heavy analyses follow panel expansion. */
  const loadActiveRun = useCallback(
    async ({ resetPage = false, includeHistory = false }: { resetPage?: boolean; includeHistory?: boolean } = {}) => {
      const runId = activeRunIdRef.current;
      if (!runId) return;
      const pending = detailRequest.current;
      if (!includeHistory && !resetPage && pending?.runId === runId && pending.full && !pending.controller.signal.aborted) return;
      pending?.controller.abort();
      const controller = new AbortController();
      detailRequest.current = { controller, runId, full: includeHistory };
      let payload: Run;
      try {
        payload = await api<Run>(`/runs/${encodeURIComponent(runId)}?include_history=${includeHistory}`, {
          timeoutMs: 60000, signal: controller.signal,
        });
      } finally {
        if (detailRequest.current?.controller === controller) detailRequest.current = null;
      }
      if (controller.signal.aborted || activeRunIdRef.current !== runId) return;
      setActiveRun((previous) => {
        // Same committed revision: retain the explicitly loaded full curve across cheap polls.
        // A changed checkpoint requires a fresh full read rather than mixing revisions.
        if (!includeHistory && previous?.run_id === runId && previous.history_mode === "full" &&
            typeof payload.read_revision === "string" && previous.read_revision === payload.read_revision) {
          return { ...payload, history_mode: "full", telemetry_live: previous.telemetry_live, phases: previous.phases };
        }
        return payload;
      });
      updateRunSummary?.(runId, effectiveRunSummary(payload));
      if (resetPage) {
        analysisOpen.current = false;
        setKeyframeAnalysis(null);
        setKeyframeAnalysisError("");
        setMemoryEffectiveness(null);
        setGraphQuality(null);
        resetQaState?.();
        setReviewDrafts({});
        await loadReviews(runId);
      }
      await Promise.all([
        ...(analysisOpen.current
          ? [loadKeyframeAnalysis(runId), loadMemoryEffectiveness(runId)]
          : []),
        // 图质量面板始终展示在 run 详情中，不应依赖记忆分析面板是否展开。
        // resetPage 表示刚切换到一条 run，此时立即读取一次；后续展开时再复用同一入口。
        ...(resetPage || analysisOpen.current ? [loadGraphQuality(runId)] : []),
        loadQaPage ? loadQaPage(resetPage ? 1 : undefined) : Promise.resolve(),
      ]);
    },
    [
      activeRunIdRef,
      updateRunSummary,
      resetQaState,
      loadReviews,
      loadKeyframeAnalysis,
      loadMemoryEffectiveness,
      loadGraphQuality,
      loadQaPage,
    ],
  );

  const setAnalysisOpen = useCallback((open: boolean) => {
    analysisOpen.current = open;
    const runId = activeRunIdRef.current;
    if (open && runId) {
      void Promise.all([
        loadKeyframeAnalysis(runId),
        loadMemoryEffectiveness(runId),
        loadGraphQuality(runId),
      ]);
    }
  }, [activeRunIdRef, loadKeyframeAnalysis, loadMemoryEffectiveness, loadGraphQuality]);

  /**
   * 1.0 `:2333` 是「惰性建草稿」的副作用版本，React 里 render 不能写 state，
   * 改成纯读取（缺省返回共享空对象）+ `setReview` 显式写。消费方按
   * `reviewFor(summary)` 取值、`setReview(qaId, patch)` 更新，等价于 1.0 的 v-model。
   */
  const reviewFor = useCallback(
    (summary: { qa_id?: unknown } | string | null | undefined): ReviewDraft => {
      const qaId = typeof summary === "string" ? summary : String(summary?.qa_id || "");
      return reviewDrafts[qaId] || EMPTY_REVIEW;
    },
    [reviewDrafts],
  );

  const setReview = useCallback((qaId: string, patch: ReviewDraft) => {
    setReviewDrafts((prev) => ({
      ...prev,
      [qaId]: { verdict: "", note: "", ...prev[qaId], ...patch },
    }));
  }, []);

  /** 1.0 `:2338`。注意：1.0 这里**没有** confirm/alert（任务描述有误，见报告）。 */
  const saveReviews = useCallback(async () => {
    const runId = activeRunIdRef.current;
    if (!runId || reviewSavingRef.current) return;
    reviewSavingRef.current = true;
    setReviewSaving(true);
    try {
      const reviews = Object.fromEntries(
        Object.entries(reviewDrafts).filter(([, value]) => value?.verdict),
      );
      await post(`/runs/${encodeURIComponent(runId)}/reviews`, { reviews });
      await loadQaPage?.();
    } finally {
      reviewSavingRef.current = false;
      setReviewSaving(false);
    }
  }, [activeRunIdRef, reviewDrafts, loadQaPage]);

  /**
   * 1.0 `:2567`。下载走浏览器（`window.open`），**不走 `api()`**，所以是
   * `/api/v1/...` 字面量而不是 client 的相对路径。`exportSftTraces` 同形（backup `:1401`）。
   */
  const exportTraces = useCallback(() => {
    const runId = activeRunIdRef.current;
    if (!runId) return;
    if (!exportScores.length) {
      emit("请至少勾选一个评分再导出");
      return;
    }
    window.open(
      `/api/v1/runs/${encodeURIComponent(runId)}/export-trace?scores=${exportScores.join(",")}`,
      "_blank",
    );
  }, [activeRunIdRef, exportScores, emit]);

  /** 最老 1.0 backup `codex-macro-20260828-121920 :1401` 的 `exportSftTraces`。 */
  const exportSftTraces = useCallback(() => {
    const runId = activeRunIdRef.current;
    if (!runId) return;
    if (!exportScores.length) {
      emit("请至少勾选一个评分再导出");
      return;
    }
    window.open(
      `/api/v1/runs/${encodeURIComponent(runId)}/export-sft?scores=${exportScores.join(",")}`,
      "_blank",
    );
  }, [activeRunIdRef, exportScores, emit]);

  /**
   * 1.0 `:2467`。1.0 用裸 `fetch`（不是 `api()`），路径自带 `/api` 前缀——照搬。
   * 失败静默保留 config 里的默认值（1.0 `catch { /* keep defaults *\/ }`）。
   */
  const loadJudgePrompts = useCallback(async () => {
    try {
      const data = (await fetch("/api/v1/judge-prompts").then((response) => response.json())) as {
        kinds?: PromptKindMeta[];
      };
      const kinds = data.kinds || [];
      setJudgePromptKinds(kinds);
      const next = { ...promptDrafts };
      for (const meta of kinds) {
        // answer_quality 的兜底是配置里的 `judge_prompt`（1.0 `:2770`
        // `config.custom_judge_prompt || config.judge_prompt`）。少了它，
        // `/judge-prompts` 不返回 default 时提示词就是空串 → `canRejudge` 永久为假。
        const fallback = meta.kind === "answer_quality" ? judgePromptFallback : "";
        if (meta.kind) next[meta.kind] = meta.custom || meta.default || fallback;
      }
      setPromptDrafts(next);
      setRejudgePrompt(next.answer_quality || "");
    } catch {
      /* keep defaults from config */
    }
  }, [promptDrafts, judgePromptFallback]);

  const switchPromptKind = useCallback(
    (kind: string) => {
      setActivePromptKind(kind);
      setRejudgePrompt(promptDrafts[kind] || "");
    },
    [promptDrafts],
  );

  /**
   * 1.0 `:2481`。「恢复默认」= 该 kind 的 `default`，没有就退回配置里的 `judge_prompt`。
   *
   * **`fallback` 的默认值必须是 `judgePromptFallback`**：此前它是空串、而唯一调用方
   * （`RunsView`）也不传，于是「恢复默认」在 `/judge-prompts` 缺 `default` 时把提示词
   * 清空——按钮随之变灰，用户看到的是「恢复默认」反而把功能弄没了。
   */
  const resetJudgePrompt = useCallback(
    (fallback = judgePromptFallback) => {
      const meta = judgePromptKinds.find((item) => item.kind === activePromptKind) || null;
      const value = meta?.default || fallback;
      setRejudgePrompt(value);
      setPromptDrafts((prev) => ({ ...prev, [activePromptKind]: value }));
    },
    [judgePromptKinds, activePromptKind, judgePromptFallback],
  );

  const activeRejudge = useMemo<RejudgeTask | null>(() => activeRun?.rejudge || null, [activeRun]);

  /** 1.0 `:365-368`。 */
  const rejudgePercent = useMemo(() => {
    const task = activeRejudge;
    return task?.total ? Math.round(((task.completed || 0) / task.total) * 100) : 0;
  }, [activeRejudge]);

  /** 1.0 `:369-374`。 */
  const canRejudge = useMemo(
    () =>
      Boolean(
        (activeRun?.item_count || activeRun?.summary?.completed) &&
          !["running", "pending"].includes(String(activeRun?.status)) &&
          activeRejudge?.status !== "running" &&
          rejudgePrompt.trim(),
      ),
    [activeRun, activeRejudge, rejudgePrompt],
  );

  /**
   * 1.0 `startRejudge` 的 `window.confirm` 抽成待确认动作：`requestRejudge()` 只做
   * 1.0 `:2584-2585` 的守卫与置位，`confirmRejudge()` 才执行 confirm 分支里的 POST，
   * `cancelRejudge()` 取消。UI 自行弹窗，测试也不必 mock `window.confirm`。
   */
  const requestRejudge = useCallback((): boolean => {
    if (!canRejudge || rejudgeSubmittingRef.current) return false;
    setRejudgeConfirmation({
      message: `仅使用现有 ${activeRun?.item_count || 0} 条 Agent 回答重新调用 Judge。旧评分将保留在历史记录中，确定开始？`,
    });
    return true;
  }, [canRejudge, activeRun]);

  const cancelRejudge = useCallback(() => setRejudgeConfirmation(null), []);

  /** 1.0 `:2583-2601` confirm 分支的主体。`judge` 对应 1.0 的连接配置区。 */
  const confirmRejudge = useCallback(
    async (judge: RejudgeJudgeConfig = {}) => {
      setRejudgeConfirmation(null);
      const runId = activeRunIdRef.current;
      if (!runId) return;
      rejudgeSubmittingRef.current = true;
      setRejudgeSubmitting(true);
      setError?.("");
      try {
        await post(`/runs/${encodeURIComponent(runId)}/rejudge`, {
          judge_url: (judge.judge_url || "").trim(),
          judge_model: (judge.judge_model || "").trim(),
          judge_provider_id: judge.judge_provider_id || "",
          ...(judge.judge_api_key ? { judge_api_key: judge.judge_api_key } : {}),
          system_prompt: promptDrafts.answer_quality || rejudgePrompt,
          task_system_prompt: promptDrafts.task_decision || undefined,
          evidence_system_prompt: promptDrafts.evidence || undefined,
        });
        await onRunsReloaded?.();
        await loadActiveRun();
        onRejudgeStarted?.();
      } catch (error) {
        setError?.((error as Error).message);
      } finally {
        rejudgeSubmittingRef.current = false;
        setRejudgeSubmitting(false);
      }
    },
    [
      activeRunIdRef,
      setError,
      promptDrafts,
      rejudgePrompt,
      onRunsReloaded,
      loadActiveRun,
      onRejudgeStarted,
    ],
  );

  return {
    activeRun,
    setActiveRun,
    keyframeAnalysis,
    keyframeAnalysisLoading,
    keyframeAnalysisError,
    memoryEffectiveness,
    graphQuality,
    loadActiveRun,
    setAnalysisOpen,
    loadKeyframeAnalysis,
    loadMemoryEffectiveness,
    loadGraphQuality,
    loadReviews,
    reviewDrafts,
    reviewFor,
    setReview,
    saveReviews,
    reviewSaving,
    exportScores,
    setExportScores,
    exportTraces,
    exportSftTraces,
    judgePromptKinds,
    activePromptKind,
    setActivePromptKind,
    promptDrafts,
    setPromptDrafts,
    rejudgePrompt,
    setRejudgePrompt,
    loadJudgePrompts,
    switchPromptKind,
    resetJudgePrompt,
    activeRejudge,
    rejudgePercent,
    canRejudge,
    rejudgeSubmitting,
    rejudgeConfirmation,
    requestRejudge,
    confirmRejudge,
    cancelRejudge,
  };
}

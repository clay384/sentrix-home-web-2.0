/**
 * PhotoBench 前端的组合层（组合根）。
 *
 * 视图切片（`features/config` / `features/runs` / `features/qa` / `features/qa-browser`）
 * 都是纯 props 驱动的展示层，数据层是 `hooks/` 下的七个 hook。这个文件把两者接起来，
 * 并复刻 1.0 `App.vue`（只读权威）里唯一一处横跨全部区块的逻辑：`init()`（`:2731-2767`）。
 *
 * 两处视图切换照搬 1.0：只有 `activeView` 一个状态（`"runs"` / `"qa-browser"`），
 * 没有 URL 语义（计划 §2 裁决），所以不引 react-router。
 *
 * ## 组合层持有的跨切片状态
 *
 * 1. **逐题状态归组合层**（`useQaPage`）。1.0 是单组件，`qaDetails` 同时喂「QA 逐题结果」
 *    与记忆面板；2.0 拆 hook 后若各自持有一份，记忆面板的「已展开轨迹」永远为空。所以
 *    这里起**唯一一个** `useQaPage` 实例，同时传给 `QaResults` 与 `RunDetail` 的记忆面板。
 *    `useRuns` 需要它的 `loadQaPage` / `resetQaState`，但 `activeRunId` 又由 `useRuns`
 *    提供——用 `qaRef` 打破这个环（thunk 在调用时读 ref，那时 `qa` 已建好）。
 *
 * ## init() 与 1.0 的三处已知出入（都有原因，逐条登记在报告里）
 *
 * 1. **配置编排走 `useConfig.initConfig({ onConfig, loadRuns, loadProfiles })`**：
 *    1.0 的 `init()` 把「配置字段」与「`loadJudgePrompts` + `/manifests` + `loadRuns(1)` +
 *    `loadProfiles()`」放进**同一个** `try`，任一条失败都会跳过 `manifests.value = ...`
 *    （`:2751-2757`）。`initConfig` 以 options 收这三条不属于配置切片的 loader，把它自己
 *    的 `/manifests` 赋值留在同一个错误边界内；`loadRuns` 的失败仍会中止赋值。
 * 2. **`loadRuns(1)` 先 await，再并发其余**：`useModelEndpoint.loadProfiles` 读的是
 *    React state（1.0 读同步 `ref`），`onConfig` 里 `applyConfig` 刚写入的模型字段在同一轮
 *    闭包里读不到。先 await 一次网络请求，让那次写入完成一次渲染，再用 `modelApiRef` 取最新闭包。
 * 3. **`loadJudgePrompts` 换到运行详情侧**：1.0 只有一份提示词状态；2.0 里 `RejudgeCard`
 *    绑的是 `useRunDetail` 那份，所以 init 调 `runs.loadJudgePrompts()`（不再是
 *    `useConfig` 那份）。它自己吞异常（1.0 的裸 fetch + catch 原样），不影响 `manifests`
 *    的失败语义。
 */
import { useEffect, useRef, useState } from "react";

import { api, post } from "./api/client.ts";
import { Lightbox } from "./components/Lightbox.tsx";
import type { LightboxTarget } from "./components/Lightbox.tsx";
import { Loading } from "./components/Loading.tsx";
import { ViewTabs } from "./components/ViewTabs.tsx";
import type { ActiveView } from "./components/ViewTabs.tsx";
import { ConfigSection } from "./features/config/ConfigSection.tsx";
import { startDisabledReason } from "./features/config/rules.ts";
import { QaBrowserView } from "./features/qa-browser/QaBrowserView.tsx";
import { QaResults } from "./features/qa/QaResults.tsx";
import { RunsView } from "./features/runs/RunsView.tsx";
import { useConfig } from "./hooks/useConfig.ts";
import { useModelEndpoint } from "./hooks/useModelEndpoint.ts";
import { useQaPage } from "./hooks/useQaPage.ts";
import type { UseQaPageApi } from "./hooks/useQaPage.ts";
import { useReuseMode } from "./hooks/useReuseMode.ts";
import { useRuns } from "./hooks/useRuns.ts";
import { openImageTarget } from "./lib/details.ts";
import { chainDetailFor } from "./lib/graphs.ts";
import type { ChainDetail, ChainMedia, GraphLayer, GraphNode } from "./lib/graphs.ts";
import { effectiveRunSummary } from "./lib/runSummary.ts";

export interface AppProps {
  /**
   * 初始加载态。真实入口（`main.tsx`）传 `true`：1.0 的 `loading` 从 `true` 起、
   * `init()` 的 `finally` 置 `false`（`:267` / `:2759`）。组件测试要立刻断言视图时
   * 不传（默认 `false`）。
   */
  initialLoading?: boolean;
}

/** 1.0 的 `a || b || c || ""`：取第一个非空字符串。 */
function firstString(...values: unknown[]): string {
  for (const value of values) {
    if (typeof value === "string" && value) return value;
  }
  return "";
}

export function App({ initialLoading = false }: AppProps) {
  const configApi = useConfig();
  // 默认复用当前已加载的模型：本地评测通常不该为了跑一份 10QA 再重启模型。
  // 用户在「可用模型」里换型号、或编辑模型服务地址时，hook 会清掉这个勾选。
  const [selectedModels, setSelectedModels] = useState<Set<string>>(
    () => new Set(["__current__"]),
  );
  const modelApi = useModelEndpoint({
    vllmTargetId: configApi.vllmTargetId,
    selectedModels,
    setSelectedModels,
    onDirty: configApi.markConnectionConfigDirty,
  });

  // 打破「useRuns 要 qa 的 loader、qa 要 useRuns 的 activeRunId」这个环：thunk 在调用时
  // 读 ref（loadActiveRun / 轮询 / saveReviews 都是异步发生后触发，那时 qa 已建好）。
  const qaRef = useRef<UseQaPageApi | null>(null);
  const runs = useRuns({
    loadQaPage: (page) => qaRef.current?.loadQaPage(page) ?? Promise.resolve(),
    resetQaState: () => qaRef.current?.resetQaState(),
    // 1.0 `:2770`：`/judge-prompts` 失败或缺 default 时的兜底。
    judgePromptFallback: configApi.config?.judge_prompt || "",
  });
  /** 逐题状态唯一实例；`QaResults` 与记忆面板共享（见文件头 1）。 */
  const qa = useQaPage({ activeRunId: runs.activeRunId, activeRejudge: runs.activeRejudge });
  qaRef.current = qa;

  const [selectedAlbum, setSelectedAlbum] = useState("album3-14");
  const [selectedQa, setSelectedQa] = useState("compact-10q");

  // 相册切换后，QA 题集必须跟随当前 manifest。否则旧相册的默认题集会
  // 被继续提交，例如把 album3 的 compact-10q 发给 human-daily-video-30-v12，
  // 后端会正确拒绝为 unknown qa_set。只在当前选择无效时回退到该相册的第一套题集。
  useEffect(() => {
    const manifest = configApi.manifests.find((item) => item.album_id === selectedAlbum);
    if (!manifest) {
      if (configApi.manifests.length > 0) setSelectedAlbum(configApi.manifests[0]!.album_id);
      return;
    }
    const qaSets = manifest.qa_sets || [];
    if (qaSets.length > 0 && !qaSets.includes(selectedQa)) setSelectedQa(qaSets[0]!);
  }, [configApi.manifests, selectedAlbum, selectedQa]);

  const reuseApi = useReuseMode({
    manifests: configApi.manifests,
    runs: runs.runs,
    sentrixUrl: configApi.sentrixUrl,
    selectedQa,
    setSelectedAlbum,
    setSelectedQa,
    judgeProviderId: configApi.judgeProviderId,
    judgeProviders: (configApi.config?.judge_providers || []) as Array<Record<string, unknown>>,
    judgeModel: configApi.judgeModel,
    setJudgeUrl: configApi.setJudgeUrl,
    setJudgeModel: configApi.setJudgeModel,
    onDirty: configApi.markConnectionConfigDirty,
  });

  const [deleteScopeAfterRun, setDeleteScopeAfterRun] = useState(false);
  const [suiteRunning, setSuiteRunning] = useState(false);
  const [lightbox, setLightbox] = useState<LightboxTarget | null>(null);
  const [activeView, setActiveView] = useState<ActiveView>("runs");
  const [loading, setLoading] = useState(initialLoading);
  /** init() 是否已跑完（`loading` 的初值可被 `initialLoading=false` 关掉，不能拿它当判据）。 */
  const [initDone, setInitDone] = useState(false);
  /** 1.0 `chainDetail` ref（`:281`）：记忆面板点开节点后置位。 */
  const [chainDetail, setChainDetail] = useState<ChainDetail | null>(null);

  // `loadProfiles` 读的是 state；init 里 `applyConfig` 刚写的值在同一轮闭包读不到。
  // 用 ref 存最新一次渲染的 API，等一次 await（loadRuns 的网络往返）后再取。
  const modelApiRef = useRef(modelApi);
  useEffect(() => {
    modelApiRef.current = modelApi;
  });

  // ── init() — 1.0 `App.vue:2731-2767` ─────────────────────────────────────
  /* oxlint-disable react-hooks/exhaustive-deps -- init() 是 1.0 的 onMounted，只跑一次：
     依赖里的 hook 对象每次渲染换 identity（loadProfiles 还随 state 变），进依赖会让
     init 反复重放。只跑一次由 `initStarted` ref 保证（同时挡掉 StrictMode 的二次 effect）。 */
  const initStarted = useRef(false);
  useEffect(() => {
    if (initStarted.current) return;
    initStarted.current = true;
    void (async () => {
      let modelEndpoint = "";
      try {
        await configApi.initConfig({
          // 1.0 :2738-2741：模型服务字段（归 useModelEndpoint）+ model endpoint。
          onConfig: (cfg) => {
            modelApi.applyConfig(cfg);
            const runtimeConfig = (cfg.runtime_config || {}) as Record<string, unknown>;
            const targets = cfg.vllm_targets || {};
            const targetId = cfg.default_vllm_target_id || Object.keys(targets)[0] || "";
            const target = targets[targetId] || {};
            modelEndpoint = firstString(
              runtimeConfig.model_base_url,
              cfg.default_vllm_model_base_url,
              target.model_base_url,
            );
          },
          // 1.0 :2751-2757 的四条：loadRuns 先串行 await（见文件头 2），其余并发。
          loadRuns: () => runs.loadRuns(1),
          loadProfiles: () => modelApiRef.current.loadProfiles(),
        });
        // 1.0 :2751 的 loadJudgePrompts —— 唯一真相在运行详情侧（见文件头 3）。
        await runs.loadJudgePrompts();
      } finally {
        setLoading(false);
        setInitDone(true);
      }
      // 1.0 :2760。
      if (modelEndpoint.trim()) void modelApi.loadCurrentModel({ openPopover: false, endpoint: modelEndpoint });
    })();
  }, []);
  /* oxlint-enable react-hooks/exhaustive-deps */

  // 1.0 :2758 / :2761-2765：init 结束后，若列表里有 running/pending 的 run，自动选中并开轮询。
  const autoSelected = useRef(false);
  useEffect(() => {
    if (!initDone || autoSelected.current) return;
    if (["pending", "running"].includes(String(runs.runPage.scan?.state))) {
      runs.startPolling();
      return;
    }
    autoSelected.current = true;
    const current = runs.runs.find((run) => ["running", "pending"].includes(String(run.status)));
    if (!current) return;
    runs.selectRun(current);
    runs.startPolling();
  }, [initDone, runs, runs.runs, runs.selectRun, runs.startPolling]);

  // ── 启动 / 停止（1.0 `startSuite` :2674-2685 / `stopSuite` :2686-2690）────────
  // 两者只做 JSX 事件处理，不需要 memo：直接定义成普通函数，避免 useCallback 依赖
  // 每轮重建的 `startInput`（那只会让 identity 抖动，没有收益）。
  const startInput = {
    hasRunning: runs.hasRunning,
    suiteRunning,
    selectedModels,
    modelEndpoint: modelApi.modelEndpoint,
    vllmManagerUrl: modelApi.vllmManagerUrl,
    selectedEndpointModel: modelApi.selectedEndpointModel,
    runMode: reuseApi.runMode,
    existingScopeId: reuseApi.existingScopeId,
  };

  const startSuite = async () => {
    const blocked = startDisabledReason(startInput);
    if (blocked && !blocked.includes("运行中")) {
      window.alert(blocked);
      return;
    }
    if (blocked) return;
    if (
      reuseApi.runMode === "build" &&
      !window.confirm("构建相册模式只导入并处理数据，不做 QA 测评；产物相册会保留。确定开始？")
    )
      return;
    setSuiteRunning(true);
    try {
      const selectedManifest = configApi.manifests.find((item) => item.album_id === selectedAlbum);
      const qaSets = selectedManifest?.qa_sets || [];
      const effectiveQa = qaSets.includes(selectedQa) ? selectedQa : (qaSets[0] || selectedQa);
      const result = await post<{ run_ids?: string[] }>("/runs", {
        album_id: selectedAlbum,
        qa_set: reuseApi.runMode === "build" ? undefined : effectiveQa,
        mode: reuseApi.runMode,
        existing_scope_id: reuseApi.runMode === "reuse" ? reuseApi.existingScopeId : undefined,
        models: [...selectedModels],
        sentrix_url: configApi.sentrixUrl.trim(),
        judge_url: configApi.judgeUrl.trim(),
        judge_model: configApi.judgeModel.trim(),
        judge_provider_id: configApi.judgeProviderId,
        ...(configApi.judgeApiKeyDirty ? { judge_api_key: configApi.judgeApiKey } : {}),
        vllm_target_id: configApi.vllmTargetId,
        vllm_manager_url: modelApi.vllmManagerUrl.trim(),
        model_base_url:
          selectedModels.has("__current__") || modelApi.modelEndpointUserEdited
            ? modelApi.modelEndpoint.trim()
            : "",
        endpoint_model: selectedModels.has("__current__") ? modelApi.selectedEndpointModel : "",
        delete_scope_after_run: reuseApi.runMode === "full" ? deleteScopeAfterRun : false,
      });
      // 1.0 :2682-2683：选中新 run。`useRuns` 没暴露 `setActiveRunId`，`selectRun` 是等价入口。
      await runs.loadRuns(1);
      const runId = result.run_ids?.[0];
      if (runId) runs.selectRun({ run_id: runId });
      runs.startPolling();
    } catch (e) {
      configApi.setError((e as Error).message);
    } finally {
      setSuiteRunning(false);
    }
  };

  const stopSuite = async () => {
    if (!window.confirm("确定停止当前所有评测任务？")) return;
    await api("/cancel-active", { method: "POST", body: "{}" });
    await runs.loadRuns();
    await runs.loadActiveRun();
  };

  /** 1.0 `openChainNode`（`:1984-2015`）：先并发补拉当前页所有详情，再算弹窗数据。 */
  const openChainNode = (mode: string, layer: GraphLayer, node: GraphNode): void => {
    const current = qaRef.current;
    if (!current) return;
    const indices = (current.qaPage.items || [])
      .map((summary) => summary.index)
      .filter((index): index is number => index != null);
    void Promise.allSettled(indices.map((index) => current.loadQaDetail(index))).then(() => {
      const latest = qaRef.current;
      if (!latest) return;
      // await 之后要读**权威**的详情/分页（渲染快照可能过期）。
      const snapshot = latest.getQaSnapshot();
      setChainDetail(
        chainDetailFor({
          mode,
          layer,
          node,
          page: snapshot.page,
          details: snapshot.details,
          albumId: runs.activeRun?.album_id,
        }),
      );
    });
  };

  // 记忆面板的数据（1.0 `:3147-3284`）。1.0 在 `activeRun` 为假时不进详情区，这里同形。
  const memory = runs.activeRun
    ? {
        run: runs.activeRun,
        manifests: configApi.manifests,
        qaDetails: qa.qaDetails,
        qaPageItems: qa.qaPage.items ?? [],
        memoryEffectiveness: runs.memoryEffectiveness,
        keyframeAnalysis: runs.keyframeAnalysis,
        keyframeAnalysisLoading: runs.keyframeAnalysisLoading,
        keyframeAnalysisError: runs.keyframeAnalysisError,
        summary: effectiveRunSummary(runs.activeRun),
        chainDetail,
        onOpenChainNode: openChainNode,
        onCloseChainDetail: () => setChainDetail(null),
        onOpenImage: (media: ChainMedia) => {
          const target = openImageTarget(media);
          if (target) setLightbox(target);
        },
      }
    : undefined;

  if (loading) {
    return <Loading />;
  }

  return (
    <>
      <main className="app-shell">
        <ViewTabs activeView={activeView} onSelect={setActiveView} />
        {activeView === "runs" ? (
          <>
            <ConfigSection
              connection={{ ...configApi, config: configApi.config, manifests: configApi.manifests }}
              model={{ ...modelApi, selectedModels, setSelectedModels }}
              reuse={reuseApi}
              album={{ selectedAlbum, setSelectedAlbum, selectedQa, setSelectedQa }}
              start={{
                ...startInput,
                deleteScopeAfterRun,
                onDeleteScopeAfterRunChange: setDeleteScopeAfterRun,
                onStart: () => {
                  void startSuite();
                },
                onStop: () => {
                  void stopSuite();
                },
              }}
              error={configApi.error}
            />
            <RunsView
              runs={runs}
              judgeModel={configApi.judgeModel}
              judgeConfig={{
                judge_url: configApi.judgeUrl,
                judge_model: configApi.judgeModel,
                judge_provider_id: configApi.judgeProviderId,
                // 与 `startSuite` 同一条口径：只有用户改过才发送 key，避免把
                // 后端已存的密钥原样回传（`useConfig` 就是这么判 dirty 的）。
                judge_api_key: configApi.judgeApiKeyDirty ? configApi.judgeApiKey : undefined,
              }}
              memory={memory}
              saveJudgePrompt={(kind, prompt) => {
                void (async () => {
                  // 保存的是运行详情侧那份（用户刚编辑的）；成功后写回它的草稿
                  // （1.0 `saveJudgePrompt` 末尾的 `promptDrafts[kind] = prompt`）。
                  const ok = await configApi.saveJudgePrompt(kind, prompt);
                  if (ok) runs.setPromptDrafts((prev) => ({ ...prev, [kind]: prompt.trim() }));
                })();
              }}
            />
            {/* 1.0 里逐题结果是 `RunDetail` 的 `v-else` 分支，未选中 run 时整块不存在。
                2.0 拆成两个兄弟组件后忘了这层条件，初始状态会多出一整块
                「QA 逐题结果 / 筛选结果 0 / 全部 0 条」的空白面板。 */}
            {runs.activeRun ? (
            <QaResults
              activeRun={runs.activeRun}
              qa={qa}
              resultRef={runs.runDetailRef}
              reviewFor={runs.reviewFor}
              onReviewChange={(summary, patch) => runs.setReview(String(summary.qa_id || ""), patch)}
              reviewSaving={runs.reviewSaving}
              onSaveReviews={() => {
                void (async () => {
                  await runs.saveReviews();
                  // 1.0 `saveReviews`（`:2338`）末尾重拉当前页。
                  await qa.loadQaPage();
                })();
              }}
              onOpenMedia={setLightbox}
            />
            ) : null}
          </>
        ) : (
          <QaBrowserView
            sentrixUrl={configApi.sentrixUrl}
            manifests={configApi.manifests}
            onOpenMedia={setLightbox}
          />
        )}
      </main>
      <Lightbox target={lightbox} onClose={() => setLightbox(null)} />
    </>
  );
}

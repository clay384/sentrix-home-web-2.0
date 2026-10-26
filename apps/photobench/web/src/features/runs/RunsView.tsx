/**
 * 「评测运行」视图：把 `RunsTable`（1.0 `:2989-3037`）与 `RunDetail`（1.0 `:3039-` 起）
 * 组成完整视图。
 *
 * **数据层由组合层注入**：`useRuns`（内含 `useRunDetail`）在 `App.tsx` 里调用一次——
 * 1.0 `init()`（`:2731-2767`）要在挂载时 `loadRuns(1)`、发现运行中的 run 就自动选中并开
 * 轮询，这些都需要 runs 的数据层；若本组件自己再调一次 `useRuns`，就会有两个实例、
 * 两条轮询。所以这里只接 `runs`（外加配置切片的 `judgeModel` / `saveJudgePrompt`，
 * 经 `RejudgeCard` 的接缝透传），**不发请求**。
 *
 * 1.0 里 RunsTable 与 RunDetail 是 `<main class="app-shell">` 下的兄弟节点（中间没有包装层），
 * 所以这里返回 fragment，不加多余 DOM。
 */
import type { RejudgeJudgeConfig } from "../../hooks/useRunDetail.ts";
import type { UseRunsApi } from "../../hooks/useRuns.ts";
import type { MemoryLayerPanelProps } from "../memory/MemoryLayerPanel.tsx";
import { RunDetail } from "./RunDetail.tsx";
import { RunsTable } from "./RunsTable.tsx";

export interface RunsViewProps {
  /** 组合层持有的 runs 数据层（`useRuns`，已内含 `useRunDetail`）。 */
  runs: UseRunsApi;
  /** 配置切片的 `judgeModel`；`RejudgeCard` 缺省显示 `-`（1.0 `judgeModel || '-'`）。 */
  judgeModel: string;
  /**
   * 重判要带上的 **Judge 连接配置**（1.0 `startRejudge` `:2606-2623`）。
   *
   * 不传的话 POST 体里 `judge_url` / `judge_model` / `judge_provider_id` 都是空串，
   * `judge_api_key` 则**永远不会发送**——于是重判要么落到后端默认 Judge，要么因认证
   * 失败整轮判不出来，而按钮上显示的模型名与实际调用的可能不是同一个。
   * 由组合层从 `configApi` 透传（与 `startSuite` 同一份来源）。
   */
  judgeConfig?: RejudgeJudgeConfig;
  /**
   * 配置切片的 `saveJudgePrompt(kind, prompt)`。**显式收参**：组合层用运行详情侧
   * （`useRunDetail`）的 `activePromptKind` / `rejudgePrompt` 去调它——1.0 只有一份
   * 提示词状态，保存的就该是用户刚编辑的那份。
   */
  saveJudgePrompt: (kind: string, prompt: string) => void;
  /** 记忆面板（1.0 `:3147-3284`）的数据；不传则整个面板不渲染。 */
  memory?: MemoryLayerPanelProps;
}

export function RunsView({ runs, judgeModel, judgeConfig, saveJudgePrompt, memory }: RunsViewProps) {
  return (
    <>
      {/* **运行列表/详情/重判的错误出口。** 此前 `useRuns.error`（列表加载、删除、
          选中 run 失败）与 `useRunDetail` 写入的错误都没有任何组件消费——点开一条
          run 失败时界面只是空着，用户看不出是加载中还是出错了。配置区的 `error`
          只覆盖配置请求，不覆盖这些。 */}
      {runs.error ? <p className="error">{runs.error}</p> : null}
      <RunsTable
        runs={runs.runs}
        runPage={runs.runPage}
        activeRunId={runs.activeRunId}
        onSelectRun={runs.selectRun}
        onDeleteRun={(run) => {
          void runs.deleteRun(run);
        }}
        onChangeRunPage={runs.changeRunPage}
        regionRef={runs.runsRegionRef}
      />
      <RunDetail
        activeRun={runs.activeRun}
        exportScores={runs.exportScores}
        onExportScoresChange={runs.setExportScores}
        onExportTraces={runs.exportTraces}
        onExportSftTraces={runs.exportSftTraces}
        memory={memory ? { ...memory, onExpandedChange: runs.setAnalysisOpen } : undefined}
        onLoadTelemetry={() => { void runs.loadActiveRun({ includeHistory: true }).catch((error: Error) => { if (error.name !== "AbortError") runs.setError(error.message); }); }}
        graphQuality={runs.graphQuality}
        rejudge={{
          activeRejudge: runs.activeRejudge,
          judgePromptKinds: runs.judgePromptKinds,
          activePromptKind: runs.activePromptKind,
          onSwitchPromptKind: runs.switchPromptKind,
          rejudgePrompt: runs.rejudgePrompt,
          onRejudgePromptChange: runs.setRejudgePrompt,
          judgeModel,
          onSavePrompt: () => saveJudgePrompt(runs.activePromptKind, runs.rejudgePrompt),
          onResetPrompt: runs.resetJudgePrompt,
          canRejudge: runs.canRejudge,
          rejudgeSubmitting: runs.rejudgeSubmitting,
          rejudgeConfirmation: runs.rejudgeConfirmation,
          onRequestRejudge: runs.requestRejudge,
          onConfirmRejudge: () => {
            void runs.confirmRejudge(judgeConfig);
          },
          onCancelRejudge: runs.cancelRejudge,
          rejudgePercent: runs.rejudgePercent,
        }}
      />
    </>
  );
}

/**
 * 6 张 Pipeline 执行阶段卡 + 阶段错误明细。逐行照搬
 * 1.0 `services/photobench/frontend/src/App.vue:3097-3129`（只读权威；任务表给的是
 * `:3090-3118`，实测 phase-list 到 `</div>` 收在 `:3129`）。
 *
 * `phasePercent` 是 1.0 `App.vue:2195-2198` 的私有函数，2.0 lib 尚未收，照搬在本文件。
 * `phaseSummary` / `phaseErrorDetails` / `phaseErrorSummary` / `pipelineMetricRows` 已在
 * `lib/details.ts`，直接 import。
 *
 * `phaseSummary(key, phase, itemCount)` 的第三参是 1.0 闭包读的 `activeRun.item_count`
 * （1.0 `:2175` 的 `activeRun.value?.item_count`），模板里只传两个参数——这里补上。
 */
import { useEffect, useState } from "react";
import type { Phase, Run } from "../../api/types.ts";
import { EXECUTION_PHASES } from "../../lib/constants.ts";
import { phaseErrorDetails, phaseErrorSummary, phaseSummary, pipelineMetricRows } from "../../lib/details.ts";
import { statusLabel } from "../../lib/format.ts";

/** 1.0 `App.vue:2195-2198`。 */
function phasePercent(phase: Phase | undefined): number {
  const progress = phase?.progress;
  return progress?.total
    ? Math.min(
        100,
        Math.round(
          (((Number(progress.processed) || 0) +
            (Number(progress.failed) || 0) +
            (Number(progress.skipped) || 0)) /
            progress.total) *
            100,
        ),
      )
    : 0;
}

export interface PhaseListProps {
  activeRun: Run;
}

const importStageLabels: Record<string, string> = {
  scanning: "扫描目录", media_processing: "分析媒体", media_staging: "读取/处理媒体",
  event_assignment: "事件归属", event_summarization: "正在总结事件",
  participant_refresh: "刷新事件参与者", family_graph: "推断家庭图谱",
  person_insights: "生成人物画像", completed: "处理结束",
};

function elapsedSeconds(start: unknown, end: number): string {
  const time = typeof start === "string" ? Date.parse(start) : NaN;
  return Number.isFinite(time) && Number.isFinite(end)
    ? String(Math.max(0, Math.floor((end - time) / 1000))) : "?";
}

export function PhaseList({ activeRun }: PhaseListProps) {
  const [now, setNow] = useState(() => Date.now());
  const terminal = ["completed", "completed_with_errors", "failed", "cancelled", "interrupted"].includes(activeRun.status ?? "");
  const running = !terminal && Object.values(activeRun.phases ?? {}).some((phase) => phase?.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  return (
    <>
      <h3>Pipeline 执行阶段</h3>
      {activeRun.mode === "build" ? <p>仅构建记忆空间；QA、Judge 和评分有效性不适用。</p> : null}
      <div className="phase-list">
        {EXECUTION_PHASES.filter((p) => activeRun.mode !== "build" || p.key !== "qa_eval").map((phaseDef, index) => {
          const phase = activeRun.phases?.[phaseDef.key];
          const start = typeof phase?.started_at === "string" ? Date.parse(phase.started_at) : NaN;
          const displayPhase = !terminal && phase?.status === "running" && Number.isFinite(start)
            ? { ...phase, total_seconds: Math.max(0, (now - start) / 1000) } : phase;
          const progress = phase?.progress;
          const observation = progress?.observation;
          const finishedAt = phase?.finished_at ?? (terminal ? activeRun.finished_at : null);
          const end = typeof finishedAt === "string" ? Date.parse(finishedAt) : now;
          const errors = phaseErrorDetails(phase);
          const metrics = pipelineMetricRows(phase);
          return (
            <article className="phase-card" key={phaseDef.key}>
              <div className="phase-title">
                <div className="phase-name">
                  <span className="phase-step">{index + 1}</span>
                  <b>{phaseDef.label}</b>
                </div>
                <span className={"phase-status " + (phase?.status || "pending")}>
                  {statusLabel(phase?.status || "pending")}
                </span>
              </div>
              <p className="phase-summary">{phaseSummary(phaseDef.key, displayPhase, activeRun.item_count)}</p>
              {phaseDef.key === "photo_import" && phase?.progress?.current_directory ? (
                <div className="import-progress-panel" data-testid="import-waiting">
                  <div className="import-progress-head">
                    <div className="import-progress-location">
                      <span className="import-progress-eyebrow">当前目录</span>
                      <strong title={String(phase.progress.current_directory)}>{String(phase.progress.current_directory)}</strong>
                    </div>
                    <span className="import-progress-clock">本目录请求已用 {elapsedSeconds(progress?.request_started_at, end)} 秒</span>
                  </div>
                  <div className="import-progress-stats">
                    <span><b>{observation?.completed ?? "?"}</b><small>已处理</small></span>
                    <span><b>{observation?.total ?? "?"}</b><small>媒体总数</small></span>
                    <span><b>{observation?.imported ?? "?"}</b><small>新增</small></span>
                    <span><b>{observation?.deduplicated ?? "?"}</b><small>去重</small></span>
                    <span><b>{observation?.failed ?? "?"}</b><small>失败</small></span>
                  </div>
                  <div className="import-progress-meta">
                    <span className="import-progress-stage">
                      {importStageLabels[observation?.stage ?? ""] ?? observation?.stage ?? "逐媒体进度尚不可用"}
                      {observation?.current_file ? ` · ${observation.current_file}` : ""}
                    </span>
                    <span className="import-progress-freshness">
                      {observation ? `后端状态：${observation.status ?? "未知"} · 更新于 ${observation.updated_at ?? "未记录"}` : "等待进度接口"}
                    </span>
                  </div>
                  {progress?.request_error ? <p className="import-progress-warning">请求错误：{progress.request_error}；产品是否已停止以独立进度为准。</p> : null}
                  {progress?.availability !== "available" ? <p className="import-progress-warning">查询状态：{progress?.availability ?? "未接入"} · {progress?.query_error ?? "等待进度接口"}；保留的计数不是当前实时进度。</p> : null}
                  {!terminal && phase?.status === "running" && progress?.last_received_at &&
                    Number(elapsedSeconds(progress.last_received_at, now)) > 10
                    ? <p className="import-progress-warning">进度查询超过 10 秒未刷新，当前显示上次观测。</p> : null}
                  <div className="import-progress-directory">
                    已完成目录 {Number(phase.progress.directories_done ?? 0)}/{Number(phase.progress.directories_total ?? 0)}
                  </div>
                </div>
              ) : null}
              {phaseDef.key === "photo_import" && observation &&
                typeof observation.total === "number" && observation.total > 0 &&
                typeof observation.completed === "number" ? (
                  <div className="import-progress-bar-wrap">
                    <progress aria-label="当前目录媒体处理进度" max={observation.total} value={observation.completed} />
                    <small>媒体处理进度；事件总结和人物画像另行收尾。</small>
                  </div>
                ) : null}
              {errors.length > 0 ? (
                <details className="phase-errors">
                  <summary>{phaseErrorSummary(phase)}</summary>
                  <ul>
                    {errors.slice(0, 20).map((raw, detailIndex) => {
                      const detail = raw as Record<string, unknown>;
                      return (
                        <li key={`${String(detail.sample_id || "error")}-${detailIndex}`}>
                          <b>{String(detail.sample_id || "未知样本")}</b>
                          <span>{String(detail.reason || detail.error || "未提供原因")}</span>
                          <small>
                            {String(detail.error_type || detail.status || "错误")}
                            {detail.asset_id ? ` · ${String(detail.asset_id)}` : ""}
                          </small>
                        </li>
                      );
                    })}
                  </ul>
                  {errors.length > 20 ? <p>仅展示前 20 条，完整记录已保存在 run.json。</p> : null}
                </details>
              ) : null}
              {(phaseDef.key === "photo_import" || phaseDef.key === "pipeline_processing") && phase?.progress ? (
                <div className="phase-bar">
                  <div className="phase-bar-fill" style={{ width: `${phasePercent(phase)}%` }} />
                </div>
              ) : null}
              {phaseDef.key === "pipeline_processing" && metrics.length > 0 ? (
                <div className="phase-metrics pipeline-metrics">
                  {metrics.map((row) => (
                    <div className="phase-metric" key={row[0]}>
                      <span>{row[0]}</span>
                      <strong>{row[1]}</strong>
                      <small>{row[2]}</small>
                    </div>
                  ))}
                </div>
              ) : null}
            </article>
          );
        })}
      </div>
    </>
  );
}

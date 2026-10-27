import { useState } from "react";

import {
  resultSetPhotoUrl,
  type AssistantProgressEvent,
} from "../../shared/api/client";
import { Lightbox } from "../../shared/ui/Lightbox";

/**
 * 助手这一轮检索到的结果集缩略图墙。
 *
 * 数据只来自已经发生的进度事件：`tool_result` 的 `payload.result` 就是工具 observation，
 * 里面带着 `result_set_id` / `asset_ids` / `total` / `has_more`（`tools.py:_page_outcome`）。
 * **前端不重新检索、不重排**，只负责把 handle 映射成授权图片 URL
 * （`resultSetPhotoUrl` → `GET /assistant/result-sets/{id}/photo`）。
 *
 * 分页：后端每页 6 张、handle 是全局序号（第 2 页从 `photo_7` 开始，
 * `domain/assistant/result_set.py`）。observation 里**没有** `page` 这一格，
 * 所以页号优先读结构化的 `result.page`（后端以后加就直接用），否则 `search_memories`
 * 固定是第 1 页，`get_result_page` 从它自己那句进度文案
 * 「已翻到第 N 页…」里取——那是这一格里唯一存在的页号。
 */

const DEFAULT_PAGE_SIZE = 6;

export interface ResultSetView {
  resultSetId: string;
  page: number;
  handles: string[];
  total: number;
  remaining: number;
  hasMore: boolean;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

function pageNumber(
  event: AssistantProgressEvent,
  payload: Record<string, unknown>,
  result: Record<string, unknown>,
): number {
  if (typeof result.page === "number" && result.page >= 1) return Math.floor(result.page);
  if (typeof payload.page === "number" && payload.page >= 1) return Math.floor(payload.page);
  if (payload.tool === "search_memories") return 1;
  const matched = /第\s*(\d+)\s*页/.exec(event.text);
  return matched !== null ? Math.max(1, Number(matched[1])) : 1;
}

/** 从一组进度事件里取**最近一次**检索结果，返回这一页要展示的 handle。 */
export function resultSetFromEvents(events: AssistantProgressEvent[]): ResultSetView | null {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (event.stage !== "tool_result") continue;
    const payload = asRecord(event.payload);
    if (payload === null) continue;
    const result = asRecord(payload.result);
    if (result === null) continue;

    const resultSetId = typeof result.result_set_id === "string" ? result.result_set_id : "";
    if (resultSetId === "") continue;

    const assetIds = Array.isArray(result.asset_ids)
      ? result.asset_ids.filter((item): item is string => typeof item === "string")
      : [];
    if (assetIds.length === 0) continue;

    const total =
      typeof result.total === "number" && result.total >= 0
        ? Math.floor(result.total)
        : assetIds.length;
    const page = pageNumber(event, payload, result);
    const pageSize =
      typeof result.page_size === "number" && result.page_size > 0
        ? Math.floor(result.page_size)
        : DEFAULT_PAGE_SIZE;
    const start = Math.max(0, (page - 1) * pageSize);
    const count = Math.max(0, Math.min(pageSize, total - start, assetIds.length - start));
    const handles = Array.from({ length: count }, (_, offset) => `photo_${start + offset + 1}`);
    const remaining = Math.max(0, total - (start + handles.length));
    const hasMore = typeof result.has_more === "boolean" ? result.has_more : remaining > 0;

    return { resultSetId, page, handles, total, remaining, hasMore };
  }
  return null;
}

export interface ResultSetWallProps {
  events: AssistantProgressEvent[];
  scopeId: string;
  onNextPage?: () => void;
  busy?: boolean;
}

export function ResultSetWall({ events, scopeId, onNextPage, busy = false }: ResultSetWallProps) {
  // Hook 必须在任何提前 return 之前——`view === null` 时也要按同样的顺序调用。
  const [lightbox, setLightbox] = useState<{ src: string; caption: string } | null>(null);
  const view = resultSetFromEvents(events);
  if (view === null || view.handles.length === 0) return null;

  return (
    <section className="result-set-wall">
      <header className="result-set-head">
        <strong>结果集</strong>
        <small>
          共 {view.total} 张{view.remaining > 0 ? ` · 还有 ${view.remaining} 张` : ""}
        </small>
      </header>
      <ul className="result-set-thumbs">
        {view.handles.map((handle) => (
          <li key={handle}>
            {/* **点缩略图是放大**，不是新标签打开原图。原图仍然在
                `resultSetPhotoUrl(..., true)` 里——那是 Lightbox 要显示的源。 */}
            <button
              type="button"
              className="result-set-open"
              title={`${handle} · 查看大图`}
              onClick={() =>
                setLightbox({
                  src: resultSetPhotoUrl(view.resultSetId, handle, scopeId, true),
                  caption: handle,
                })
              }
            >
              <img
                src={resultSetPhotoUrl(view.resultSetId, handle, scopeId)}
                alt={handle}
                loading="lazy"
              />
            </button>
          </li>
        ))}
      </ul>
      {view.hasMore ? (
        onNextPage !== undefined ? (
          <button
            type="button"
            className="result-set-next"
            onClick={onNextPage}
            disabled={busy}
          >
            {view.remaining > 0 ? `还有 ${view.remaining} 张 · 看下一页` : "看下一页"}
          </button>
        ) : (
          <p className="result-set-more">
            {view.remaining > 0 ? `还有 ${view.remaining} 张` : "还有更多"}
          </p>
        )
      ) : null}
      {lightbox === null ? null : (
        <Lightbox
          src={lightbox.src}
          caption={lightbox.caption}
          onClose={() => setLightbox(null)}
        />
      )}
    </section>
  );
}

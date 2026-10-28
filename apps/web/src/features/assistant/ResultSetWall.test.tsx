import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { AssistantProgressEvent } from "../../shared/api/client";
import { ResultSetWall } from "./ResultSetWall";

const ASSET_IDS = [
  "a1", "a2", "a3", "a4", "a5", "a6", "a7",
  "a8", "a9", "a10", "a11", "a12", "a13",
];

function retrievalEvent(overrides: Partial<AssistantProgressEvent> = {}): AssistantProgressEvent {
  return {
    text: "结果集里共有 13 张候选照片。",
    status: "ok",
    stage: "tool_result",
    step_index: 3,
    timestamp: "2026-07-01T10:00:03+00:00",
    kind: "tool_result",
    payload: {
      tool: "search_memories",
      tool_call_id: "call1",
      result: {
        result_set_id: "rs_1",
        total: 13,
        has_more: true,
        asset_ids: ASSET_IDS,
      },
    },
    ...overrides,
  };
}

afterEach(() => cleanup());

describe("结果集缩略图墙", () => {
  it("渲染当前页的缩略图，并给出「还有 N 张 · 看下一页」", () => {
    const onNextPage = vi.fn();
    render(
      <ResultSetWall
        events={[retrievalEvent()]}
        scopeId="album1"
        onNextPage={onNextPage}
      />,
    );

    const images = screen.getAllByRole("img");
    expect(images).toHaveLength(6);
    expect(images[0].getAttribute("src")).toBe(
      "/api/v1/assistant/result-sets/rs_1/photo?handle=photo_1&scope_id=album1",
    );
    expect(images[5].getAttribute("src")).toContain("handle=photo_6");

    fireEvent.click(screen.getByRole("button", { name: "还有 7 张 · 看下一页" }));
    expect(onNextPage).toHaveBeenCalledTimes(1);
  });

  it("页号取自 get_result_page 的事件文案，第 2 页从 photo_7 开始", () => {
    render(
      <ResultSetWall
        events={[
          retrievalEvent({
            text: "已翻到第 2 页（这一份共 13 张候选）。",
            step_index: 5,
            payload: {
              tool: "get_result_page",
              tool_call_id: "call2",
              result: {
                result_set_id: "rs_1",
                total: 13,
                has_more: false,
                asset_ids: ASSET_IDS,
              },
            },
          }),
        ]}
        scopeId="album1"
      />,
    );

    const images = screen.getAllByRole("img");
    // 13 张候选、每页 6 张：第 2 页展示 photo_7..photo_12。
    expect(images).toHaveLength(6);
    expect(images[0].getAttribute("src")).toContain("handle=photo_7");
    expect(images[5].getAttribute("src")).toContain("handle=photo_12");
    expect(screen.queryByRole("button", { name: /看下一页/ })).toBeNull();
  });
});

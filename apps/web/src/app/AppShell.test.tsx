import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../shared/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../shared/api/client")>();
  return {
    ...actual,
    fetchHealth: vi.fn(),
    listMemorySpaces: vi.fn(),
  };
});

import * as client from "../shared/api/client";
import { AppShell } from "./AppShell";
import { ScopeProvider } from "./ScopeProvider";
import { ToastProvider } from "../shared/ui/Toast";

function renderShell() {
  return render(
    <ToastProvider>
      <ScopeProvider>
        <MemoryRouter initialEntries={["/"]}>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<div>概览内容</div>} />
            </Route>
          </Routes>
        </MemoryRouter>
      </ScopeProvider>
    </ToastProvider>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(client.listMemorySpaces).mockResolvedValue([
    { id: "album-1", name: "第一册", kind: "benchmark", status: "active" },
  ]);
  vi.mocked(client.fetchHealth).mockResolvedValue({
    status: "ok",
    version: "0.2.0",
    database: "ok",
    embeddings: "ok",
  });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe("AppShell 壳层入口", () => {
  it("顶栏 ⌘ 打开命令面板（只是按钮，不注册全局快捷键）", async () => {
    renderShell();
    await screen.findByText("第一册");

    fireEvent.click(screen.getByLabelText("打开命令搜索"));
    expect(screen.getByRole("dialog", { name: "打开一个工作区" })).toBeTruthy();
  });

  it("侧栏使用帮助打开静态说明", async () => {
    renderShell();
    await screen.findByText("第一册");

    fireEvent.click(screen.getByText("使用帮助"));
    const dialog = screen.getByRole("dialog", { name: "当前可用能力" });
    expect(dialog.textContent).toContain("导入");
    expect(dialog.textContent).toContain("相册管理");
  });

  it("QA 测评在新窗口打开同主机的 9597", async () => {
    const open = vi.spyOn(window, "open").mockImplementation(() => null);
    renderShell();
    await screen.findByText("第一册");

    fireEvent.click(screen.getByText("QA 测评"));
    expect(open).toHaveBeenCalledWith("http://localhost:9597/", "_blank", "noopener,noreferrer");
  });

  it("相册切换失败时提示，而不是静默停在旧范围", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("storage denied");
    });
    renderShell();
    await screen.findByText("第一册");

    const select = screen.getByLabelText("切换全部相册或独立相册");
    fireEvent.change(select, { target: { value: "album-1" } });

    await waitFor(() =>
      expect(screen.getByRole("status").textContent).toContain("相册切换失败：storage denied"),
    );
  });
});

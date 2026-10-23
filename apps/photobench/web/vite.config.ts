import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// 与 `apps/web/vite.config.ts` 同一体例（defineConfig 从 vitest/config 取，
// 让测试与构建跑同一份 JSX 转换），只有两处不同，都是这个 app 自己的事实：
//
//   port 9502  —— 设计文档 §5.1 的端口表里**没有**「2.0 PhotoBench 前端」这一项
//                 （那里 9501 是 2.0 主产品 Web），所以这是新增约定。计划原建议的
//                 4275 实测被占（`/home/asus/s5verify/web/apps/web` 的 vite）。
//   proxy 9597 —— 2.0 的 PhotoBench 后端；`apps/web` 代到 9598 是主 API，不是这个。
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 9502,
    proxy: {
      "/api/v1": { target: "http://127.0.0.1:9597", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.ts", "src/**/*.test.tsx"],
  },
});

/**
 * QA 测评（2.0 PhotoBench）入口地址。
 *
 * 1.0 `src/app.js:2153` 写死 `${protocol}//${hostname}:8771/`；2.0 的 PhotoBench 在
 * **9597**（`Makefile:132`「端口 9597 出自设计文档 §5.1 的端口表；1.0 跑 8771」）。
 *
 * **不写死 IP。** 主机名与协议从当前页面推导，这样局域网里的手机打开 `192.168.x.x:9501`
 * 时，点进测评会去同一台机器的 9597，而不是回到开发机的 localhost。部署时若端口
 * 另有安排，用 `VITE_PHOTOBENCH_ORIGIN` 整串覆盖。
 */

const DEFAULT_QA_PORT = 9597;

/** Vite 在构建期注入；测试里不存在时退回端口推导。 */
const CONFIGURED_ORIGIN = import.meta.env.VITE_PHOTOBENCH_ORIGIN as string | undefined;

export function qaDashboardUrl(
  location: Pick<Location, "protocol" | "hostname">,
  configured: string | undefined = CONFIGURED_ORIGIN,
): string {
  if (configured && configured.trim() !== "") {
    return configured.trim().replace(/\/?$/, "/");
  }
  return `${location.protocol}//${location.hostname}:${DEFAULT_QA_PORT}/`;
}

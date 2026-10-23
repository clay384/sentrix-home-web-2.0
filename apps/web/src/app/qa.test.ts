import { describe, expect, it } from "vitest";

import { qaDashboardUrl } from "./qa";

describe("qaDashboardUrl", () => {
  it("从当前页面的协议与主机名推导 9597，不写死 IP", () => {
    expect(qaDashboardUrl({ protocol: "http:", hostname: "192.168.0.42" })).toBe(
      "http://192.168.0.42:9597/",
    );
    expect(qaDashboardUrl({ protocol: "https:", hostname: "sentrix.local" })).toBe(
      "https://sentrix.local:9597/",
    );
  });

  it("配置了整串 origin 时以配置为准，并补齐尾斜杠", () => {
    expect(
      qaDashboardUrl({ protocol: "http:", hostname: "127.0.0.1" }, "https://qa.example/base"),
    ).toBe("https://qa.example/base/");
    expect(
      qaDashboardUrl({ protocol: "http:", hostname: "127.0.0.1" }, "https://qa.example/base/"),
    ).toBe("https://qa.example/base/");
  });

  it("空配置回落到端口推导", () => {
    expect(qaDashboardUrl({ protocol: "http:", hostname: "localhost" }, "   ")).toBe(
      "http://localhost:9597/",
    );
  });
});

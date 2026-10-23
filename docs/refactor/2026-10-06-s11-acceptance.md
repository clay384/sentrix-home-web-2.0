# S11 验收记录：独立启停

**验收口径是「独立性」，不是「起得来」。** 设计文档 `:516` 要的是「主 API / Web /
PhotoBench / model-manager **独立启停**」。下面每一条都是**真机实测**，
不是「配置看起来对」。

---

## 1. 独立性矩阵（2026-10-06 实测）

| 动作 | 9598 | 9597 | 8500 | 8100 | 8101 | 8102 |
|---|---|---|---|---|---|---|
| 起点 | 200 | 200 | 200 | 200 | 200 | 200 |
| `stop sentrix-api` | **000** | 200 | 200 | 200 | 200 | 200 |
| `start sentrix-api` | 200 | 200 | 200 | 200 | 200 | 200 |
| `stop sentrix-photobench` | 200 | **000** | 200 | 200 | 200 | 200 |
| **两个都停** | **000** | **000** | **200** | **200** | **200** | **200** |
| 全部起回 | 200 | 200 | 200 | 200 | 200 | 200 |

**「两个都停」那一行是关键**：2.0 全停时，8500 / 8100 / 8101 / 8102 **一个都没受影响**。

## 2. model-manager 与 2.0 互不依赖（三个方向都验了）

| 方向 | 怎么验的 | 结果 |
|---|---|---|
| 2.0 停 → 8500 不受影响 | 见上表 | ✔ |
| **8500 停 → 2.0 仍能启动** | 停 8500，**重启两个 2.0 单元** | ✔ 两个都 200 |
| 结构上无依赖 | `systemctl --user list-dependencies sentrix-api` 只有 `basic.target`/`app.slice`；两个单元文件里 `8500\|model-manager\|vllm` **零命中**；`apps/api` 全仓 grep `8500` 零命中 | ✔ |

**这条是要验证的性质，不是要写的功能**——设计 §2.1（`:147`）明写「2.0 不管理模型进程
生命周期」，所以 2.0 从代码到单元都不该碰它。实测与设计一致。

（做这条测试前先确认了 1.0 PhotoBench **0 个活跃 run**，测完已把 8500 起回。）

## 3. `Restart=` 真的重拉（裸进程缺的那条）

T1102 实测：对两个服务各 `kill -9` 主进程 → **8 秒后 systemd 自动拉起新 pid**、探活 200。

## 4. 前端产物由各自的 FastAPI serve（裁决③）

**生产不再依赖 vite dev。** 实测（重启单元后）：

| 探活 | 结果 |
|---|---|
| `9598 /` | 200 `text/html` |
| `9598 /timeline`（react-router 深链） | 200 `text/html` ← **SPA 回落生效** |
| `9598 /api/v1/health` | 200 `application/json` |
| `9598 /api/v1/nope` | **JSON 404** ← **API 没被静态挂载遮蔽** |
| `9597 /` | 200 `text/html` |
| `9597 /api/config` | 200 `application/json` |
| `9597 /api/runs` | 191 个 run（扫的是 1.0 的 results） |

> **2026-10-07 补注**：上表的路径写于当时，PhotoBench 的前缀当日是 `/api`。2026-10-07 完成 T924 后改为 **`/api/v1`**——上表里的 `/api/config`、`/api/runs` 现在是 `/api/v1/config`、`/api/v1/runs`。本表保持原样作为当时的记录。


## 5. 一条我以为有风险、实测没风险的

**systemd 单元没有继承旧裸进程的 conda `stmem` 环境**（几十个变量），而 2.0 进程内
有 Chinese-CLIP / ONNX 视觉嵌入器。T1102 的判据是「服务起来了、建库了、探活正常」——
**但那不加载那些重模型**，所以我在验收里补了一条它没做的：

**在 systemd 单元下真跑一次导入**（会强制加载视觉嵌入器）：

```json
{"asset_id":"asset_c308767d82ac","status":"processed","deduplicated":false,
 "indexed":true,"placed":true,"event_id":"evt_1d4be65eab09"}
```

**`indexed: true`、`placed: true`、事件也归上了**（首调 9.45s 是加载模型）。
⇒ **conda 环境确实不必要**，T1102 的判断成立，而且现在有实测支撑。

**测试数据已清理**：删掉那个一次性 scope 的整条链（**14 张表**，含 entity/relationship/
search_fts 等），`PRAGMA foreign_key_check` **干净**，assets/events 回到基线 **53 / 17**。

## 6. 未验（留给你）

| 项 | 为什么没验 |
|---|---|
| **重启机器后自启** | 要你安排（单元已 `enable` + `Linger=yes`，但我没重启过机器） |
| **1.0 的 4174 / 8771 独立启停** | 它们是 1.0 的服务，不归 2.0 |
| **两个前端在浏览器里的观感** | 见 `2026-10-05-s10b-acceptance.md`——没有 headless 浏览器 |
| **换模型那条链的真机端到端** | T1103 未做（会动到正在验收的单元）；机制只用一次性 unit 验过。**下一次真跑评测时会自然覆盖** |

## 7. 复跑

```sh
cd /home/asus/Github/sentrix-home-web2.0
make status                    # 两个单元的状态
make restart                   # 重启两个
journalctl --user -u sentrix-api -n 50
```

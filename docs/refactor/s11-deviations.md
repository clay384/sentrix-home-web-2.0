# S11 偏离与收敛记录（T1104：四条收敛）

> 依据：`docs/refactor/2026-10-06-s11-implementation-plan.md` §3 四条；用户 2026-10-06 裁决「四条全做」。
> 本文只记 **T1104**。T1101–T1103 / T1105 / T1106 是并行切片，不在本文范围。
> 核查时间：2026-10-06，机器 asus@192.168.0.153。

---

## 0. 事实更正（相对 S11 计划书与设计文档的转录）

| # | 计划书/设计中写的 | 实测 |
|---|---|---|
| 0.1 | registry 第一处是「**2.0 仓库**的 `configs/…_192_168_0_153.json`」 | **错**。2.0 仓库（`sentrix-home-web2.0/`）**没有 `configs/` 目录**，也没有任何 registry 文件。那份 `configs/…_192_168_0_153.json` 在 **1.0 仓库** `/home/asus/Github/Sentrix-Home-Web/configs/` |
| 0.2 | 「启动脚本却指向 `…_local_100.json`」 | 对，但**只是其中一个脚本**：`1.0 repo/scripts/runtime/start_sentrix_api_8091.sh:62`。1.0 仓库里另有 `scripts/runtime/start_sentrix_api.sh:66` 指 **153 版**，而 8091 的 systemd ExecStart（`/home/asus/sentrix-8091-systemd.sh`）指**部署版**。当前活着的 8091 进程（pid 1765805，裸 uvicorn，**不带任何 `SENTRIX_VLLM_*`**）走的是 `backend/app.py:76` 的默认值 `/home/realmagic/sentrix-vllm/registry.json`（**100 机器的路径**）。所以实际上是**四处**口径，不是三处 |

---

## 1. A：`services/model-manager/` —— 结论「**不建**」

**处置：改文档，不补目录。** 已就地更正设计文档 §2.2（原 `:193`，现该行已删，代之以脚注）。

**依据（设计文档 §2.1，行号未动）**：
- `:139`「services/model-manager → managed model profiles（**2.0 只通过 HTTP 使用**）」
- `:147`「**2.0 不管理模型进程生命周期**。模型进程、锁、PID、显存采样、LoRA 加载逻辑都不进 2.0 的 API 进程」

**为什么补目录是错的**：`services/model-manager/` 若存在，它承载的只能是 profile 注册表、模型进程起停/锁/PID、显存采样、LoRA 加载——**逐条都在 `:147` 的禁止名单里**。2.0 里放不下任何「属于它」的内容，补目录等于把 1.0 的 8500 角色搬进 2.0，直接违反系统边界。因此按不建处置，model-manager 留在 1.0（8500）。

---

## 2. B：registry 三处不一致 —— 核实结果与收敛结论

### 2.1 三处各是什么、差在哪、谁在读

| 处 | 路径 | profiles | 谁在读 |
|---|---|---|---|
| ① 1.0 仓库（**不是 2.0**） | `/home/asus/Github/Sentrix-Home-Web/configs/sentrix_vllm_registry_192_168_0_153.json`（7453 B，2026-09-08） | 13 | `services/vllm_manager/manager.py:24-26` 与 `services/vllm_manager/app.py:33` 的**默认值**；`scripts/runtime/start_sentrix_api.sh:66`。**活的 8500 / 8091 都不读它** |
| ② 部署 | `/home/asus/sentrix-vllm/registry.json`（10036 B，2026-09-25） | **15** | **活的 8500**（`sentrix_vllm_api.py:26` 默认值，进程 pid 1268 无 `SENTRIX_VLLM_REGISTRY`）；`/home/asus/sentrix-8091-systemd.sh` 显式指它（该 unit 现为 dead） |
| ③ 100 机器版 | `/home/asus/Github/Sentrix-Home-Web/configs/sentrix_vllm_registry_local_100.json`（8170 B，2026-09-16） | 13 | `scripts/runtime/start_sentrix_api_8091.sh:62`。**该脚本不是当前 8091 的启动者** |

**内容差异（① vs ②）**：② 是 ① 的**超集/演进版**——② 多 `qwen3.5-4b-bnb4`、`qwen3.5-4b-lora`，且两者共有的 **12 个 profile 内容全都不同**（模型路径/参数已各自演进）。③ 是**另一台机器**（100，realmagic）的 profile 集（`gemma4-12b-it-bnb4-tp2`、`qwen3.5-0.8b-agent-closed-loop-v18` 之类），与 153 不是同一套。

### 2.2 谁权威、其余怎么处理

- **权威 = ② `/home/asus/sentrix-vllm/registry.json`**。理由：它是**唯一被活着的 model-manager 8500 读取**的那份（manager 的 `/registry`、`/switch` 都基于它），模型切换的事实来源就是它。
- **① / ③ 是 1.0 仓库里的陈旧快照，不是权威**。③ 更是**串了机器**（100 的 profile 挂在 153 的启动脚本上）——那是 1.0 的一个 bug。
- **处置：仓库外文件一律不动**（`/home/asus/sentrix-vllm/`、1.0 仓库均只读）。收敛产物落在 **2.0 可读处**：
  1. 本文件（权威判定 + 三处清单）；
  2. 设计文档 §10 的 registry 行已就地更正（注明「1.0 仓库」、标注 ② 权威、指到本文）；
  3. 2.0 **不需要**在仓库里放 registry 的副本——那只会制造第四处。2.0 要用模型 profile 时，**经 HTTP 向 8500 取**（`GET /registry`）。
- **留给 1.0 的待办（不在 2.0 范围，只登记）**：修 `start_sentrix_api_8091.sh:62` 的 `_local_100` 指向；决定 ① 是删除还是由运维从 ② 回灌。2.0 不复刻这些问题（设计 §10「修复，不迁移」）。

---

## 3. C：8500 在 2.0 的定位 —— **不需要自建，只需可选 HTTP 消费**

**明文一句话**（已写入设计文档 §2.1，紧随 `:147`）：
「**2.0 不建 model-manager**……2.0 只在需要时**经 HTTP 消费**它。」

**代码注释**：`apps/photobench/src/photobench/application/config/service.py` 的 `vllm_manager_url` 定义处，指向上面那句话。

**结论细节**：
- `apps/api`（2.0 主 API）全仓 grep `8500` / `model_manager` / `vllm_manager` **零命中**——主产品不碰 model-manager。
- PhotoBench 侧确实还带 `manager_url`：`config/service.py`（`vllm_manager_url`，来源 `runtime_connection.json` / `BENCH_VLLM_API_URL`）与 `config/vllm_targets.json`（每个 target 一个 `manager_url`）。它喂 `infrastructure/runtime/selection.py` 的 `ManagerLifecycleProvider` / `ManagerTelemetryProvider`（跑测时可选地经 HTTP 起停/采样模型）。
- **保留这条通路**。理由：它正是设计 §2.1「2.0 只通过 HTTP 使用 model-manager」的实现；删掉它等于砍掉「通过 HTTP 使用」这条被明文允许的能力。2.0 不自建 8500（不启停），但可选消费它。

---

## 4. D：PhotoBench 侧的 8091 残留 —— 收敛到 9598

改了 4 个文件（1 个是**新增的第 4 处**，见下）：

| 文件 | 改动 | 性质 |
|---|---|---|
| `apps/photobench/src/photobench/application/config/service.py:102` | fallback `http://127.0.0.1:8091` → `9598` | 代码，**受版本管理** |
| `apps/photobench/config/runtime_connection.example.json` | `8091` → `9598` | 模板，受版本管理（任务书未点名，但同属残留） |
| `apps/photobench/config/runtime_connection.json` | `http://192.168.0.153:8091` → `9598` | **部署者配置，`.gitignore` 忽略**（`config/runtime_connection.json`） |
| `apps/photobench/tests/test_config_service.py:131` | 断言兜底 `8091` → `9598` | 测试，跟随代码默认值 |

**关于 `runtime_connection.json` 的取舍（已先读其现有内容）**：它现有 6 个键（`sentrix_url` / `judge_url` / `judge_model` / `model_base_url` / `endpoint_model` / `judge_provider_id`），是 2.0 PhotoBench（9597）在本机**实际生效**的连接配置（`settings.config_root` 默认 = `apps/photobench/config`，`config.py:66`）。**决定：就地改**，因为 153 上这套部署就是 2.0 自己，它该指向 9598；但它是 **gitignore 的部署文件**，改动**不进 git**、也不会随仓库分发到别的实例。所以：
- **可分发的那半**是代码兜底 + `*.example.json`（已在 git 里改）；
- **每个已部署实例必须手工改**自己的 `runtime_connection.json`，否则它继续读旧值（文件值优先于兜底）。本机已改；**其他实例（若有）需运维照做**——这条记在这里，T1105 的 runbook 应带上。

**未动、且经核实属于「刻意保留」而非残留**：`infrastructure/runtime/jetson_telemetry.py:48` 的 `("8771","8091",...)`——S9a 已裁决 Jetson 那份端口集与 `providers.py` **独立**（注释在案），照搬 1.0，不改。

---

## 5. 门禁

见交付报告正文（原文输出）。

---

## 2. 施工中的偏离与纠正（2026-10-06）

### 2.1 裁决③ 的一个推论：是 **2 个单位**，不是 4 个

验收口径写的是「主 API / **Web** / PhotoBench / model-manager 独立启停」。裁决③（dist 挂到
各自的 FastAPI）之后，**Web 与主 API 是同一个进程**（PhotoBench 前端与它的后端同理）。

**这不是缩水**，依据在设计文档自己那里——§2.1（`:147`）：

> **2.0 不管理模型进程生命周期**。模型进程、锁、PID、显存采样、LoRA 加载逻辑都不进 2.0 的 API 进程。

所以「model-manager 独立启停」的含义是**它独立于 2.0**——**是要验证的性质，不是要写的功能**。
已按性质验（见 `2026-10-06-s11-acceptance.md` §2：三个方向都测了）。

### 2.2 **设计文档 `:382` 的 qdrant 症状说法与实测不符**

文档说共享 qdrant 目录会让 2.0「**启动即失败**」。T1105 做了持锁实验：

- 持锁状态下 `create_app()` **照常成功**（returncode 0、mounted=True）；
- 真实后果是 **向量索引不可用 → 检索降级回 SQLite 全扫**（`retrieve_visual` 吞掉
  `VectorIndexUnavailable`），且 `backfill_visual_vectors.py` 会失败。

**结论不变**（`qdrant/` 必须独立），但**症状不是启动崩溃**——而这一点很重要：
「启动即失败」是**看得见**的，「静默降级成全扫」是**看不见**的。runbook §6.1 已按实测写清。

### 2.3 三条我转述错、被核查纠正的

| 我说 | 实际 |
|---|---|
| registry 三处，第一处在 **2.0 仓库** | 第一处在 **1.0 仓库**；2.0 仓库**连 `configs/` 都没有** |
| registry **三处** | **四处**——活着的 8091（裸进程、无 env）落到 `backend/app.py:76` 的 **100 机器路径** |
| PhotoBench 的 8091 残留**两处** | **三处**——漏了 `config/runtime_connection.example.json`（在 git 里，可分发的那份） |

### 2.4 **T1103 纠正了我对 2.0 行为的误述**

我在任务书里写「失败不中断整轮评测（1.0 的 `gamma_sync` 语义）」——**对 2.0 不成立**。
`application/run_execution/service.py::_sync_sentrix_endpoint` 在
`outcome.restarted and not outcome.ok` 时**故意抛**（S9 偏离 §7.3：2.0 重启失败是致命的，
怕产出「标着 A 模型、其实是 B 模型」的结果）。

我把 1.0 的语义当成了 2.0 的。**T1103 没有改这条规则**，还据此把 systemd 路径的失败
一律记成 `restarted=True, ok=False`（「已经试图换、没换成」），好让那条致命规则正确触发；
记成 `restarted=False` 的话，`systemctl` 坏掉时整轮会**静默继续跑错模型**。

### 2.5 一条我以为有风险、实测没风险的

**systemd 单元没有继承旧裸进程的 conda `stmem` 环境**（几十个变量），而 2.0 进程内有
Chinese-CLIP / ONNX 视觉嵌入器。T1102 判「不必要」，判据是「服务起来了、建库了、探活正常」——
**但那不加载那些重模型**。我在验收里补了它没做的：**在单元下真跑一次导入**，
得 `indexed: true` / `placed: true`。⇒ 判断成立，且现在有实测支撑（验收 §5）。

### 2.6 现状缺口：`PHOTOBENCH_SENTRIX_SERVICE_UNIT` **当前没设**

`deploy/` 无命中，9597 进程环境里只有 `PHOTOBENCH_RESULTS_ROOT`。
⇒ **生产的 PhotoBench 现在走的是裸进程路径，不是 systemd 接管**（T1103 的默认值）。
runbook §4.1 已标出并给了补法。这是**部署配置**，不是代码缺陷。

### 2.7 一处值得记的坑：`fastapi.HTTPException` 是子类

`fastapi.HTTPException` 继承自 `starlette.exceptions.HTTPException`，而 `StaticFiles`
抛的是**基类**。T1101 第一版用 `from fastapi import HTTPException` 去 `except` ——
**一条都接不住**，SPA 回落**整条静默失效**（未知路由仍 404，看起来像「回落没写」）。

### 2.8 我的切分方式**第五次**造成并发写冲突

T1103 覆盖了 `apps/photobench/src/photobench/config.py`，T1101 的 `web_dist_root` 一度丢失
（表现为 `TypeError: unexpected keyword argument`），它重新合并。已核实两个字段都在。

前四次：纯函数层 8 处同名函数 / `itemMedia` 归属写错 / hooks 层三处重复 /
两个 agent 改同一个 `RunDetail.tsx`。**根因每次都一样**：我按 1.0 的行号或文件切分，
而接缝处（共享函数、共享文件）没有先划清楚。

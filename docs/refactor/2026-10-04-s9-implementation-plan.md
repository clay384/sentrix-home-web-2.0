# S9 实施计划：PhotoBench 后端重构

> 路线图出处：设计文档 `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md:514`
> ——「**S9 | PhotoBench 后端重构 | 7,751 行 stdlib 单文件 → FastAPI 拆分；断 `sys.path` 硬耦合；
> 修死端点循环；改指 `/api/v1` | 能对 2.0 跑完一轮评测**」。它是 **M3（S8–S11）** 的第二段。
> §5.1 的端口表（`:365`）给 2.0 的 PhotoBench 定的是 **9597**。
>
> 前置：S0–S8 全部完成（S8 于 2026-10-04 收口）。W1（检索换 wemm）完成。

---

## 0. 动工前核查（2026-10-04）

### 0.1 **S9 是「在 2.0 新建一个服务」，不是「搬一个已存在的服务」**

- 2.0 的 `apps/photobench/` 只有 **6 个文件**：`DATASET.md` + 5 个 config 示例。
  **代码一行没有**；`services/` 目录在 2.0 不存在。
- 1.0 的实现在 `services/photobench/backend/benchmark_orchestrator.py`，
  **实测 7,997 行**（设计文档写 7,751——**文档比代码旧**；S10 那个 3,646 同样应为 3,797）。

设计文档把它叫「重构」是准确的：目标产物是按 FastAPI 分层重写，不是拷贝。

### 0.2 有一块设计文档**没单列**的隐藏工作量

`benchmark_orchestrator.py:42-61` 反向 `sys.path` 硬耦合：

```python
REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
from backend.runtime_providers import (...)
from backend.apple_telemetry import LocalAppleUnifiedTelemetryProvider, is_apple_silicon
```

被依赖的两份在 **2.0 里都不存在**：

| 1.0 文件 | 行数 | 2.0 |
|---|---|---|
| `backend/runtime_providers.py` | 869 | **无** |
| `backend/apple_telemetry.py` | 418 | **无** |

**合计 1,287 行要在 2.0 重建**——设计文档没有把这块算进「7,751 行」里。

### 0.3 PhotoBench 现在**跑不通 2.0**

它调的端点与 2.0 的对照（`benchmark_orchestrator.py` 行号）：

| 方法 | 1.0 路径 | 2.0 状态 |
|---|---|---|
| GET | `/api/arbiter/status` | **死端点**（1.0/2.0 均无） |
| POST | `/api/ingest-batches/{id}/cancel` | **死端点**（1.0/2.0 均无，**本次新发现**） |
| POST | `/api/model-profiles/bind-{runtime,cloud-runtime,external-runtime}` | ❌ 2.0 完全没有 |
| POST | `/api/ingest-batches/{id}/complete` | ❌ 2.0 无（语义并入 imports） |
| POST | `/api/memory-spaces` | ❌ 只有 GET（2.0 改为导入时内部自动建） |
| POST | `/api/people/seed-batch` | ⚠️ 只有 `people/seed` |
| GET | `/api/assistant/turn/{id}` | 改名 → `/assistant/turns/{turn_id}` |
| POST | `/api/assistant/turn` | 改两段式（先建 conversation） |
| POST | `/api/import` | 改名 → `/imports` 或 `/media/import` |
| — | 其余（memory-spaces 读、assets、assets/{id}/file、relationships/batch） | ✅ 有，路径加 `/v1` |

### 0.4 两个死端点

1. **`/api/arbiter/status`**（`:762-774`）：设计文档 §10 点过。核对属实——1.0 主 API 里
   `arbiter` 零命中，该端点从不存在；PhotoBench 自己的前端轮询它，后端再去打主 API，
   恒 404 且被 `unsupported` 分支吞掉。
2. **`/api/ingest-batches/{id}/cancel`**（`:2265-2277`，**设计文档没提**）：1.0 的
   ingest-batches 只有 POST / GET / `complete` 三条，**没有 `/cancel`**；调用点被
   `except Exception` 吞掉，静默 404。

### 0.5 部署现状

- 1.0 的 PhotoBench 跑 **8771**，**不受 systemd 管**——裸进程 + `scripts/deploy/restart_photobench.sh`。
- 启动时要扫 `results/` 下 **270 个 run 目录**，**约 55 秒**才绑端口（脚本注释明说）。
- 结果体量大：单个 `run.json` 最大 **447 MB**、`results.jsonl` 337 MB。
- 2.0 的目标端口是 **9597**（设计文档 §5.1）。

---

## 1. 裁决

### 用户 2026-10-04 定的四条

| # | 问题 | 裁决 |
|---|---|---|
| ① | 要不要保留「模型部署编排」那半边 | **照搬全量**——runtime providers / 遥测 / 多模型编排都搬 |
| ② | 2.0 缺的那几个端点怎么办 | **改 PhotoBench 适配 2.0**，不给主 API 加端点 |
| ③ | 评测中途换模型怎么落地 | **PhotoBench 重启 2.0**（带上新的 `vlm_base_url`） |
| ④ | S9 怎么切 | **切三段**：S9a 遥测 → S9b 主链 → S9c 适配 |

### ③ 的由来（**这是本切片最要紧的一条**）

1.0 的多模型评测流程建立在「**不重启主 API 就能换模型端点**」之上：

```python
request_json(f"{sentrix_url}/api/model-profiles/bind-runtime",
             {"manager_url": self.vllm_api_url,
              "model_base_url": self.vllm_model_base_url}, "POST", 30)
# 上一行的注释：Sync .100 Sentrix gamma client to the new model (no restart)
```

而 2.0 的 `vlm_base_url` 是**启动时从 Settings 读死的**（`container.py:248` 的
`ServedModel(settings.vlm_base_url)`）。`ServedModel` 只有**模型名**带 TTL 重探与
`invalidate()`，**base_url 换不了**。

裁决②（不加端点）与裁决①（保留编排）放在一起，出路只有三条：重启 2.0、约束
PhotoBench 只用固定端点、或给 2.0 加 rebind 端点。**用户选第一条。**

**代价要在实现时如实承担**：PhotoBench 于是要管一个它本不该管的进程（起停 2.0），
那属于部署知识。**它与 S11（服务与部署）有重叠**——S11 要做「主 API / Web /
PhotoBench / model-manager 独立启停」，这里的重启逻辑到 S11 可能要收敛。
登记这一点，免得两边各写一套。

---

## 2. 三段

### S9a 运行时提供者与遥测（1,287 行）

**验收**：能在 2.0 里查到本机 GPU/模型进程的遥测，且 provider 可被替换成假件测试。

| # | 内容 |
|---|---|
| T901 | `runtime_providers.py`（869 行）在 2.0 重建。**先读它到底提供了哪些 provider**——1.0 支持 vLLM / llama.cpp / Ollama / Jetson / Apple / 各种 telemetry，其中一部分 2.0 未必用得上（2.0 的设计口径是「不管理模型进程生命周期」）。**但用户裁决是照搬全量**，所以默认全搬，只在某一条明显只服务 1.0 特有部署时才提出来单议 |
| T902 | `apple_telemetry.py`（418 行）重建 |
| T903 | 落位：这些是**基础设施**（子进程 / 系统调用 / HTTP 探活），按 import-linter 契约只能进 `infrastructure/`；端口进 `domain/` |
| T904 | 测试：1.0 那边**没有**这两个模块的测试（它们是被 benchmark 间接覆盖的）。2.0 要为它们补，用假件 |

### S9b orchestrator 主链拆分（7,997 行）

**验收**：能对一个跑着的 2.0 跑完**一轮**评测并产出指标。

| # | 内容 |
|---|---|
| T911 | 拆 `benchmark_orchestrator.py`。建议按域拆：**runs**（建/查/取消/重跑/评审）/ **config**（profiles / targets / judge providers）/ **judge**（打分与重判）/ **telemetry**（GPU 采样、功耗）/ **run-execution**（那条主链：建空间 → 种身份 → 导入 → 处理 → 问答 → 打分 → 聚合）/ **http-client**（`request_json` / `request_bytes` 的替代，2.0 用 `httpx`） |
| T912 | HTTP 栈从 `http.server` 换成 FastAPI + uvicorn（端口 9597）。**40 多条路由**逐条对照搬 |
| T913 | **断 `sys.path` 硬耦合**（裁决①的连带）：不再反向 import 1.0 的 `backend/`，改为用 T901/T902 在 2.0 里的实现 |
| T914 | 它自己的 `results/` 读写（270 个 run、单个 JSON 最大 447 MB）：**开服务时要扫目录**，1.0 因此要 55 秒才就绪。**这是一个真实的启动性能问题**，值得在迁移时一并想（懒加载？索引文件？）——但**先量再改**，不要凭感觉 |

### S9c 结果读侧 + 对 2.0 适配 + 死端点

**验收**：`能对 2.0 跑完一轮评测`（设计文档 S9 的验收原话）。

| # | 内容 |
|---|---|
| T921 | **端点对照表落地**（§0.3 那张）：每一处调用点改成 2.0 的等价接口，或按裁决②删掉 |
| T922 | **修两个死端点**：arbiter 那条**删掉**（它对应的能力在 2.0 也不存在）；`ingest-batches/{id}/cancel` 同样处理——**先确认 2.0 的导入取消语义落在哪**，没有就删 |
| T923 | **换模型 = 重启 2.0**（裁决③）：实现这条路径，并**登记它与 S11 的重叠** |
| T924 | 全局前缀 `/api` → `/api/v1` |
| T925 | 收口：capability-map / module-map / behaviour-baseline / 偏离登记 / openapi |

---

## 3. 门禁与交付物

- 新 `.py` 登 `docs/architecture/module-map.md`；新能力登 `capability-map.jsonl`；
  新常量登 `behaviour-baseline.md`。
- **PhotoBench 是 2.0 仓库里的新 app**（`apps/photobench/`），**它与 `apps/api` 是两套东西**——
  先确认：它要不要受 `make ci` 的同一套 import-linter 契约管？还是有自己的门禁？
  **这是动工前要定的第一个问题**（见 §4）。
- 2.0 仓库根 `Makefile` 的 `ci` 链要不要加 photobench 的测试？同样要先定。

---

## 3.5 工程标准（用户 2026-10-04 裁决）

**PhotoBench 受与 `apps/api` 同一套门禁**：`ruff` + `mypy strict` + 同样的
`domain / application / infrastructure / api` 分层契约，并在 `Makefile` 的 `ci` 链上
加一段 photobench 检查。

理由：**它不会是仓库里唯一无人看守的代码**。仓库全部价值都压在这几道门上，
一个 9,300 行的服务豁免于它们，等于在仓库里开一块飞地。而 FastAPI 拆分本来
就该有分层——设计文档把它叫「重构」也是这个意思。

**代价如实承担**：~9,300 行都要过 strict 类型检查，且分层契约会约束 provider
那批代码的落位（它们碰子进程/系统调用/HTTP，只能进 `infrastructure`）。

---

## 3.6 动工前还要定的两件

1. **1.0 那 8 个测试文件（约 71 KB）怎么处理**：重写、适配、还是先不搬。
2. **`results/` 的 270 个 run 要不要迁进 2.0**：它们按 `DATASET.md` 声明是「本地数据，
   不入 Git」。2.0 的 `apps/photobench/datasets/` 已有 1.4 GB 数据集，`results/` 是另一回事。

---

## 5. 施工记录

### S9a 运行时提供者与遥测（T901–T904，2026-10-04）

**落位结论（§4 那个「动工前要定的第一个问题」）**：PhotoBench 的 Python 代码放在
**`apps/photobench/src/photobench/`**，与 `apps/api/src/sentrix` 同构（
`domain / infrastructure`，S9b 再补 `application / api`），并**新起一个发行包**
`apps/photobench/pyproject.toml`。依据三条：实施设计 §145 定死「PhotoBench 不得读取
主产品 Python 内部模块」——同一个包里就没有隔墙；用户 2026-10-04 裁决 §3.5 要求它与
`apps/api` 同一套门禁，而门禁（mypy `files` / import-linter `root_packages` /
三个 arch 脚本的 `--src`）都按「包」为单位配置；`apps/api` 自己就是
`apps/api/pyproject.toml` + `src/` 布局，照它的体例不需要发明新东西。

**新增**

| 文件 | 一句话 |
|---|---|
| `apps/photobench/pyproject.toml` | 第二个发行包；依赖只有 httpx（1.0 原本就用它） |
| `apps/photobench/src/photobench/__init__.py` | 包标记 + 隔离约束的说明 |
| `apps/photobench/src/photobench/domain/runtime/ports.py` | `InferenceProvider` / `LifecycleProvider` / `TelemetryProvider` 三个 Protocol + `RuntimeProviders` |
| `apps/photobench/src/photobench/infrastructure/runtime/providers.py` | 1.0 `backend/runtime_providers.py` 的重建（全部 provider + 工厂） |
| `apps/photobench/src/photobench/infrastructure/runtime/apple_telemetry.py` | 1.0 `backend/apple_telemetry.py` 的重建 |
| `apps/photobench/src/photobench/infrastructure/runtime/hardware.py` | 只搬 1.0 `backend/hardware.py` 的 `read_cpu_temperature_c` |
| `apps/photobench/tests/{fakes,test_runtime_providers,test_apple_telemetry}.py` | 假件 + 66 条用例（HTTP / 子进程 / `/proc` / `sysfs` 全假） |
| `docs/architecture/photobench-module-map.md` | PhotoBench 自己的模块地图（与 `module-map.md` 同一个校验脚本、另一个 `--src`） |

**修改**：`pyproject.toml`（ruff isort / mypy files / pytest testpaths / coverage source /
import-linter 加 `photobench` 四条契约）、`Makefile`（install 加第二个 editable 包、
lint 与 format 加 photobench 路径、新 `photobench-check` 段并挂上 ci 链）、
`scripts/check_reachability.py` 的 `PENDING_WIRING`（4 条）、
`scripts/check_method_reachability.py` 的 `PENDING_METHODS`（9 条）、
`docs/refactor/capability-map.jsonl`（4 条）、`docs/refactor/behaviour-baseline.md`
（新增一节）、`apps/api/tests/unit/test_architecture_rules.py`（它把契约**条数**
写死成 5，加契约后必须改成 8——见下第 12 条）。

**「行为一致」的证据方式**：除用例之外，另跑了一遍**差分对照**——把 1.0 的
`backend.runtime_providers` / `backend.apple_telemetry` 当预言机，用同一批假件
（子进程、`/proc`、HTTP 全不打）同时喂两个版本，比对返回的字典：纯函数
（`parse_vm_stat` / `parse_macmon_sample` / `parse_agx_performance` /
`_is_related` / `_is_model_process` / 两个 URL 归一化 / 四组常量）、
`_augment_host_process_memory` 的四种情形、Apple 提供者的四个方法，**逐字段一致**。
脚本是一次性的（依赖 1.0 仓库在盘上），**没有进仓库**——进仓库会在没有 1.0 检出的
机器上红。

**偏离与待裁决**

1. **1.0 这两个模块其实有测试**（`backend/tests/test_runtime_providers.py` /
   `test_runtime_provider_system_memory.py` / `test_apple_telemetry.py`），§2 的
   「它们没有测试」不准确。2.0 的用例是**重写**（改用假件对象而不是 `unittest.mock`
   的 patch 目标），不是照搬。
2. **`backend/jetson_telemetry.py`（660 行）不在 S9a**。它是第三个模块：1.0 的
   `runtime_providers.py` **import 了它的两个名字却从未使用**（ruff F401），真正
   构造 Jetson 那路提供者的是编排侧的 `select_runtime_providers`。按「照搬全量」
   的口径它该搬，但它属于 S9b 的 provider 选择逻辑；**本切片只搬了被真正用到的两条**
   （`runtime_providers` + `apple_telemetry`）。**请裁决**：Jetson 那 660 行是进 S9b，
   还是单独切片。
3. **`backend/hardware.py` 只搬了一个函数**（`read_cpu_temperature_c`）。另外四个
   （`collect_gpu` / `collect_cpu` / `collect_memory` / `collect_hardware`）服务
   `run_qa_benchmark` 写 run meta，归 S9b。
4. **只服务 1.0 部署形态的两处，照搬但登记**：(a) `OrinLlamaCppTelemetryProvider`
   把远端写死成 `orin@192.168.0.118` + 固定 PID 文件路径——它只对 153 上跑、
   模型在 118 上这一种部署成立；(b) `LocalAppleUnifiedTelemetryProvider` 只对
   Mac mini M4 成立（`macmon` / `ioreg` / `vm_stat` / `libproc`）。两者都是 1.0 的
   真实部署，按裁决①照搬；**若 2.0 只跑 153，这两条是候选的裁剪对象**。
5. **`chat` / `chat_stream` 的返回类型改了**：1.0 返回 `httpx.Response` 与 httpx 的
   stream 上下文管理器，端口不能把 httpx 摆到 domain 上，改成「解析后的 JSON」与
   「逐行的迭代器」。**失败行为不变**（非 2xx 仍抛异常，只是由适配器抛）。
6. **1.0 的 ABC 基类不搬**（换成 Protocol，实现类不继承），基类上的
   `raise NotImplementedError` 与 `token_count` 的默认实现随之消失——后者落在唯一的
   实现 `OpenAICompatibleInferenceProvider` 上。
7. **`_process_smaps_field_mib` 结尾那行不可达的 `return None` 没搬**（1.0
   `runtime_providers.py:244`）。行为等价。
8. **门禁登记**：`photobench-*` 四个模块进 `PENDING_WIRING`、九个端口方法进
   `PENDING_METHODS`，接线任务写的是 S9b 的 T911–T913。另记一条**本门禁的已知盲区**：
   `start` / `stop` / `list_models` / `gpu_stats` / `process_memory` 五个方法名因为
   photobench 自己的实现里就有 `self.*()` 调用，被判成「已接线」而**不在登记表里**——
   它们是漏报，不是接线。
9. **import-linter 只加了 `photobench` 的四条契约**（`domain-purity` /
   `infrastructure-purity` / `isolation` / 加进 `root_packages`）：`forbidden` 契约
   引用不存在的源模块会加载失败，而 `photobench.application` / `api` / `main` 要 S9b
   才建。S9b 建层时补 `photobench-application-purity` 与 `photobench-layers`。
10. **`check_reachability.py` 的 ROOTS 还没扩到 `photobench.main`**：S9a 没有入口，
    四个模块本来就该登记；**S9b 建出 `photobench.main` 时必须把 ROOTS 扩过去**，
    否则那张 PENDING 表会因为「根找不到」而永远合法。这条不变量现在无人守。
11. **`check_module_map.py` 的依赖边只统计包内 import**，所以 `photobench-isolation`
    契约（禁止 `sentrix.*`）是这条隔离的**唯一**机械保障——它不在模块地图里体现。
12. **`apps/api/tests/unit/test_architecture_rules.py` 的 `CONTRACT_COUNT` 从 5 改成 8**。
    这是 S9a 唯一改动的 sentrix 侧文件，理由是那个常量写死的是**仓库全部契约**的条数，
    而 `lint-imports` 一次跑两个包。**它同时暴露了一个反过来的缺口**：以后任何切片加
    契约都会红在一个与契约本身无关的地方——这次是 S9a 撞上的。
13. **`make ci` 的结果**：`1 failed, 2147 passed`，唯一失败是已知的模型侧 flaky
    `apps/api/tests/e2e/test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers`
    （两次全量跑都在同一个位置红）。因为 pytest 非零退出，`coverage` 那一步之后的
    目标没有跑；另外单独跑了一遍「去掉这条 flaky」的同一条链确认覆盖率和前端门禁。

### S9a 带出来的待定项与处置（2026-10-04）

实施者做完后提了五个「只服务 1.0 特有部署形态」的疑点。逐条处置：

| # | 疑点 | 处置 |
|---|---|---|
| 1 | `OrinLlamaCppTelemetryProvider`（端点/ssh 用户/PID 路径全写死成「模型在 118、PhotoBench 在 153」） | **保留**（用户裁决①「照搬全量」）。2.0 若两者同机它确实是死重量，但那是**部署决定**，不是迁移时该替用户做的决定 |
| 2 | `LocalAppleUnifiedTelemetryProvider`（只对 Mac M4 成立） | **保留**，同上。2.0 的目标机是 153，但 provider 可被替换——这正是把它做成端口的意义 |
| 3 | **`SYSTEM_PROCESS_PORTS` 默认值是 1.0 的部署**（8771 / 8091 / 8500） | **S9b 必须改**（2.0 是 9597 / 9598，model-manager 归 S11）。不改的话 PhotoBench 自己的端口判据是过期的——进程名关键词还能兜住，端口那条已经失效。可由 env 覆盖，所以是「改默认值」不是「能不能改」 |
| 4 | Apple 那份的 `SYSTEM_PROCESS_KEYWORDS` 是**独立一份**（多 `mlx`、不读 env） | **不合并**。1.0 如此；合并会悄悄改变 Apple 主机上的归属结果 |
| 5 | `backend/jetson_telemetry.py`（660 行）：1.0 的 `runtime_providers.py` **import 了它两个名字却从未使用**（F401），真正构造 Jetson provider 的是编排侧的 `select_runtime_providers` | **归 S9b**，与 `select_runtime_providers` 一起搬。它属于「provider 选择逻辑」那一块，不是 provider 本体 |

**另外两条实施者报上来的、我确认为需要跟进的：**

1. **`check_reachability.py` 的 ROOTS 还没扩到 `photobench.main`**——S9a 没有入口，四个模块
   本来就该登记（已在 `PENDING_WIRING` 里注明接线任务是 S9b 的 T911–T913）。
   **但 S9b 建出入口时必须把 ROOTS 也扩过去**，否则那张表会因为「根找不到」而永远合法。
   **这条不变量现在无人守**，已登记在此。
2. **一个门禁盲区（实施者如实报的）**：`LifecycleProvider.start/.stop`、
   `InferenceProvider.list_models`、`TelemetryProvider.gpu_stats/.process_memory`
   五个方法名，因为 photobench 自己的实现里就有 `self.*()` 调用，
   被 method-reachability 判成「已接线」而**不在登记表里**。**那是漏报，不是接线。**
   S9b 接上真调用者后要复查这五个是否真的可达。
3. **`test_architecture_rules.py` 的 `CONTRACT_COUNT` 5→8** 暴露一个反向缺口：
   它写死的其实是**全仓契约条数**，所以**以后任何切片加契约都会红在一个与契约无关的地方**。
   本轮照实改了；要不要把它做成动态的（读 `pyproject.toml`）留待后续。

### S9b-1 服务骨架与 HTTP 层（2026-10-04）

**交付点达成**：9597 起得来、能列出 run。建起 `application / api / bootstrap` 三层与
入口 `photobench.main`；契约从 8 条增至 **11 条**。

#### 一条反直觉的实测，推翻了「懒加载」的设计直觉（T914）

计划书 T914 说「先量再改，不要凭感觉」。量出来的结果**否定了一个很自然的直觉**：

| 步骤（463 MB 的 `run.json`） | 耗时 |
|---|---|
| 只读字节，不解析 | 2.9 s |
| `json.loads` 全部解析 | **2.3 s** |
| **纯 Python 逐字节扫一遍**（想跳过 `items` 只读其余键） | **18.3 s** |

**扫描比直接解析慢 8 倍**——纯 Python 的括号配对比 C 解析器慢一个数量级，
「跳过 items」是**净亏**。（实施者测到 24.8 s / 12×，我用更粗糙的扫描器复测得 18.3 s / 8×，
**结论一致**。）顺带修正一个文档数字：1.0 脚本注释写「约 55 秒」就绪，实测**要 75 秒**。

**所以选的方案是「照常解析、丢掉 items」，不是「懒解析」**，另加两条：
- **启动时后台扫描**（daemon 线程），**端口立刻绑**——实测 `uvicorn` 起来后 **t+3 s**
  就能拿到 200 与数据，扫描 **t+50~60 s** 完成（`total` 从 169 涨到 267）；
- **只留瘦条目**（每个几百字节），进程里任何时刻最多压着一个 run 的 items
  ——1.0 是 11 GB 全常驻；单 run 完整状态按需解析 + 2 条 LRU。

#### 验证

- **差分对照**：把 1.0 当预言机，对 3 个真实 run（0.4 MB/10 题、32.9 MB/100 题、
  **448.9 MB/1167 题**）比对 6 个纯函数，**逐字段一致**。
- **定点变异 12/12 被杀**。头一轮有 2 条存活，查明**是用例太弱**（见下）。

#### 又一条「夹具必须让两种口径给出不同答案」

两条存活变异都是**夹具不具区分力**：

1. **路径穿越**：原来只断言「抛 `KeyError`」——**不拦也一样抛**（文件根本不存在）。
   改成在 `results/` 的**兄弟目录**里真放一份 `run.json`，拦截器才成为唯一让它失败的东西。
2. **`scope_id` 早退**：原来断言「没抛异常」，可那段外面裹着 `except Exception`（1.0 `:6862`），
   真打网络失败也会被吞。改成断言假件的**调用记录为空**。

**这是本项目第五次同一个教训。**

#### 一处「把查得到的不变量换成查不到的」被挡住了

实施者原想给 `ModelProfiles` 的方法起名 `profiles`（跟 1.0 仓储名）。但
`check_method_reachability` 判「端口方法名**全仓唯一**」，一个同名 application 方法会把
`LifecycleProvider.profiles` 挤进**重名盲区**。改名 `query_profiles` 后**盲区名单归 0**。
**这是拿一条查得到的不变量换了查不到的，幸好门禁把它顶回来了。**

#### 五盲区复查（S9a 登记的那条）

| 方法 | 复查结论 |
|---|---|
| `InferenceProvider.list_models` | **现在真接线了**（本切片的 `/api/current-model` 调它，假件钉住） |
| `LifecycleProvider.start` / `.stop` | **仍是盲区**，一个真调用者都没有（属 S9b-2） |
| `TelemetryProvider.gpu_stats` / `.process_memory` | 同上 |

**改坏这几个方法名不会有任何用例变红**——如实登记，**别读成「已接线」**。

#### 门禁

`make ci`：`1 failed / 2234 passed / 6 skipped`，唯一失败是那条已知的模型侧 flaky；
coverage 94.35%；`web-check` 单独补跑绿；photobench 自己的 160 条用例 0.39 s。

### S9b-2 run-execution 主链（2026-10-04）

**交付点**：能对一个跑着的 2.0 跑完一轮「建空间 → 种身份 → 导入 → 处理 → 问答」，
把每题的问答与轨迹落进 run 目录。**真机达成**（见下「真机验收」）。

#### 一条范围调整（用户 2026-10-04 定）

计划书把「对 2.0 的端点适配」放在 S9c（T921）。但主链本来就要调主 API，先照 1.0 的
端点写一遍再在 S9c 改一遍是白费。**本切片直接按 2.0 的接口写**，S9c 相应收窄为
「两个死端点 + 换模型重启 + 收口」。

#### 与 1.0 的相位对照

| 相位 | 2.0 的落地 |
|---|---|
| `model_deploy` | 保留。**`LifecycleProvider.start` / `.stop` 的唯一生产调用者**；`InferenceProvider.chat` / `chat_stream` 也在这里（探活 + 吞吐探测） |
| `scope_setup` | **没有 HTTP 调用**：2.0 的空间由导入路径内部的 `_ensure_space` 建（`import_assets.py:249`） |
| `scope_attach` | 删。reuse 模式的入口，依赖写侧路由（S9b-3） |
| `identity_seed` | 逐个 `POST /people/seed`（2.0 没有 `seed-batch`）+ `POST /relationships/batch` |
| `photo_import` | 按媒体目录逐个 `POST /api/v1/imports`（**服务端目录**，非递归） |
| `pipeline_processing` | 轮询 `GET /api/v1/assets`（2.0 的导入是同步的，通常一两次收敛） |
| `qa_eval` | 每题一个会话：建会话 → 逐条消息发起回合 → 轮询到终态 |
| `gpu_metrics` / `aggregate` | **不搬**（S9b-3） |

#### 五盲区复查（S9a 登记、S9b-1 部分偿还的那笔账）

| 方法 | 本切片之前 | **本切片之后** |
|---|---|---|
| `InferenceProvider.list_models` | S9b-1 已接线 | 不变 |
| `LifecycleProvider.start` | **盲区，零调用者** | **真接线**：`_phase_model_deploy` 调它（`{"profile": …, "wait_ready": False}`），用例钉住「start 被调了几次、带的什么参数」 |
| `LifecycleProvider.stop` | **盲区，零调用者** | **真接线**：`_phase_model_deploy` 在 start 之前调它，用例钉住「stop 的下标 < start 的下标」 |
| `TelemetryProvider.gpu_stats` | **盲区，零调用者** | **真接线**：`GpuSampler.sample_once` |
| `TelemetryProvider.process_memory` | **盲区，零调用者** | **真接线**：同上 |
| （顺带）`TelemetryProvider.system_memory` | S9b-1 登记在 `PENDING_METHODS` | **真接线**：同上，条目已由反向规则删掉 |

**但这五个名字仍然不在 `check_method_reachability` 的表里，也不在它的检查范围内**
——门禁的判据是「这个名字在别处被属性访问过」，而 photobench 自己的 provider 实现里
就有 `self.start()` / `self.gpu_stats()` 之类的调用，于是它们被判成「已接线」。
**盲区本身没有修**（修它要换判据，不在本切片）。所以「可达」这件事现在的证据是
**用例里假件的 `calls` 记录**：`tests/test_run_execution.py` 与 `tests/test_gpu_sampler.py`
改坏这五个方法名会红。

另外两条同一族的账：

- `InferenceProvider.chat` / `chat_stream` 从 `PENDING_METHODS` 删除（真的接上了）。
- `InferenceProvider.health` / `token_count`、`TelemetryProvider.kv_cache` **仍无调用者**
  （主链的探活走 `chat`），登记保留、改注明「S9b-2 余项」。

#### 真机验收（153，对跑着的 9598）

夹具：`/tmp/pb-smoke/album-smoke/` —— 2 张真照片（从 `album3-14` 拷来）+ 1 张参考脸 +
2 道题（从 `compact-10q` 精简），**不进仓库**。

**结果：`status=completed`、`run_valid=true`、主链 12.9 s。**

```
model_deploy         done  0.0s
scope_setup          done  0.0s
identity_seed        done  0.185s   seeded 1（entity_8aa8826981d1）
photo_import         done  0.0s     deduplicated 2
pipeline_processing  done  3.0s     total 3 / processed 3 / pending 0 / failed 0
qa_eval              done  9.7s
```

两道题都走真模型（8100 的 llama-server）：

| 题 | turn 终态 | 交付 | 图片召回 |
|---|---|---|---|
| smoke-q1（地点事实题） | `partial` / `answered_with_gap` | 无附件 | `0.0`（GT 2 张，一张没交付） |
| smoke-q2（找合影） | `complete` / `answered` | `IMG_8653.JPG` + `IMG_8654.JPG` | **`1.0`** |

轨迹逐条落了盘（每秒 5–6 s 的墙钟）：q1 是 12 步（plan → 4×status → 3×model_action →
3×tool_result → terminal），`executed_tools = {search_memories: 1, inspect_photo: 1,
query_memory_facts: 1}`。落盘：`run.json` 21 KB、`results.jsonl` 2 行、`gpu_samples.jsonl`
**0 字节**——采样器真的起了，但这个 scope 的遥测提供者是 `UnavailableTelemetryProvider`
（没配 manager），它按端口口径回 `unavailable`，于是不产出样本。**provider 选择逻辑
没搬**（见下「偏离」），所以这条在真机上没有可用的遥测来源。

| 段 | 真跑 / 假件 |
|---|---|
| 种身份 `POST /api/v1/people/seed` | **真跑**：seeded 1 人（`entity_a0eac5e13278`），带参考图 multipart |
| 导入 `POST /api/v1/imports` | **真跑**（但见下「一个 2.0 缺陷」） |
| 流水线（VLM 观察 / 事件 / 总结） | **真跑**：`imported=2, events=2, summarized=1` |
| 处理 `GET /api/v1/assets` | **真跑** |
| 问答（建会话 → 发起回合 → 轮询） | **真跑**：走真模型（8100 的 llama-server），每轮 1–2 s |
| judge 打分 | **不在本切片**（占位 `pending_judge`） |
| GPU 采样 | 真跑（`gpu_samples.jsonl`），但**聚合**不在本切片 |
| 单元用例 | **全假件**（233→243 条），不碰网络 |

#### 一个 2.0 缺陷（本切片撞出来的，不在本切片修）

`POST /api/v1/imports` 在**先种过身份**的 scope 上必然 500：

```
application/identity/extract_moments.py:162 → repositories/moments.py:127
sqlite3.IntegrityError: FOREIGN KEY constraint failed
```

链路：`timeline.py:155` 的 `_maybe_run_insights` → `run_insights` → `extract_moments`
→ `strategy_moments.upsert`，`person_moments` 的 `event_id` / `cluster_id` 是
`NOT NULL REFERENCES`，而从**参考图**（不属于任何事件）来的那个人脸簇带不出 event。

两条独立的证据：

1. 同一个相册、**不种身份**、在全新 scope 上导入 → `201 Created`（`imported=2, events=2`）；
   种过身份的 scope → 同样的请求 500。差别只在参考图那张 asset 上。
2. 完整去重的重复导入（`imported=0`）→ `201`：`_maybe_run_insights` 只在
   `imported > 0` 时跑，所以它一跳过，500 就没了。

**而且 `_extract_asset` 只接住 `(OSError, ValueError)`**（`extract_moments.py:84-96`），
`IntegrityError` 一路冒到路由变成 500——那段代码自己的注释写的是「一张坏图不该让
其余的都不落库」，但它的 except 名单拦不住这一类。

**本切片的处置**：不动 `apps/api`（不在本切片范围，且改了要重新裁决）。真机验收用
「先把 scope 预热一次，让导入全程去重」绕过；**这条缺陷已登记给 S9b-3/S9c 定夺**。

#### 定点变异（19/19 被杀）

每条都先核过锚点在文件里唯一（`count == 1`）。**头一轮有 3 条存活，其中 2 条查明是
夹具没有区分力**——本仓库第六次同一个教训，记在这里：

| # | 变异 | 结果 | 谁杀的 |
|---|---|---|---|
| 1 | `photo_import` 的 `limit` 写死 1 | KILLED | `test_photo_import_hands_the_server_side_directory_…` |
| 2 | 收工只等一次零待处理（`>= 2` → `>= 1`） | KILLED | `test_processing_requires_two_consecutive_zero_pending_snapshots` |
| 3 | 超时分类退回按文案判（去掉 `isinstance`） | KILLED | `test_a_turn_that_never_reaches_a_terminal_state_fails_its_item` |
| 4 | `model_deploy` 不先卸载（去掉 `stop`） | KILLED | `test_with_a_manager_the_model_is_stopped_then_started_then_probed` |
| 5 | 采样无视关闭开关 | KILLED | `test_sampling_can_be_disabled_for_an_environment_without_telemetry` |
| 6 | 终态判据退回 1.0 的列举成功值 | KILLED | `test_a_partial_turn_is_a_completed_item_not_a_failure` |
| 7 | 进程列表不给 `or []` 兜底 | KILLED | `test_process_lists_default_to_empty_lists_not_none` |
| 8 | **探不到也产出样本**（判据换成 `== "never"`） | KILLED（第二轮） | `test_an_unavailable_gpu_probe_produces_no_sample_at_all` |
| 9 | CPU 温度无条件覆盖每张卡 | KILLED | `test_the_gpu_row_supplies_the_cpu_temperature_when_it_lacks_one` |
| 10 | **`executed_tools` 数发出而不是数回执** | KILLED（第二轮） | `test_summarize_progress_counts_tools_by_their_result_not_their_request` |
| 11 | 交付集不去重 | KILLED | `test_selected_asset_ids_keeps_delivery_order_and_dedupes` |
| 12 | 关键帧不回落到父视频 | KILLED | `test_resolve_predicted_media_falls_back_from_a_keyframe_to_its_parent_video` |
| 13 | id 不转义斜杠（`safe=""` → 默认） | KILLED | `test_list_assets_quotes_the_scope_id` |
| 14 | multipart 把文件排在字段之前 | KILLED | `test_build_multipart_matches_the_1_0_envelope` |
| 15 | `results.jsonl` 写空 | KILLED | `test_write_snapshot_round_trips_and_splits_items_into_jsonl` |
| 16 | 采样不截空 | KILLED | `test_reset_then_append_leaves_exactly_one_sample` |
| 17 | `limit` 数全部文件（不只看媒体后缀） | KILLED | `test_import_target_counts_media_files_only` |
| 18 | `album_id` 不挡路径穿越 | KILLED | `test_an_album_id_with_a_path_separator_is_not_found` |
| 19 | 端点为空时不退回配置 | KILLED | `test_a_run_execution_can_be_built_without_a_manager_or_an_endpoint` |

**两条存活暴露的夹具缺陷（已修）**：

1. **第 8 条**：夹具用的是 `FakeTelemetry(gpu_status="unavailable")`，而那个假件在
   `status != available` 时**不返回 gpus**——于是「按状态跳过」与「按 data 空跳过」
   都得到零个样本，用例对判据没有区分力。改成专用假件
   `_UnavailableWithStaleData`（报 `unavailable` 但 `data.gpus` 里留着上次的读数），
   这才是真的判据所在：**按状态跳过，不是按 data 恰好空**。
2. **第 10 条**：夹具里每个工具恰好「一次发起 + 一次回来」，于是「数发起」与
   「数回执」的输出完全一样。加了一个 `inspect`（**发起两次、只回来一次**，第二次
   被 guard 拦在工具里），两种口径才分得开。
3. 第 3 条第一轮 `count == 0`（`ruff format` 把那个表达式折成了一行），锚点改对后
   被杀——**这条不是存活，是打错靶子**，按纪律记下来。

**每条断言「能区分正确与错误两种实现」的自问**，逐条写在各用例的 docstring 里；
最典型的是 `test_resolve_gt_media_reports_why_a_reference_could_not_be_mapped`
（三态 `ok`/`missing`/`ambiguous`，其中两态的 `asset_id` 都是 `None`，**只断言
`asset_id` 的用例对它们没有区分力**）与 `test_a_question_without_an_answer_in_the_library_costs_recall`
（没有它，「召回 == 1.0」对一个恒返回 1.0 的实现同样成立）。

#### 门禁

`make ci`：**`1 failed, 2311 passed, 14 skipped`（27 分 35 秒）**，唯一失败是那条
**已知的模型侧 flaky**——`apps/api/tests/e2e/test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers`
（断言在 `:189`，比的是模型的输出里有没有内部标识符；S9a 与 S9b-1 两次全量跑红的
是同一条）。**是不是唯一失败：是**——单独重跑它仍然红，而其余 2311 条全绿。
因为 pytest 非零退出，`coverage` 之后的 `web-check` 没有跑；单独补跑 `make web-check`
通过（vite build 94 ms）。

覆盖率 **94.45%**（门槛 85%）。本切片新增的两层：`application/run_execution/` 的
`service.py` 92% / `gpu_sampler.py` 100% / `state.py` 100%，`domain/runs/execution.py` 98%。

`photobench-check` 三件全绿：`check_reachability` 0 问题、
`check_method_reachability` 0 问题（原 3 条被反向规则要求删除——`chat` /
`chat_stream` / `system_memory` 真的接上了）、`check_module_map` 0 问题。
`lint-imports` 11 条契约全 KEPT（没有新增契约，`photobench-isolation` 未破）。
`check_capability_map` 0 问题。

#### 偏离与待裁决

1. **provider 选择逻辑没搬，`apple_telemetry` 至今零调用者。**
   1.0 `select_runtime_providers`（`benchmark_orchestrator.py:176`）要按主机形态在
   Apple / Jetson / Orin / 宿主 nvidia-smi 四路里选遥测，其中 Jetson 那一路还要连
   660 行的 `backend/jetson_telemetry.py` 一起搬。S9a 已裁决「归 S9b」，但 S9b 切三段时
   **没有把它分进任何一段**。后果有两层：(a) `check_reachability` 里那条登记改注成
   「未分配切片」；(b) **真机上遥测没有可用来源**——container 只按「有没有
   `manager_url`」装配遥测，153 上跑的 llama-server（8100）那一路
   `LlamaCppTelemetryProvider` 根本没有被选中的路径，所以真机验收的
   `gpu_samples.jsonl` 是 0 字节。**请裁决它归哪一段。**
2. **`_phase_processing` 的「空导入」判据换了**：1.0 是 `import_accepted_count == 0`
   （`:2913`），2.0 是「这个 scope 里有没有资产」。理由：2.0 的导入会报
   `deduplicated`，「一张都没收」有两种成因（全去重 / 什么都没导），而其中一种是正常的。
   新判据覆盖 1.0 那条的用场（不在空相册上问完整套题），且不会把「重复导入同一个相册」
   误判成失败。
3. **没有后台 persist 写线程**（1.0 `_persist_loop` `:2056`）：本切片同步写快照。
   那个线程存在的理由是「不让磁盘 IO 污染墙钟」+「让 API 的实时视图保持新鲜」，
   而写侧路由属 S9b-3，现在没有实时视图要喂。
4. **没有取消**：`RunCancelledError` / `self._cancel` / 取消路由都属写侧（S9b-3）。
   代价如实记：`_wait_model_ready` 与 `_await_turn` 里的等待**不是取消感知的**，
   1.0 那两处的 `self._cancel.wait(...)` 现在是固定 `sleep`。
5. **`InferenceProvider.health` / `token_count`、`TelemetryProvider.kv_cache` 仍无调用者**
   （主链的探活走 `chat`），登记保留、注明「S9b-2 余项」。
6. **1.0 `backend/hardware.py` 的另外四个函数没有归属**：S9a 的模块地图注记说它们
   「服务 run meta，归 S9b-2」，但**实测它们只被 1.0 的 `backend/app.py` 调用**，
   `benchmark_orchestrator.py` 一次都没调。模块地图那行已按实情改写；它们至今无人认领。
7. **三处刻意偏离 1.0 的行为**（id 一律 `quote(safe="")`、multipart 的非 2xx 转文案、
   超时分类多一条 `isinstance`），逐条记在 `behaviour-baseline.md` 的新一节里。
8. **真机验收绕过了一个 2.0 缺陷**（见上）——验收的 `photo_import` 走的是
   「预热一次让导入全程去重」那条路。**导入链本身（VLM 观察 / 事件 / 总结）是预热那一次
   真跑的**；主链那一次真跑的是「去重 + 处理 + 问答」。
9. **`model_deploy` 的第 4 步（1.0 的 `bind-runtime` 同步）留空**：2.0 的
   `vlm_base_url` 启动时读死，换模型要重启（裁决③），那是 S9c 的 T923。相位记录里
   如实写了 `gamma_sync.ok = false` 与原因。

#### 没做 / 做不了

- judge 打分与重判、遥测聚合（`_phase_gpu_metrics` / `_phase_aggregate`）、
  写侧路由（建 run / 取消 / 重跑 / 评审写入）——S9b-3。
- 两个死端点、换模型重启、收口——S9c。
- 前端一行没动。
- 真机验收的 GPU 采样**没有拿到样本**（原因见偏离 1）。
- `results/` 的 447 MB 单文件问题本切片没有碰（本切片的 run 只有 21 KB）。

### S9b-2 带出的两个待定项与裁决（2026-10-04）

| # | 待定项 | 裁决 |
|---|---|---|
| 1 | **`select_runtime_providers` + `jetson_telemetry.py`（660 行）归哪一段**——S9a 说归 S9b，但 S9b 切三段时没把它分进任何一段。后果之一：**真机遥测没有可用来源**（GPU 采样器真起了，但拿不到样本——153 上 llama-server 那一路没有被选中的路径） | **归 S9b-3**，与遥测聚合一起。它属于「provider 选择 + 遥测」那一块，放在那里才能让采样器真拿到样本 |
| 2 | `backend/hardware.py` 另外四个函数的归属（S9a 的模块地图注记写「服务 run meta，归 S9b-2」） | **不搬**。实测它们**只被 1.0 的 `app.py` 调用**，`benchmark_orchestrator.py` 一次都没调。注记已按实情改写 |

### S9b-3 judge / 遥测聚合 / 写侧（2026-10-04）

**交付点达成**：一轮评测**带着 judge 分数与遥测样本**落盘；写侧路由能建 run / 取消 /
重判 / 评审 / judge 提示词 / 内存画像。这一块也是 S9b 的最后一块。

#### 新增

| 文件 | 一句话 |
|---|---|
| `infrastructure/runtime/selection.py` | 1.0 `select_runtime_providers`（`:176`）连同 `local_lan_ip` / `is_orin_telemetry_host` / `resolve_runtime_framework` / `detect_runtime_framework` 的重建。**它是 S9b-2「153 上采不到样本」的修复点** |
| `infrastructure/runtime/jetson_telemetry.py` | 1.0 `backend/jetson_telemetry.py` 的重建（S9a 裁决「归 S9b」、S9b-2 再次上报的那 660 行）。调用者只有一个：`selection.py` 的 Jetson 分支 |
| `domain/runs/judge.py` | 三段打分提示词与评分规则（1.0 `:455`–`:664`，逐字照搬）+ `parse_judge_json` / `execution_failure` / `thinking_kwargs` / `judge_chat_url` |
| `application/run_execution/judge.py` | `JudgeRunner`：`_judge` / `_judge_task_action` / `_judge_evidence` / `_judge_request`（退避 + 4xx 不重试 + 限流）/ `_judge_item` |
| `infrastructure/http/judge_images.py` | 证据判分的「召回图 → data URL」。**因为 PIL 只能在 infrastructure**（见下「一处门禁红」） |
| `application/runs/suites.py` | 写侧注册表：`start_suite` / `cancel_run` / `cancel_active_suite` / `delete_run` / `save_reviews` / `get_run_judge_prompt` / `start_rejudge` / `start_memory_profile` |
| `api/routes/suites.py` | 写侧路由（含 `resume` 的 501） |
| `infrastructure/config/env.py` | `.env.local` 的 `SecretEnvStore`（judge 密钥；0600 原子写） |
| `tests/{test_selection,test_judge,test_aggregation,test_suites,test_run_execution_cancel}.py` | 新增 5 份用例（+ `fakes_execution` 里的 `FakeHttpClient` / `judge_reply`） |

**改动**：`domain/runs/analysis.py`（`aggregate_gpu_samples` + `apply_live_sample`）、
`domain/runs/execution.py`（`RunCancelledError`）、`domain/{config,results,runtime}/ports.py`
（`SecretEnvStore` / 写侧两个方法 / `RuntimeProviders.source`）、
`application/run_execution/{state,service,gpu_sampler}.py`（后台 persist、取消信号、判分池、
两个收尾相位）、`application/config/service.py`（自定义提示词 + 密钥 + 三个 config 字段）、
`application/runs/index.py`（`upsert` / `remove`）、`infrastructure/results/store.py`
（`save_reviews` / `delete_run`）、`bootstrap/container.py`、`api/routes/config.py`、`main.py`、
根 `pyproject.toml`（judge.py 的 E501 单文件豁免）、`scripts/check_reachability.py` /
`check_method_reachability.py`（各删一条被反向规则追上的登记）。

#### 真机验收（153，对跑着的 9598 与 8100）

夹具：`/tmp/pb-smoke/album-judge/` —— 2 张真照片 + 1 张参考脸 + 2 道题（**不进仓库**）。
判分真打 **8100 的 llama-server**（qwen35-q4km），不烧云端额度。

**结果：`status=completed`，八个相位全 `done`，墙钟 29.0 s。**

```
model_deploy / scope_setup / identity_seed / photo_import /
pipeline_processing / qa_eval / gpu_metrics / aggregate   全部 done
telemetry_source = host_nvidia_smi
```

| 验收点 | 结果 |
|---|---|
| **GPU 采样器真拿到样本了吗** | **是**：`gpu_samples.jsonl` **39 行**，`gpu_metrics.samples_count=39`，首行带 `gpu_utilization_pct` / `memory_used_mib` / `power_draw_w` / `cpu_temperature_c`（宿主 nvidia-smi 口径） |
| judge 分数 | `judge_valid_count=2`，分布 `{"0":0,"1":0,"2":2}`，`answer_quality_mean=2.0` |
| 任务行为判分 | q1 `answer`、q2 `clarify`（与题面 GT 一致） |
| 遥测聚合 | `memory_profile.method = idle_process_minus_reserved_kv_plus_peak_used_kv_v1` |

| 段 | 真跑 / 假件 |
|---|---|
| 建 run（`RunSuites.start_suite`） | **真跑** |
| 主链八相位 | **真跑**（种身份 / 导入 / 处理 / 两道题走真模型） |
| judge 打分 | **真跑**（8100 的 llama-server，4 次判分调用） |
| GPU 采样与聚合 | **真跑**（宿主 nvidia-smi） |
| 单元用例 | **全假件**（291 条），不碰网络 |
| 证据判分（`BENCH_EVIDENCE_JUDGE`） | **没跑**（默认关；它要图片字节与多模态 judge） |

**清理**：验收写了真实库，做完删干净——`foreign_key_check` **0 问题**，
`assets 53`、`events 17`、`entities 36`、`person_moments 8` 全部回到基线。

#### 真机验收抓到两个「假件看不见」的接缝缺陷

两个都是**「一切正常、只是结果不对」**的形状，只有真跑才暴露：

1. **`RunRequest` 被塞进 `state` → run.json 一次都没写出来。** 为了让 `cancel()`
   读得到 `manager_url`，曾经 `state.set("_request", request)`；而 `state.data` 是要
   `json.dumps` 落盘的。第一次验收的表现是：`gpu_samples.jsonl` 一直在长、
   **`run.json` 始终不存在**（后台写线程把 `TypeError` 收进 `_persist_error`）。
   修法是把 request 挂在执行体上；回归用例
   `test_a_real_writer_gets_a_serializable_state` 直接跑 `FileResultWriter`。
2. **回合记录不带 `expected_action` → 一次判分都不发生。** 第二次验收：八相位全 done、
   39 行样本，但每个 item 的 judge 都是 `{"score": None, "reason": "not_labeled"}`
   ——`JudgeRunner.judge_item` 从**回合**上读 `expected_action`（1.0 的 runtime_turns
   带这个字段），而 2.0 的 `_evaluate_one` 没放进去。回归用例
   `test_runtime_turns_carry_the_gt_expected_action`。

**这两条是同一课的第三次**：夹具能跑通不等于链路上每个字段都到位；
「跑完了」与「拿到了」是两件事。

#### 一处门禁红（`arch` 第一步就报出来的）

证据判分要缩放 JPEG，最初写在 `JudgeRunner._inline_images` 里，`lint-imports` 直接报
`photobench-application-purity` BROKEN（`judge -> PIL`）。**惰性 import 也照样算**——
import-linter 看的是 AST 里的 import 节点，不是运行时机。修法是把那段搬到
`infrastructure/http/judge_images.py`，由装配点以可注入回调交给 `JudgeRunner`。

#### 定点变异（6 被杀 / 1 存活，存活的原因查明了）

每条都先核过锚点在文件里唯一（`count == 1`），改一处、跑**指定的那条**用例、还原。
变异跑在仓库的一份 `/tmp` 副本上（`make ci` 同时在跑，不能动工作区）。

| # | 变异 | 结果 | 谁杀的 / 为什么没杀 |
|---|---|---|---|
| 1 | 选择器的 **Jetson 分支整条关掉** | KILLED | `test_a_local_endpoint_on_a_jetson_is_the_uma_pss_path`（`isinstance` 变 HostNvidia） |
| 2 | judge 的 4xx 判定段改成 `500 <= code < 600` | KILLED | `test_a_client_error_is_not_retried`（调用次数 1 → 2） |
| 3 | 聚合的 `p95` 永远取最大值 | KILLED | `test_p95_uses_the_rank_index_not_the_maximum_once_there_are_past_twenty_samples` |
| 4 | 写线程的收尾判断改成 `if stopping: return` | **存活** | 见下 |
| 5 | 回合不带 `expected_action` | KILLED | `test_runtime_turns_carry_the_gt_expected_action` |
| 6 | 重判目标退回 `conversation` | KILLED | `test_rejudge_archives_the_old_score_and_writes_a_new_one` |
| 7 | 实时曲线不截尾 | KILLED | `test_apply_live_sample_trims_the_history_to_the_tail` |

**第 4 条为什么存活（以及为什么不是用例的错）**：`stop_persist_writer` 自己会先
`persist(wait=True)` 把 `requested` 追平，而写线程的内层等待条件是
`written >= requested`——两者一起保证了「停止前那份请求一定会被写出去」。
所以改「停止判断」这一行**在外部不可观测**：不是夹具没有区分力，是被测代码的结构
让这个变异等价于恒等变换。**这一条如实记在这里，不当成被杀。**

**两条头一轮打错靶子的**（按纪律记下来）：第一轮的 M1 改的是「宿主分支的
`not jetson`」，但 Jetson 分支在它之前就返回了，那个条件是**冗余的**——变异等价于
恒等变换；第一轮的 M4 改的是 `stop_persist_writer` 里那句 `persist(wait=True)`，
被写线程的 debounce 写覆盖掉了，同样不可观测。两条都换成真能改变行为的变异后，
M1 被杀；M4 换到另一处仍然存活，原因如上。

#### 与 1.0 的偏离（逐条登记在 `behaviour-baseline.md` 的 S9b-3 一节）

`mode` 只接 `full`（`reuse`/`build` 显式 400）；`POST /runs/{id}/resume` 返回 501；
重判的轮次来源从 `conversation` 换成 `runtime_turns`；重判不重判证据项；
内存画像不调 `bind-runtime`（如实记 `gamma_sync.ok=false`）。

#### 一条与任务书措辞不符的实测

任务书说 `select_runtime_providers` 要「在 Apple / Jetson / Orin / nvidia-smi 四路里选」。
**1.0 的选择逻辑里没有 Orin 那一路**：`OrinLlamaCppTelemetryProvider`（110 行）在
`benchmark_orchestrator.py` 里只被 import 过、**一次都没被构造**（S9a 报的同一处 F401）；
「Orin」只通过 `_is_orin_telemetry_host` 参与框架探测（8100 端口的远端主机判成
llama.cpp）。这一段**照 1.0 的分支搬，不替它补一条没人验证过的 ssh 采样路**。

#### 门禁

`make ci`（2026-10-04 23:12→23:49）：

- 前七段全绿：`lint` / `format-check`（440 files）/ `typecheck`（mypy strict，268 files）/
  `arch`（`lint-imports` **11 契约全 KEPT**，含 `photobench-application-purity`）/
  `photobench-check`（可达性 0 / 端口可达性 0 / 模块地图 0）/ `check-docs`（capability-map 0、
  module-map 0、openapi 契约一致）。
- `coverage`：**`3 failed, 2402 passed, 8 skipped`（36 分 33 秒）**，覆盖率 **93.73%**
  （门槛 85%，已达标；`make` 因 pytest 非零退出停在这一步）。

三条失败**没有一条是 PhotoBench 的**：

| 失败的用例 | 判读 |
|---|---|
| `apps/api/tests/e2e/test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers` | **已知的模型侧 flaky**（S9a / S9b-1 / S9b-2 三次全量跑红的是同一条）。**它不是唯一的失败**——这次还有下面两条；单独重跑它仍然红（4.84 s） |
| `apps/api/tests/contract/test_search_routes.py::test_the_lexical_channel_is_fused_into_the_endpoint` | **满载下的 flaky**：断言 `text_ann` 这条通道在导入时写进了向量，而它取决于**文本向量 sidecar 在那一刻答不答**。**单独重跑整个文件 12 条全绿** |
| `apps/api/tests/contract/test_search_routes.py::test_the_endpoint_reports_only_the_channels_that_have_hits` | 同上（同一个断言族） |

因为 pytest 非零退出，`web-check` 没有跑；单独补跑 `make web-check` 通过
（`tsc -b` / oxlint / vitest / vite build 90 ms）。

**一次环境事故如实记录**：第一次 `make ci`（23:02）跑到 8% 时停住不动。查明
**8100 上的 llama-server 已经不在了**（它自己的日志最后一行是
`srv operator(): cleaning up before exit...`——一次干净的退出，不是崩溃，也不是 OOM）。
模型端点没了，依赖它的 e2e 用例于是各等 900 秒。我把当前 `ci` 停掉，用仓库外面那份
`/home/asus/start_vlm_8100.sh`（153 上 8100 的既有启动脚本，注释里写明它就是
llama.cpp + qwen35-q4km）把它起回来，再重跑 `make ci`——就是上面这一次。
**它为什么退出没有定论**（没有 OOM 记录、没有 systemd 记录；8100 那个进程本来也不在
systemd 管理下——`sentrix-vllm.service` 显示 active (exited) 是文档里点名的假活）。

### S9c 适配与收口（T922 / T923 / T925 的一部分，2026-10-05）

**S9 的最后一块，也是 S9 的收口。** 验收原话是「**能对 2.0 跑完一轮评测**」。

#### 两个死端点（T922）

| 端点 | 处置 | 依据 |
|---|---|---|
| `GET {sentrix}/api/arbiter/status` | **删（从未加过）** | 1.0 主 API 里 `arbiter` 零命中；2.0 主 API 也没有这个能力。S9b-1 只建 2.0 真正需要的路由，**2.0 的 PhotoBench 也没有前端**——1.0 那三样（代理函数 / `/api/arbiter-status` 路由 / 前端轮询）在 2.0 一个都没有。用例扫 `app.routes` 与端口方法名钉住「从未加过」 |
| `POST {sentrix}/api/ingest-batches/{id}/cancel` | **删（没有等价物）** | **先核了 2.0 的导入取消语义落在哪**：没有落点。`POST /api/v1/imports` 是**同步**请求（`timeline.py:130-142` 一个请求跑完整条链），没有任务队列、没有 batch 实体。1.0 的 `_cancel_remote_batch`（`:2265`）打的端点从不存在，被 `except Exception` 吞成静默 404 |

两处「连带清理」的表现都是**从未移植**（1.0 的 `unsupported` 兜底分支与前端契约）。
新增两条用例把它变成会红的断言。

#### 换模型 = 重启 2.0（T923，用户裁决③）

1.0 的 `bind-runtime`（`:2611`，"no restart"）在 2.0 只能变成**重启进程**——`vlm_base_url`
启动时读死（`SENTRIX_VLM_BASE_URL`），`ServedModel` 只有模型名带 TTL 重探。

- **新增** `domain/deploy/{__init__,ports}.py`（`SentrixProcess` 端口 + `RestartOutcome`）、
  `application/deploy/{__init__,service}.py`（`SentrixModelSwitch.ensure_endpoint`）、
  `infrastructure/deploy/{__init__,sentrix_process}.py`（`SubprocessSentrixProcess`）。
- **改动** `config.py`（五个 `sentrix_*` 部署参数）、`bootstrap/container.py`、
  `application/run_execution/service.py`（`model_deploy` 的**两个分支**都接上，替换 S9b-2 的占位）。
- **与 S11 的重叠**：按计划书 §1 的要求写进了模块注释；三块（端口 / 应用服务 / 适配器）
  就是 S11 接管的全部边界。**三条部署假设**（同机 `make dev`、pid 归属靠 `lsof`、健康判据
  只看 `/api/v1/health`）也逐条写在注释里。
- **偏离 1.0 的一处**：重启失败**致命**（1.0 非致命）——2.0 的助手用自己的端点答题。

#### photobench 自己的 openapi（T925 的一半）

新增 `scripts/export_photobench_openapi.py` → `packages/contracts/openapi/photobench.yaml`，
挂上 `make check-docs`。文件名没有 `-v1`（前缀仍是 `/api`，T924 未做，见下）。

#### 真机验收（153，对跑着的 9598 与 8100）

夹具：`/tmp/pb-smoke/album-noident/` —— 2 张真照片 + 2 道题（**不进仓库**）。
**没有 `faces`**：身份环为空是**刻意**的，为的是绕过 `POST /api/v1/imports` 在
「先种过身份」的 scope 上必然 500 的那个既有缺陷（§2 第 3 条，不在 S9 范围）。
用 `album-judge`（带 faces）跑的那一次**如实复现了它**：`photo_import` 相位
`HTTP 500 Internal Server Error`，与 S9b-2 记的一字不差。

**结果：`status=completed`、`run_valid=true`，八个相位全 `done`，墙钟 63 s。**

```
model_deploy         done  30.6s   ← 重启 2.0（换模型那一步）
scope_setup          done   0.0s
identity_seed        done   0.0s   seeded 0（夹具无身份环，见上）
photo_import         done  24.2s
pipeline_processing  done   3.0s
qa_eval              done   5.1s
gpu_metrics          done   0.0s
aggregate            done   0.0s
```

**「换模型 = 重启 2.0」被真的走到，而且是这一轮自己走的**：

| 段 | 真跑 / 假件 |
|---|---|
| A. 重启路径（用真的 `SubprocessSentrixProcess`） | **真跑**：9598 从 8100 重启到标记端点 8199；旧 pid 消失、新 pid 在听、`/api/v1/health` 200、**`/proc/<新pid>/environ` 的 `SENTRIX_VLM_BASE_URL = 8199`** |
| B. 一轮评测 | **真跑**：起 PhotoBench 9597 → `POST /api/runs`（model_base_url=8100）→ `model_deploy` 发现 2.0 正指着 8199，**在 run 内把它重启回 8100**（`gamma_sync = {ok:true, restarted:true, endpoint:8100, previous_endpoint:8199, pid:86759}`）→ 导入 / 处理 / 两道题走真模型（8100 的 llama-server）→ 判分也真打 8100 |
> **2026-10-07 补注**：本文里的 `POST /api/runs` 等路径写于当时，PhotoBench 的前缀当日是 `/api`。T924 之后是 **`/api/v1`**。本行保持原样作为当时的记录。

| C. 收尾核对 | **真跑**：9598 最终指着 8100，健康 200 |
| 单元用例 | **全假件**（352 条），不碰网络、不起真进程 |
| judge 打分 | **真跑**（8100 的 llama-server）：`judge_valid_count=2`、`answer_quality_mean=2.0`、`exact_accuracy=1.0` |
| 身份环 / 关系 | **空**（夹具刻意无 faces，见上）——这是这一轮的**假件侧** |
| 证据判分（`BENCH_EVIDENCE_JUDGE`） | **没跑**（默认关） |

**清理**：5 个验收 scope 全删干净——`foreign_key_check` **0 问题**，
`assets 53`、`events 17`、`entities 36`、`person_moments 8` 全部回到基线；删掉 11 个媒体文件。

#### 真机验收抓到两个「假件看不见」的接缝缺陷（已修）

1. **重启时的进程发现会误杀客户端**：`lsof -ti tcp:PORT` 把客户端也列出来，而验收
   驱动脚本的 httpx 连接池里留着到 9598 的连接，于是它被自己触发的重启 SIGTERM 掉
   （静默，没有堆栈）。修法是只停监听进程（`-sTCP:LISTEN`）。
2. **读侧状态缓存不失效**：`ReadRuns._state` 的 LRU 2 从不失效，run 已 `completed`
   而 `GET /api/runs/{id}` 一直回 `running`（验收脚本按 API 轮询，白等十分钟）。
   修法是缓存**只对终态生效**。

两条都有用例钉住（`test_the_port_discovery_only_looks_at_listeners` /
`test_a_running_run_is_not_served_from_a_stale_cache`），详见 `s9-deviations.md` §7.5。

**一次环境事故如实记录**：验收第二轮时 **8100 的 llama-server 已经不在了**（与 S9b-3
那次同一形态）。用仓库外的 `/home/asus/start_vlm_8100.sh` 起回来再跑。

#### 收口

`photobench-module-map.md`（+6 行，改 3 行的依赖列）、`capability-map.jsonl`（+1 条）、
`behaviour-baseline.md`（新增 S9c 一节）、**新建 `docs/refactor/s9-deviations.md`**（S9a–S9c 的偏离汇总）。

#### 门禁

`make ci`（2026-10-05 01:06→01:35），前七段**全绿**：

- `lint` / `format-check`（449 files）/ `typecheck`（mypy strict，275 files）；
- `arch`：`lint-imports` **11 契约全 KEPT**（含 `photobench-isolation`、`photobench-application-purity`）；
  两套可达性检查 0 问题；
- `photobench-check`：可达性 0 / 端口可达性 0 / 模块地图 0；
- `check-docs`：capability-map 0、module-map 0、**两份 openapi 契约都一致**（sentrix + photobench）。

`coverage`：**`2 failed, 2417 passed, 14 skipped`（28 分 42 秒）**，覆盖率 **93.75%**（门槛 85%）。

两条失败**没有一条是 PhotoBench 的**：

| 失败的用例 | 判读 |
|---|---|
| `apps/api/tests/e2e/test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers` | **已知的模型侧 flaky**（S9a / S9b-1 / S9b-2 / S9b-3 四次全量跑红的都是同一条）。**单独重跑仍然红**：`assert "photo_" not in answer`（模型把内部标识符 `photo_1` 写进了答案）。与 S9c 无关 |
| `apps/api/tests/e2e/test_visual_search.py::test_qdrant_ranking_matches_the_exact_scan_on_the_authoritative_store` | **GPU 显存竞争**：onnxruntime 初始化时 `Failed to allocate memory for requested buffer`（人脸检测器与其它 GPU 消费者抢显存）。**单独重跑整个文件全绿** |

因为 pytest 非零退出，`web-check` 没有跑；单独补跑 `make web-check` 通过（`tsc -b --force` /
oxlint / vitest **62 passed** / vite build 82 ms）。

**一次环境处置如实记录**：第一次 `make ci` 慢得反常（30 分钟才 11%），查明
**8102 的 WeMM 嵌入 sidecar 不在了**——检索类测试打不通它、各自等超时。用仓库外的
`/home/asus/Github/Sentrix-Home-Web/scripts/deploy/restart_wemm_sidecar.sh` 起回来
（6 s 就绪）之后重跑，28 分 42 秒跑完、失败从 7 条降到 2 条。**第一次那次跑在废弃的树上，
已作废。**

---

## S9 整体状态（2026-10-05 收口）

| 段 | 交付点 | 状态 |
|---|---|---|
| S9a 运行时提供者与遥测（T901–T904） | 能在 2.0 里查到本机 GPU/模型进程遥测，provider 可换假件 | **完成** |
| S9b-1 服务骨架与 HTTP 层（T911–T914） | 9597 起得来、能列出 run | **完成** |
| S9b-2 run-execution 主链 | 能对跑着的 2.0 跑完「建空间 → 种身份 → 导入 → 处理 → 问答」 | **完成**（真机达成） |
| S9b-3 judge / 遥测聚合 / 写侧 | 一轮评测带着 judge 分数与遥测样本落盘 + 写侧路由 | **完成**（真机达成） |
| S9c 适配与收口（T922 / T923 / T925） | 两个死端点 / 换模型重启 / openapi / 收口 | **完成**（真机达成） |

**S9 收口：设计文档的验收「能对 2.0 跑完一轮评测」已达成**——见 §5 · S9c 的真机验收。
**未纳入本切片的两件**：T924（PhotoBench 自己的前缀，属前端/S10）与
`POST /api/v1/imports` 的既有缺陷（`apps/api` 的产品问题，不在 S9 范围）。

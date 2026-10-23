# 2026-10-15 QA 问题修复与复用评测实施计划

> **执行方式：** 逐任务 TDD（先写失败测试 → 跑红 → 最小实现 → 跑绿 → 提交）。每个提交前跑一次相关测试，全部完成后跑 `make ci`。
>
> **纪律（本项目既有）：** 动工前先核查（本文件的 §1 就是核查结果）；偏离必须有证据；不新增 1.0 没有的设计（新增必须裁决并登记）；计划里的代码块要当作待验证的草稿，落笔时以真实文件为准。

**目标：** 修掉 2026-10-10 那轮 487q 评测暴露的 16 处问题，然后用**复用相册**的方式重跑一轮 487qa，与 1.0 基准对拍。

**架构约束（不可违反）：**
- 四层 DDD：`api → application → domain`，infrastructure 实现 domain 端口。
- **11 条 import-linter 契约**（`pyproject.toml:78-225`），含 `photobench-isolation`（`photobench` 禁 import `sentrix`）。**主 API 与 PhotoBench 是两个包，同一能力要各写一份，不能互相 import。**
- mypy strict 覆盖 `apps/api/src/sentrix` + `apps/photobench/src` + `scripts`。
- `scripts/check_reachability.py` / `check_method_reachability.py`：**新加端口方法必须有生产调用者**，否则报红；`PENDING_METHODS` 里已登记的方法一旦真被调用，**反向报红要求删登记**。
- 契约条数被 `apps/api/tests/unit/test_architecture_rules.py` 的 `CONTRACT_COUNT = 11` 硬断言。

**基准数据（本轮所有改动都要对着它验收）：**

| | 1.0（同模型同题集） | 2.0（本轮） |
|---|---|---|
| run | `20261011-021159-…-c76fab`（full） | `20261010-100127-…-17f740`（full） |
| judge_mean | **0.830** | **0.755** |
| 零分率 | 0.52 | 0.544 |
| qa_eval | 2876s | 6570s |
| 整轮 | 87 min | 172 min |
| `step_budget_exhausted` | 无此概念 | 48 题（均分 0.021） |

---

## §1 核查结论（事实基础，全部带 `文件:行号`）

### 1.1 助手链路（`apps/api`）

| # | 事实 | 位置 |
|---|---|---|
| A | L2 评审先 `state.model_steps += 1`，判不实后**无条件** `return True` → 调用方 `continue` → 下一圈 `can_step()` 为假 → `_stop()` 用占位符覆盖模型原答案 | `run_turn.py:1008-1042`（`+1` 在 `:1021`）、`:846-848`、`:575-583` |
| B | **下游的放行路径已经存在**：`_judge_answer` 里 `if session.l2_unverified:` 打 `l2_soft_release` 标 + `state.finish(ANSWERED_WITH_GAP)` + `return answer` | `run_turn.py:853-880` |
| C | `_one_shot_evidence_reminder` 自己就有 `if not state.can_step(): return None` 闸门 | `run_turn.py:941-945` |
| D | L2 调用判据 `not state.records` **恒为假**（`final_answer` 自己先 `state.record`，`:791` 早于 `:812`） | `run_turn.py:1018`、`:791-812` |
| E | 同一规律**代码里已为 ③ 号提醒修过一次**：`called_tools = [r for r in state.records if r.tool != FINAL_ANSWER_TOOL]` | `run_turn.py:985` |
| F | `_tool_result_shape` 白名单 = `total/has_more/backend/degraded/delivered` + `asset_ids`；`degraded_reason` 被丢、`read_photo_text` 的 5 个 observation 键**零交集** → 159 条 `{}` | `run_turn.py:177-191`（唯一调用点 `:1130`） |
| G | `session`（`_Session`）**没有任何一格能存"上一版答案"** | `run_turn.py:362-385` |
| H | 1.0 的闸门：`if turn.budget.can_model_step(): … continue`，没步就落下去放行原答案 | 1.0 `runtime.py:2897-2911` |
| I | 1.0 的放行打标：`possibly_fabricated: True` + `turn.agent2_trace.quality.l2_soft_release = True` | 1.0 `runtime.py:3064-3076` |
| J | 1.0 另有 `_force_final_once`：预算耗尽时给一次**不计步数**的纯 final 调用 | 1.0 `runtime.py:1590-1623`、调用点 `:2379-2387` / `:3051` |
| K | 1.0 的 `ProfileConfig` **没有 `max_page_calls`**；2.0 有（值 2），唯一执行机构在 `tool_policy.py:143-156` | 1.0 `profile.py:13-22`；2.0 `profile.py:65` |

### 1.2 PhotoBench（`apps/photobench`）

| # | 事实 | 位置 |
|---|---|---|
| L | **没有 `_run_phases`**；相位循环内联在 `execute`，相位表是 `list[tuple[str, Callable]]`，**没有任何按 mode 跳过的机制** | `service.py:300-325` |
| M | `qa_concurrency` 字段存在但 **HTTP 路径从不设置它** → `_resolve_qa_concurrency` 的显式分支恒不成立 | `service.py:170`；`suites.py:203-221` |
| N | 2.0 **没有任何容量探测**：无 `/props`、无 `/stats`、无 `total_slots`；`max_num_seqs` 只在托管分支落盘、从不被解析为槽位 | `service.py:630-641` |
| O | `InferenceProvider.health()` **零生产调用者**，登记在例外表里 | `check_method_reachability.py:158` |
| P | `_phase_qa_eval` 的 Agent / Judge 是**两段串行** `as_completed`（先跑完全部 Agent 再跑全部 Judge） | `service.py:1265-1300` |
| Q | 取消的题：`item = {"error": repr(exc), "failed": True}` → `judge_status = "skipped"` | `service.py:1269-1276` |
| R | `run_valid` 只在两处出现，**读侧从不重算**；取消时那段跑不到 → 永久 false | `service.py:416`、`:1310` |
| S | `progress.completed` 用 `len(items)`（**含 failed**） | `service.py:1284-1285`、`:1239-1240` |
| T | `completed` 口径含 `skipped`（1.0 `:7286` 逐字移植） | `domain/runs/analysis.py:1330-1332` |
| U | `RUN_MODES = ("full",)`；`start_suite` 拒绝其余 mode | `application/runs/suites.py:64`、`:161-163` |
| V | **前端 reuse UI 已完整实现**（复用基座选择器、`existingScopeId`、payload 里已传 `existing_scope_id`） | `apps/photobench/web/src/hooks/useReuseMode.ts`、`App.tsx:213-223` |
| W | `resume` 返回 501 | `api/routes/suites.py:80-87` |
| X | 相位名被测试**逐项断言**，加/减相位会打破它（这是它的意图） | `tests/test_run_execution.py:223-232` |
| Y | `tests/` 无 `conftest.py`，假件在 `fakes_execution.py`（**未预设的调用直接失败，不回落真实现**） | `tests/fakes_execution.py:1-11` |

### 1.3 1.0 的对应实现（移植来源）

| 能力 | 1.0 位置 | 读什么 |
|---|---|---|
| 并发三级来源 | `benchmark_orchestrator.py:3632-3665` | env → `model_deploy.model_state.max_num_seqs` → `_runtime_max_concurrency()` |
| llama.cpp 槽位 | `:3690-3713` | `GET {base 去 /v1}/props` → **`total_slots`** |
| 托管 vLLM 槽位 | `:3715-3748` | `GET {manager_url}/state` → `max_num_seqs` / `max_num_seqs_running` / `max_concurrency` |
| ninfer 槽位 | `:3676-3684` | `GET {stats}/stats` → `capacity.max_concurrency` |
| judge 并发 | `:3833-3841` | env 否则 `min(8, qa_concurrency)` |
| 导入 workers | 1.0 **主 API** `app.py:352-368` | `min(configured, max_num_seqs)`；`platform_profile.py:198-224` 按内存 16GB→2 / 32GB→5 / 48GB+→8 |
| 入 QA 前健康探测 | `:3394-3397` | `_probe_model_health_error()` → 不健康就抛 |
| 中途熔断 | `:4065-4074` | 每题 agent 出错后复探 → `_trip_infrastructure_circuit` |
| `run_valid` 判据 | `:3816-3830` | 无 partial/failed 相位 + 未熔断 + 全 completed + 全 judge completed |
| 进度口径 | `:3433-3451`、`:3536-3538` | `_qa_agent_completed` 只在真拿到 item 时 +1 |
| resume | `:7488`（入口）、`:2718-2727`（相位筛选）、`:3414-3417` + `:3455-3457`（题级跳过） | 跳过 `execution_status not in {"scheduled","running"}` 的题 |

### 1.4 环境事实（跑评测前必须处理）

- 8100 当前跑的是 **vLLM gemma4-12b-it**（`--max-model-len 8192 --max-num-seqs 12`），而基准是 **qwen35-q4km**。`restart_q4km_ctx8192.sh` 可切回（llama.cpp，`--ctx-size 32768 --parallel 4 --metrics`）→ 每槽 8192、4 个并发槽。
- **8102 WeMM 侧车没在跑**。venv 在 `/home/asus/Github/Sentrix-Home-Web/.venv-text/.venv-wemm`，模型在 `/home/asus/models/WeMM-Embedding-2B`（5.1G）。无常驻管理（`deploy/systemd/` 只有 api 与 photobench 两个单元）。启动路径靠 `wemm.py` docstring 里一句话。
- `9598 /health` 只查数据库（`api/routes/health.py:8-11`），侧车全废时仍报 `ok`。
- 内存充裕（60G 总/44G available），dmesg **无 OOM 记录** —— 侧车是被外部杀掉的。
- 2.0 库里已有可复用 scope：`PhotoBench-20261010-161414-…`（362 assets），`GET /api/v1/memory-spaces` 的 `reuse_bases` 已能列出它（`ready: true`）。

---

## §2 修复任务

> 顺序即执行顺序。T1–T2 是助手链路（收益最大、改动最小）；T3–T4 是吞吐；T5 是运维可见性；T6–T7 是口径；T8 是 reuse/resume；T9 是两条裁决项。

### T1 · L2 判不实后放行原答案（F1）

**Files:**
- Modify: `apps/api/src/sentrix/application/assistant/run_turn.py:1008-1042`
- Test: `apps/api/tests/integration/test_run_turn.py`

**为什么只需两行：** 下游的放行路径（`l2_soft_release` 标记 + `state.finish(ANSWERED_WITH_GAP)` + `return answer`）**已经存在于** `_judge_answer:853-880`，只是 `_review_with_l2` 判不实后总是 `return True`，永远走不到那里。

- [ ] **Step 1: 写失败测试**

```python
def test_an_unfaithful_answer_is_released_when_the_judge_spent_the_last_step(env):
    """评审吃掉最后一个模型步时，**放行模型自己那版答案**，不用占位符覆盖。

    1.0 ``runtime.py:2897`` 的 ``if turn.budget.can_model_step()`` 闸门：没步就不回环，
    落下去放行上一版 final 并打 ``possibly_fabricated`` 标。2.0 漏抄这个闸门，
    于是 487q 里 48 题（9.9%）的答案被 ``emergency.fallback_answer`` 换成占位符，
    均分 0.021（其余题 0.891）。
    """
    _conversations, _turns, conversation, _db = env
    # 8 步预算：1 次 search + 6 次翻页 + 1 次 final = 8，L2 评审正好落在第 8 步
    script = [tool_call("search_memories", "c1", query="海边")]
    script += [tool_call("get_result_page", f"p{i}", result_set_id="rs_1", page=i) for i in range(1, 7)]
    script += [final("相册里有 5 张海边的照片。")]
    model = ScriptedModel(script, judge=PlannerScript([judge_reply(("fabrication", "工具里没有 5")).content]))
    run_turn = build(env, model, StubToolbox({"search_memories": SEARCH_OK, "get_result_page": SEARCH_OK}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "相册里有 5 张海边的照片。"          # 不是占位符
    assert result.termination_reason is TerminationReason.ANSWERED_WITH_GAP
    released = [e for e in _turns.events_after(turn_id, 0)
                if e.payload.get("code") == "l2_soft_release"]
    assert len(released) == 1
    assert released[0].payload["possibly_fabricated"] is True
```

- [ ] **Step 2: 跑测试确认它失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_run_turn.py::test_an_unfaithful_answer_is_released_when_the_judge_spent_the_last_step -v`
Expected: FAIL —— `result.answer` 是 `这一轮检索到 …（本轮因「模型步数用尽」提前结束。）`

- [ ] **Step 3: 最小实现**

在 `_review_with_l2` 的 `session.pending_reminder = verdict.reminder` 与 `return True` 之间插入：

```python
        session.pending_reminder = verdict.reminder
        # **评审自己吃掉了一个模型步**（上面 ``state.model_steps += 1``）。若那正好是
        # 最后一步，「回环重答」永远不会发生——调用方 ``continue`` 之后立刻撞上
        # ``can_step()`` 为假，答案被 ``_stop`` 的占位符覆盖。
        #
        # 1.0 ``runtime.py:2897`` 的 ``if turn.budget.can_model_step():`` 就是这道闸门：
        # **没步就不回环**，落回收尾路径放行上一版答案，并在 ``_judge_answer`` 里打
        # ``l2_soft_release`` / ``possibly_fabricated`` 标。2.0 抄了「评审计一步」的
        # 成本口径，漏抄了这个配套闸门。
        #
        # 与 ``_one_shot_evidence_reminder:941`` 的闸门是同一条理由——那句注释写着
        # 「没有模型步了，提醒只会换来一次『预算用尽』的收尾」，L2 这条路当时没跟上。
        if not state.can_step():
            return False
        return True
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_run_turn.py -v`
Expected: 新用例 PASS，**既有 1364 行全部用例仍 PASS**（尤其 `test_an_unfaithful_answer_is_pushed_back_once_then_released`）。

- [ ] **Step 5: 提交**

```bash
git add apps/api/src/sentrix/application/assistant/run_turn.py apps/api/tests/integration/test_run_turn.py
git commit -m "fix(assistant): L2 判不实后无步可走时放行原答案，不用占位符覆盖

1.0 runtime.py:2897 的 can_model_step 闸门在 2.0 漏抄。487q 实测 48 题
（9.9%）的答案被 emergency.fallback_answer 换成占位符，均分 0.021。"
```

---

### T2 · L2 调用条件改为「有成功的工具观察」（F2）

**Files:**
- Modify: `apps/api/src/sentrix/application/assistant/run_turn.py:1018`
- Test: `apps/api/tests/integration/test_run_turn.py`

- [ ] **Step 1: 写失败测试**

```python
def test_the_judge_is_not_called_when_no_tool_ever_returned(env):
    """一次工具都没成功过的回合不该送 L2 评审。

    ``state.records`` 包含 ``final_answer`` 自己的记录（``_finish_with_final_answer``
    先 record 后 judge），所以 ``not state.records`` 恒为假 —— 判据等于「每轮都评审」。
    1.0 ``runtime.py:2865`` 用的是 ``task.tool_results``（真实工具观察）。
    """
    _conversations, _turns, conversation, _db = env
    model = ScriptedModel([final("你好呀。")], judge=RuntimeError("不该被调用"))
    run_turn = build(env, model, StubToolbox({}))
    turn_id = start(run_turn, conversation)

    result = run_turn.run(turn_id)

    assert result.answer == "你好呀。"
```

（`judge=RuntimeError(...)` 是既有体例，见 `test_the_judge_is_not_called_without_tool_results`。若那条用例已覆盖本场景，则本步骤改为**加一条钉「工具全部被拒」的用例**，因为 1.0 要的是「成功的观察」。）

- [ ] **Step 2: 跑测试确认它失败**

Run: `.venv/bin/pytest apps/api/tests/integration/test_run_turn.py::test_the_judge_is_not_called_when_no_tool_ever_returned -v`
Expected: FAIL —— `RuntimeError: 不该被调用`

- [ ] **Step 3: 最小实现**

把 `:1018` 改成：

```python
        # 「有工具观察」要排掉两样东西：``final_answer`` 自己（它先 ``state.record``
        # 才走到这里，直接看 ``not state.records`` 会恒为假——``_one_shot_evidence_
        # reminder:985`` 已经为同一条规律修过一次），以及**没成功的**调用（被拒 /
        # 参数错 / 回放都不产出观察）。1.0 的判据是 ``task.tool_results``，那是
        # 「执行成功并记下的观察」。
        observations = [
            record
            for record in state.records
            if record.tool != FINAL_ANSWER_TOOL and record.status == "ok"
        ]
        if session.judged or not observations or not state.can_step():
            return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `.venv/bin/pytest apps/api/tests/integration/test_run_turn.py apps/api/tests/unit/test_assistant_judge.py -v`
Expected: 全绿。

- [ ] **Step 5: 提交**

```bash
git commit -am "fix(assistant): L2 评审的进门判据改成「有成功的工具观察」

not state.records 恒为假（final_answer 自己先 record），等价于每轮都评审，
连一次工具都没成功过的回合也送审——评审判不实率因此被抬高。"
```

---

### T3 · 端点容量探测：并发（F5）

**Files:**
- Modify: `apps/photobench/src/photobench/domain/runtime/ports.py`（加端口方法）
- Modify: `apps/photobench/src/photobench/infrastructure/runtime/providers.py`（实现）
- Modify: `apps/photobench/src/photobench/application/run_execution/service.py:1179-1206`（消费）
- Modify: `scripts/check_method_reachability.py`（T3 接线后删掉 `InferenceProvider.health` 那条登记——如果本次顺手接上）
- Test: `apps/photobench/tests/fakes_execution.py`（`FakeInference` 加容量方法）+ `apps/photobench/tests/test_run_execution.py`

**设计（各取所需，不越包）：** PhotoBench 只缺**并发槽位**，主 API 只缺**窗口**。两个包不能互相 import（`photobench-isolation`），所以各写一份职责单一的探测，不做通用抽象。

- [ ] **Step 1: 加端口方法 + 假件**

`domain/runtime/ports.py` 的 `InferenceProvider`：

```python
    def max_concurrency(self) -> int | None:
        """这个推理端点**同时能服务几个请求**。读不到返回 ``None``。

        它与 ``health`` 是两件事：健康只回答「能不能用」，这一格回答「能用几个」。
        1.0 把它当作 QA 并发的**唯一诚实来源**（``benchmark_orchestrator.py:3667``），
        2.0 因为缺这一格把并发硬编码成 1，实测 qa_eval 6570s vs 1.0 的 2876s。
        """
```

- [ ] **Step 2: 写失败测试**

在 `apps/photobench/tests/test_run_execution.py` 加：

```python
def test_qa_concurrency_follows_the_endpoint_slots():
    """并发跟随端点自报的槽位，而不是恒定 1。

    1.0 ``benchmark_orchestrator.py:3632-3690``：env → deploy 快照的 max_num_seqs
    → 实时问引擎（llama.cpp ``/props`` 的 ``total_slots``）。
    """
    execution, *_ = _execution(inference=FakeInference(max_concurrency=4))
    state = execution.execute(_request())
    assert state["qa_concurrency"] == 4
    assert state["judge_concurrency"] == 4          # min(8, 4)
    assert state["qa_concurrency_requested"] == 4


def test_qa_concurrency_falls_back_to_one_when_the_endpoint_says_nothing():
    """问不到就退回 1（串行）——这是 1.0 ``:3662`` 的最后一级，不是 2.0 的默认值。"""
    execution, *_ = _execution(inference=FakeInference(max_concurrency=None))
    state = execution.execute(_request())
    assert state["qa_concurrency"] == 1


def test_qa_concurrency_never_exceeds_the_endpoint_limit():
    """请求值被端点实际容量夹住（1.0 ``:3665`` 的 ``min(requested, runtime_limit)``）。"""
    execution, *_ = _execution(inference=FakeInference(max_concurrency=2),
                               env={"PHOTOBENCH_QA_CONCURRENCY": "8"})
    state = execution.execute(_request())
    assert state["qa_concurrency"] == 2
    assert state["qa_concurrency_requested"] == 8
    assert state["qa_concurrency_runtime_limit"] == 2
```

- [ ] **Step 3: 跑测试确认失败**

Run: `.venv/bin/pytest apps/photobench/tests/test_run_execution.py -k qa_concurrency -v`
Expected: FAIL —— `FakeInference` 没有 `max_concurrency`；`state["qa_concurrency"] == 1`

- [ ] **Step 4: 实现探测（infrastructure）**

`infrastructure/runtime/providers.py` 的 `OpenAICompatibleInferenceProvider` 加：

```python
    def max_concurrency(self) -> int | None:
        """1.0 ``benchmark_orchestrator.py:3690-3713`` 的 ``_llamacpp_max_concurrency``。

        llama-server 在 ``GET /props`` 里自报 ``total_slots``（= 启动时的
        ``--parallel N``）。**这是外部端点唯一问得到的容量**——没有 manager 时
        （153 的形态，``service.py:574``）manager 侧什么都不知道。
        """
        root = self._base_url[:-3] if self._base_url.endswith("/v1") else self._base_url
        payload = self._get_json(f"{root}/props", timeout=3)
        if not isinstance(payload, dict):
            return None
        try:
            slots = int(payload.get("total_slots"))
        except (TypeError, ValueError):
            return None
        return slots if slots > 0 else None
```

（`_get_json` 用既有的 httpx 客户端；异常一律吞成 `None` —— **这是可选依赖，抛出去会让每次查询都挂在这上面**，与 `wemm.py` 同一条纪律。）

托管 vLLM 那一支同形：`GET {manager_url}/state` → `max_num_seqs` / `max_num_seqs_running` / `max_concurrency`（首个 > 0）。

- [ ] **Step 5: 实现消费（application）**

`service.py` 的 `_resolve_qa_concurrency` 按 1.0 三级顺序重写：

```python
    def _resolve_qa_concurrency(self, request: RunRequest) -> int:
        """1.0 ``:3632-3665``：env → deploy 快照的 ``max_num_seqs`` → **实时问引擎**。

        2.0 原先只实现了第一级、其余直接退回 1。这一格不是「保守」，是缺能力：
        1.0 的默认值本身就是从被测模型那儿读来的。
        """
        runtime_limit = self._runtime_max_concurrency(request)
        requested = self._requested_concurrency(request)
        if requested is None:
            requested = self._snapshot_concurrency() or runtime_limit or 1
        state_holder = ...  # 由调用方写入；见下
        return min(requested, runtime_limit) if runtime_limit is not None else requested
```

并把 `qa_concurrency_requested` / `qa_concurrency_runtime_limit` / `qa_concurrency` / `judge_concurrency` 四个键写进 state（1.0 的四个键名逐字照搬）。

- [ ] **Step 6: 跑测试确认通过**

Run: `.venv/bin/pytest apps/photobench/tests/ -v`
Expected: 全绿；既有相位顺序断言不受影响（本任务不加相位）。

- [ ] **Step 7: 提交**

```bash
git commit -am "feat(photobench): QA 并发从端点自报槽位推导，不再硬编码 1

1.0 benchmark_orchestrator.py:3632-3690 的三级来源：env → deploy 快照
→ llama.cpp /props 的 total_slots。2.0 只实现了第一级。"
```

---

### T4 · Agent 与 Judge 流水线化（F5 续，P 条）

**Files:**
- Modify: `apps/photobench/src/photobench/application/run_execution/service.py:1257-1303`
- Test: `apps/photobench/tests/test_run_execution.py`

**为什么：** 2.0 是两段串行（跑完全部 Agent 再跑全部 Judge），1.0 是单循环流水线（`while pending` + `FIRST_COMPLETED`，Agent 一完成立刻提交 Judge）。这是 qa_eval 6570s vs 2876s 的另一半成因。

- [ ] **Step 1: 写失败测试**

```python
def test_the_judge_pool_starts_while_the_agent_pool_is_still_running():
    """判分不排在全部答题之后——Agent 一完成就立刻进 Judge 池。

    两段串行（先跑完全部 Agent 再跑全部 Judge）是 2.0 相对 1.0 的退化：
    1.0 是单循环 ``FIRST_COMPLETED`` 流水线（``benchmark_orchestrator.py:3501-3587``）。
    """
    # 用一个"Agent 只有在 Judge 已经开始后才允许完成"的闸门假件：
    # 若是两段串行，这个 run 会死锁/超时；流水线化后正常完成。
```

（具体闸门写法照 `test_suites.py:262-272` 用 `threading.Event` 的体例。）

- [ ] **Step 2: 跑测试确认失败** → 预期超时或断言失败。
- [ ] **Step 3: 改成单循环**（`as_completed` 合并两个池，Agent 完成即刻 `judge_pool.submit`）。
- [ ] **Step 4: 跑测试确认通过**
- [ ] **Step 5: 提交**

```bash
git commit -am "perf(photobench): 判分与答题流水线化，不再两段串行"
```

---

### T5 · WeMM 侧车常驻 + 健康可见（F14）

**Files:**
- Create: `deploy/systemd/sentrix-wemm-sidecar.service`
- Modify: `Makefile`（`install-services` 的拷贝清单）
- Modify: `apps/api/src/sentrix/api/routes/health.py`
- Test: `apps/api/tests/contract/test_health_route.py`（或既有健康路由测试）

- [ ] **Step 1: 单元文件**

```ini
# WeMM-Embedding-2B 嵌入侧车（端口 8102）。**它不在 2.0 的管理范围内**
# （设计 §2.1「不管理模型进程生命周期」）——这里只是把「谁来拉起它」从
# 一句 docstring 变成 systemd，因为 2026-10-08 与 10-10 两次它自己消失，
# 而整轮评测 354/370 次检索都跑在降级状态下、没有任何一处报错。
#
# venv 与模型都不属于本仓库（侧车脚本在 1.0 仓库，WeMM 要 transformers==5.2.0，
# 与 2.0 的 venv 不兼容），所以这里写绝对路径。
[Unit]
Description=Sentrix WeMM embedding sidecar (port 8102)
After=network.target

[Service]
Type=simple
WorkingDirectory=%h/Github/Sentrix-Home-Web
# PYTORCH_CUDA_ALLOC_CONF 不是可选调优：不设时缓存分配器会攥住约 4.3GB 不放，
# 而那部分显存对同卡上的 8100 是不可见的（见 apps/api/.../models/wemm.py）。
Environment=PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
ExecStart=%h/Github/Sentrix-Home-Web/.venv-text/.venv-wemm/bin/python scripts/maintenance/wemm_embedder_sidecar.py --port 8102
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

- [ ] **Step 2: 健康并入**

`api/routes/health.py` 改成带 `embeddings` 一格：

```python
@router.get("/health")
def health(request: Request) -> dict[str, str]:
    """``status`` 的语义：``ok`` / ``degraded`` / **``impaired``**。

    **为什么要有第三档。** 2026-10-10 那轮评测里，视觉嵌入侧车全程不在，
    354/370 次检索跑在 ``degraded=true`` 下，而本端点一直回 ``{"status":"ok"}``
    —— 降级因此**静默**，整轮数据作废却没人知道。数据库好、嵌入器坏，
    不是 ``ok``。
    """
```

- [ ] **Step 3–5: 测试、跑绿、提交**

```bash
git commit -am "feat(ops): WeMM 侧车进 systemd；嵌入器健康并入 /health

侧车两次自行消失且静默降级（354/370 次检索）。/health 只查数据库，
所以一直报 ok。"
```

---

### T6 · 评测守卫与口径（F9–F12）

**Files:**
- Modify: `apps/photobench/src/photobench/application/run_execution/service.py`（守卫 + 进度 + 取消标态 + run_valid）
- Modify: `apps/photobench/src/photobench/domain/runs/analysis.py:1330-1332`（completed 口径）
- Test: `apps/photobench/tests/test_run_execution.py`、`test_aggregation.py`

四个子项，各自一个提交：

- [ ] **6a · 入 QA 前健康守卫**：`_phase_qa_eval` 开头探一次端点（照 1.0 `:3394-3397`），不健康就 `phase_partial` 并写明原因 —— **不抛**（2.0 的 qa_eval 已经跑过导入，抛出去会把整轮变成 failed）。
- [ ] **6b · 进度口径**：`record_progress` 改用**成功计数**（`sum(1 for i in items.values() if i.get("execution_status") == "completed")`），`completed` 与 `agent_completed` 都不含 failed。测试：取消场景下 `progress.completed < total`。
- [ ] **6c · 取消的题标 `scheduled`**：`:1269` 的 `except` 里先捕 `RunCancelledError`，标 `execution_status="scheduled"`、`error="cancelled while waiting"`。**这是 T8 resume 能救回那 155 题的前提**（1.0 的跳过判据是 `not in {"scheduled","running"}`）。
- [ ] **6d · `completed` 口径 + `run_valid`**：`analysis.py:1330` 改成按 `execution_status == "completed"` 算；`run_valid` 改成读侧重算（1.0 `:3816-3830` 的五条判据）。

---

### T7 · 轨迹投影与导入并发（F3 + F6）

**Files:**
- Modify: `apps/api/src/sentrix/application/assistant/run_turn.py:177-191`
- Modify: `apps/api/src/sentrix/config.py:73` + `application/media/import_album.py:74`
- Test: 两侧各加

- [ ] **7a · `_tool_result_shape` 保留全量 observation**

原白名单的注释写着「规模不是结果本身」，但**它的后果是可测的**：159 条 `read_photo_text` 轨迹是空对象、`degraded_reason` 丢失导致本轮降级原因无法从 run.json 定位。轨迹的用途恰恰是复盘「模型拿到了什么」。改成 `return dict(observation)`，并把 docstring 改写成变更理由。测试：钉 `read_photo_text` 的 5 个键、`search_memories` 的 `degraded_reason`。

- [ ] **7b · `album_workers` 由端点容量推导**

1.0 是 `min(configured, max_num_seqs)`（主 API `app.py:352-368`）。2.0 写死 4。**本轮 4 恰好匹配**（`--parallel 4`），但换 12 槽端点不会自动提速。照 1.0 口径实现，并在注释里登记「153 的 4 是巧合」。

---

### T8 · reuse 模式与 resume（F7 + F8）

**Files:**
- Modify: `apps/photobench/src/photobench/application/runs/suites.py`（`RUN_MODES`、`start_suite`）
- Modify: `apps/photobench/src/photobench/application/run_execution/service.py`（`RunRequest.mode`、相位表按 mode 组装、`scope_source`、`scope_reused_from_runs`）
- Modify: `apps/photobench/src/photobench/api/routes/suites.py:80-87`（resume 不再 501）
- Test: `apps/photobench/tests/test_suites.py`、`test_run_execution.py`

**目标形状（1.0 `20261011-111752-…-reuse-2a3103` 实测）：**

```
mode: "reuse"          scope_source: "reused"
existing_scope_id: "album_328346bc165e"
scope_reused_from_runs: ["20261011-021159-album3-max-video10-qwen35-q4km-c76fab"]
phases: {scope_attach, qa_eval, aggregate}
qa_concurrency_requested / runtime_limit / qa_concurrency / judge_concurrency 四个键
```

- [ ] **8a · reuse**：`RUN_MODES = ("full", "reuse")`；`start_suite` 解析 `existing_scope_id`（reuse 时必填，缺失就 400）；`RunRequest` 加 `mode`；`execute` 的相位表按 mode 过滤（reuse 跳过 `identity_seed`/`photo_import`/`pipeline_processing`）；写 `scope_source="reused"`。前端**已经就绪**，不用改。
- [ ] **8b · 前端 `selectedQa` 缺陷**：`useReuseMode.ts:183-190` 对数组用了 `Object.keys` → 选复用基座后 `selectedQa` 被写成 `"0"`。**这是已在注释里登记的已知缺陷，但它会直接挡住本轮评测**，必须修（`Array.isArray` 分支）。
- [ ] **8c · resume**：照 1.0 `:7488` / `:2718-2727` / `:3414-3417` 移植；题级跳过用 `execution_status not in {"scheduled","running"}`（依赖 T6c 的标态）。

---

### T9 · 两条裁决项（F15 + F16）

- [ ] **9a · `viewer_escape` 的裸词 `他人`**：正则与 1.0 **逐字相同**（`final_guard.py:174`），所以这是继承的固有缺陷、不是抄错，**按规范报给用户裁决**。本轮先只做一件事：把 `他人` 收紧成有语境的模式（`他人(?:的)?(?:相册|照片|隐私|内容)`），并加一条反向用例钉住「家人合影里出现『与他人』不触发」。**改之前先跑一轮统计确认 10-10 那 5 题的原文到底命中了哪个词**（run.json 里当时没有 evidence，需要从 `runtime_turns[].trace` 的模型输出里找）。
- [ ] **9b · `max_page_calls=2`**：1.0 没有这一项。**建议保留**（它是 4B 弱模型的必要护栏：实测 43 次「翻页额度已经用完」，删掉只会让模型继续翻、同样烧步数），但**按「不新增 1.0 没有的设计」这条规范登记为有意的偏离**，写进 `behaviour-baseline.md` 的偏离表并说明代价。

---

## §3 登记与验收

- [ ] `docs/refactor/2026-10-15-qa-fixes-deviations.md`：D1（`max_page_calls`）、D2（`completed` 口径修正 —— 1.0 同错）、D3（`/health` 加 `impaired` 档，1.0 无此档）、D4（reuse/resume 恢复：1.0 有、2.0 曾删）。
- [ ] `docs/refactor/behaviour-baseline.md`：新增一节 `## 2026-10-15：QA 修复（本轮）`，两张表（新增行为 / 偏离）。
- [ ] `docs/refactor/capability-map.jsonl`：`reuse` / `resume` / 并发容量探测三条 `migrated` 记录。
- [ ] `docs/architecture/module-map.md` 与 `photobench-module-map.md`：若新增 `.py` 文件或改包内 import，必须同步（`check_module_map.py` 双向校验）。
- [ ] `docs/refactor/2026-10-05-s10-implementation-plan.md` 或新计划：记录本轮。

**门禁：**

```bash
make ci            # lint format-check typecheck arch photobench-check check-docs coverage web-check photobench-web-check
```

> ⚠️ **已知门禁事实**：`test_assistant_turn.py::test_the_answer_never_leaks_internal_identifiers` 是模型侧 flaky，会让 `ci` 在 `coverage` 一步停住、**后面的 `web-check` 不跑**。所以跑完 `ci` 要**单独补跑** `make web-check` 与 `make photobench-web-check`。
> ⚠️ `tsc -b` 已改成 `--force`（新加源文件不做类型检查的盲区），但**新加的前端文件仍要确认它被 `include` 覆盖**。

---

## §4 端到端重跑（T10）

- [ ] **Step 1: 释放显存并切模型**

```bash
# 1.0 的回退线这次要停（用户已授权）：8091 / 8771 是 1.0，它们不占 GPU，
# 但 8100 上的 gemma4-12b-it（vLLM，gpu-memory-utilization 0.7 ≈ 17GB）要腾出来。
sudo systemctl stop sentrix-api.service sentrix-vllm.service   # 1.0 的两个单元
bash /home/asus/restart_q4km_ctx8192.sh                        # 切回 qwen35-q4km（8192/槽 × 4 槽）
```

- [ ] **Step 2: 拉起侧车并确认**

```bash
systemctl --user daemon-reload
systemctl --user enable --now sentrix-wemm-sidecar.service
curl -s http://127.0.0.1:8102/health          # 侧车自己的健康
curl -s http://127.0.0.1:9598/api/v1/health   # 必须是 ok（不是 impaired）
curl -s http://127.0.0.1:8100/props | python3 -m json.tool | head   # 确认 total_slots=4、n_ctx
```

- [ ] **Step 3: 重启 2.0 两个服务**

```bash
sudo systemctl --user restart sentrix-api.service sentrix-photobench.service
```

- [ ] **Step 4: 发起复用相册的 487q 评测**

```bash
curl -s -X POST http://127.0.0.1:9597/api/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{
    "album_id": "album3-max-video10",
    "qa_set": "mixed-image-video-487q",
    "mode": "reuse",
    "existing_scope_id": "PhotoBench-20261010-161414-album3-max-video10-qwen35-q4km",
    "models": ["qwen35-q4km"],
    "sentrix_url": "http://192.168.0.153:9598",
    "model_base_url": "http://127.0.0.1:8100/v1",
    "judge_url": "https://ark.cn-beijing.volces.com/api/plan/v3",
    "judge_model": "doubao-seed-2.0-lite",
    "judge_provider_id": "volcengine-doubao-seed-2.0-lite"
  }'
```

预期响应里 `mode: "reuse"`、`existing_scope_id` 非空。

- [ ] **Step 5: 盯盘**

```bash
bash /home/asus/memwatch_487q.sh <run_id>       # 内存看门狗（既有脚本）
# 相位进展 / 并发数
curl -s "http://127.0.0.1:9597/api/v1/runs/<run_id>" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['status'], d['phases']['qa_eval']['progress'])"
```

预期：`qa_concurrency: 4`、`judge_concurrency: 4`、`progress.completed` **不含 failed**。

- [ ] **Step 6: 对拍验收**

```bash
python3 /tmp/cmp_runs.py        # §1 那张基准表的重算
```

**验收线（四条都要满足）：**

| 指标 | 目标 | 依据 |
|---|---|---|
| `judge_mean` | **≥ 0.82** | 1.0 基准 0.830；当前 0.755。T1 的反事实估算 → 0.825 |
| `step_budget_exhausted` | **< 10 题** | 当前 48 题；T1 修好后这类只剩"真·预算耗尽" |
| qa_eval 耗时 | **≤ 3200s** | 1.0 是 2876s；当前 6570s。T3+T4 修好后应接近 1.0 |
| 整轮状态 | **`completed`**（不是 `cancelled`） | 前两轮都跑不完 |

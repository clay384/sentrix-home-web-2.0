"""judge 域用例：纯函数（``domain/runs/judge.py``）+ 判分编排（``application/run_execution/judge.py``）。

**这一份钉的是打分的口径**，不是「跑得起来」：发出的 payload（模型 / 温度 / 提示词 /
rubric）、从散文里抠 JSON、分数—理由一致性重问、4xx 不重试、一个 item 的多轮编排与
终态。**判分失败绝不产出 0 分**（0 是合法分数）——这条口径在多处被单独钉住。

用例不打网络：``FakeHttpClient`` 按队列作答，队列空了就失败（见假件的 docstring）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fakes_execution import FakeHttpClient, judge_reply

from photobench.application.run_execution.judge import JudgeRunner
from photobench.domain.runs.execution import RunCancelledError
from photobench.domain.runs.judge import (
    ANSWER_QUALITY_RUBRICS,
    JUDGE_PROMPT,
    execution_failure,
    judge_chat_url,
    parse_judge_json,
    thinking_kwargs,
)


@pytest.fixture(autouse=True)
def _fast_judge(monkeypatch: pytest.MonkeyPatch):
    """限流与退避在用例里必须是零成本——它们与判分口径无关。

    **改的是被测模块的常量而不是环境变量**：那些常量在 import 时读环境，
    ``monkeypatch.setenv`` 在这里已经晚了。
    """
    import photobench.application.run_execution.judge as judge_module

    monkeypatch.setattr(judge_module, "JUDGE_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(judge_module, "JUDGE_RETRY_BACKOFF_SECONDS", 0.001)
    monkeypatch.setattr(judge_module, "JUDGE_RETRY_BACKOFF_MAX_SECONDS", 0.002)
    # 2 次而不是默认的 6 次：既保留「重试」这条路径（取消感知与失败收敛都依赖它），
    # 又不让一个必然失败的用例把假件的响应队列掏空成 6 次 AssertionError。
    monkeypatch.setattr(judge_module, "JUDGE_RETRY_ATTEMPTS", 2)
    yield


def _runner(http: FakeHttpClient, **overrides: Any) -> JudgeRunner:
    base: dict[str, Any] = {
        "http": http,
        "judge_url": "http://judge:9999/v1",
        "judge_model": "judge-model",
        "sleep": lambda _seconds: None,
        "rand": lambda _low, _high: 0.0,
    }
    base.update(overrides)
    return JudgeRunner(**base)


# ---- 纯函数 ---------------------------------------------------------------


def test_judge_chat_url_completes_the_endpoint_in_three_shapes():
    """判分端点的补全规则（1.0 ``:4425``）。

    **三态都断言**：只测 ``/v1`` 那一支的实现，对「已经在 ``/chat/completions`` 上」
    与「裸主机」两种输入都会拼出错的 URL，而那种错误在真机上只会表现为一次 404。
    """
    assert judge_chat_url("http://j/v1") == "http://j/v1/chat/completions"
    assert judge_chat_url("http://j/v3/") == "http://j/v3/chat/completions"
    assert judge_chat_url("http://j/chat/completions") == "http://j/chat/completions"
    assert judge_chat_url("http://j:9999") == "http://j:9999/v1/chat/completions"


def test_parse_judge_json_takes_the_outermost_object():
    """1.0 ``:4417``：模型经常在 JSON 外面裹一句「好的，我的评分是：…」。"""
    assert parse_judge_json('好的：{"score":2,"reason":"对"} ') == {"score": 2, "reason": "对"}
    assert parse_judge_json("没有 JSON") == {}
    assert parse_judge_json("坏 { ") == {}


def test_execution_failure_reads_both_status_and_termination_reason():
    """1.0 ``:1488``。**两个来源都要看**：只看 ``agent_status`` 会漏掉
    ``termination_reason`` 里的 ``budget_limit``／``parse_failure`` 那几类。"""
    assert execution_failure("partial", "answered") is True
    assert execution_failure("complete", "step_budget_limit") is True
    assert execution_failure("complete", "answered") is False


def test_thinking_is_disabled_differently_for_volces_and_local():
    """1.0 ``:231``：豆包用 ``thinking``，其余用 ``enable_thinking``。"""
    assert thinking_kwargs("https://ark.cn-beijing.volces.com/api/v3") == {"thinking": {"type": "disabled"}}
    assert thinking_kwargs("http://127.0.0.1:8100/v1") == {"enable_thinking": False}


# ---- 回答质量判分 ---------------------------------------------------------


def test_the_quality_payload_carries_the_model_rubric_and_gt_fields():
    """判分请求的形状：模型 / 温度 0 / system 是提示词 / user 里带 rubric 与 GT。

    **区分力**：一个把 rubric 漏掉的实现同样会返回一个分数——只有把 rubric 的
    特征串与 GT 字段断出来，才能把「按题打分」与「让模型自由发挥」分开。
    """
    http = FakeHttpClient([judge_reply('{"score":2,"reason":"覆盖了核心信息"}')])
    runner = _runner(http, answer_quality_prompt="自定义系统提示")
    result = runner.judge(
        "在海边的照片有哪些？",
        "a.jpg",
        "海边的是 a.jpg",
        expected_action="answer",
        task_type="retrieval",
        question_type="fact",
        answerability="answerable",
    )
    assert result["score"] == 2
    assert result["judge_status"] == "completed"
    payload = http.payloads()[0]
    assert payload["model"] == "judge-model"
    assert payload["temperature"] == 0
    assert payload["messages"][0] == {"role": "system", "content": "自定义系统提示"}
    user = payload["messages"][1]["content"]
    assert ANSWER_QUALITY_RUBRICS["answer"] in user
    assert "在海边的照片有哪些？" in user
    assert "retrieval" in user  # 任务类型 GT 被带进 user 段


def test_an_inconsistent_zero_asks_once_more_and_accepts_the_second_answer():
    """1.0 的分数—理由一致性保护（``:4272``）。

    **这条用例专门针对「理由说对、分数给 0」**：不重问的实现会把一个正确的回答记成
    0 分——0 是合法分数，读侧分不出来。第二条响应必须真的被用上（分数变成 2）。
    """
    http = FakeHttpClient(
        [
            judge_reply('{"score":0,"reason":"回答正确，与参考答案一致"}'),
            judge_reply('{"score":2,"reason":"覆盖了核心信息"}'),
        ]
    )
    result = _runner(http).judge("q", "ref", "answer", expected_action="answer")
    assert result["score"] == 2
    assert len(http.calls) == 2
    # 第二次请求的 system 里带上了那段一致性提示（否则它只是重发一遍同样的请求）。
    second_system = http.payloads()[1]["messages"][0]["content"]
    assert "一致性保护" in second_system


def test_a_retry_that_stays_inconsistent_yields_no_score_not_zero():
    """仍然矛盾时**不给分**——给 0 会把一次判分失败记成一次「答得很差」。"""
    http = FakeHttpClient(
        [
            judge_reply('{"score":0,"reason":"回答正确"}'),
            judge_reply('{"score":0,"reason":"回答正确"}'),
        ]
    )
    result = _runner(http).judge("q", "ref", "answer", expected_action="answer")
    assert result["score"] is None
    assert result["reason"] == "judge_inconsistent_score_reason"


def test_an_answerable_question_does_not_turn_a_valid_judge_zero_into_a_missing_score():
    http = FakeHttpClient(
        [judge_reply('{"score":0,"reason":"该问题可回答，正确地点已知。模型未给出正确答案，因此得0分。"}')]
    )
    result = _runner(http).judge("q", "ref", "answer")
    assert result["score"] == 0
    assert result["consistency_version"] == 3
    assert len(http.calls) == 1


def test_a_client_error_is_not_retried():
    """4xx 是请求本身的问题，重试改变不了任何事（1.0 ``:4445`` 的实测代价：195 秒/题）。

    **区分力**：一个「什么都重试」的实现会把这个用例跑成 6 次调用；断调用次数才是
    真的判据，只断言「抛了 RuntimeError」在两种实现下都成立。
    """
    # 复刻 ``HttpxJsonClient`` 的失败形状：RuntimeError 包住带 ``response`` 的原异常。
    cause = Exception("unauthorized")
    cause.response = SimpleNamespace(status_code=401)  # type: ignore[attr-defined]
    error = RuntimeError("HTTP 401 Unauthorized: bad key")
    error.__cause__ = cause
    http = FakeHttpClient([error])
    with pytest.raises(RuntimeError) as excinfo:
        _runner(http)._request({"model": "m"})
    assert "not retryable" in str(excinfo.value)
    assert len(http.calls) == 1


def test_the_judge_method_turns_a_client_error_into_a_failed_verdict():
    """``judge`` 把一切失败收成 ``judge_status: failed``（1.0 ``:4312``）**不抛**。

    这条与上一条是同一件事的两层：``_request`` 抛，``judge`` 收。**判分失败不是 0 分**
    ——``score`` 必须是 ``None``。
    """
    cause = Exception("unauthorized")
    cause.response = SimpleNamespace(status_code=401)  # type: ignore[attr-defined]
    error = RuntimeError("HTTP 401 Unauthorized")
    error.__cause__ = cause
    http = FakeHttpClient([error])
    result = _runner(http).judge("q", "ref", "answer", expected_action="answer")
    assert result["judge_status"] == "failed"
    assert result["score"] is None
    assert "not retryable" in result["reason"]


def test_a_cancelled_runner_stops_the_retry_wait():
    """取消要能打断退避（1.0 ``self._cancel.wait``）——最长 60 秒的等待不可被忽略。

    断言打在 ``_request`` 上而不是 ``judge`` 上：``judge`` 会把 ``RunCancelledError``
    也收成一条 failed verdict（1.0 的 ``except Exception`` 就是这个行为），
    **取消信号本身不失真**，失真的只是那一道 judged 结果的形状。
    """
    import threading

    cancel = threading.Event()
    cancel.set()
    http = FakeHttpClient([RuntimeError("connection refused"), RuntimeError("connection refused")])
    runner = _runner(http, cancel=cancel)
    with pytest.raises(RunCancelledError):
        runner._request({"model": "m"})
    assert len(http.calls) == 0
    assert runner.request_metrics()["requests_started"] == 0


# ---- 任务行为与证据判分 ---------------------------------------------------


def test_an_empty_answer_is_none_without_asking_the_model():
    """空回答 = ``none``，直接判，不发请求（1.0 ``:4323``）。"""
    http = FakeHttpClient([])
    result = _runner(http).judge_task_action("q", "   ", "answer", "complete", "answered")
    assert result["actual_action"] == "none"
    assert result["correct"] is False
    assert http.calls == []


def test_task_action_without_a_gt_label_is_not_labeled():
    http = FakeHttpClient([])
    result = _runner(http).judge_task_action("q", "答案", None, "complete", "answered")
    assert result == {"actual_action": None, "correct": None, "reason": "not_labeled"}


def test_task_judge_marks_correctness_by_comparing_actual_to_expected():
    http = FakeHttpClient([judge_reply('{"actual_action":"refuse","reason":"明确拒绝"}')])
    result = _runner(http).judge_task_action("q", "我不能提供", "refuse", "complete", "answered")
    assert result["actual_action"] == "refuse"
    assert result["correct"] is True


def test_evidence_judge_is_off_and_returns_not_applicable():
    """证据判分默认关（``BENCH_EVIDENCE_JUDGE``），一个 item 的 evidence 字段应是 not_applicable。

    **区分力**：把开关读反的实现会去打一次多模态请求；断 ``http.calls`` 才能分开。
    """
    # 两个响应：task_judge 一次、回答质量一次。**两条都要给**——假件的队列空了就失败，
    # 而那种失败会被 judge 的重试吞成一次 failed verdict，让这条断言变得毫无区分力。
    http = FakeHttpClient(
        [
            judge_reply('{"actual_action":"answer","reason":"直接回答"}'),
            judge_reply('{"score":2,"reason":"x"}'),
        ]
    )
    item = {
        "question": "q",
        "reference_answer": "ref",
        "runtime_turns": [
            {
                "message": "q",
                "answer": "边上是 a.jpg",
                "predicted_images": [{"asset_id": "asset_a"}],
                "expected_action": "answer",
            },
        ],
    }
    _runner(http).judge_item(item, {"answer": "ref"}, {})
    assert item["runtime_turns"][0]["evidence_judge"] == {"score": None, "reason": "not_applicable"}
    assert len(http.calls) == 2  # 一次 task_judge + 一次回答质量


def test_evidence_without_images_is_a_zero_not_an_error():
    """没有召回图却断言了图片事实 → 0 分（1.0 ``:4394``）。"""
    http = FakeHttpClient([])
    result = _runner(http, evidence_enabled=True).judge_evidence("q", "边上是 a.jpg", [], {})
    assert result == {"score": 0, "reason": "no_image_evidence", "input": None}


# ---- 一个 item 的编排 -----------------------------------------------------


def _multi_turn_item() -> dict[str, Any]:
    return {
        "qa_id": "q1",
        "question": "她穿什么颜色的裙子？",
        "reference_answer": "红色",
        "runtime_turns": [
            {"message": "她穿什么？", "answer": "红色", "expected_action": "answer"},
            {"message": "什么颜色？", "answer": "红色", "expected_action": "answer"},
        ],
        "judge_status": "pending",
    }


def test_judge_item_attaches_per_turn_scores_and_item_level_is_the_last_turn():
    """多轮题：**每一轮各判一次**，item 级取最后一轮（1.0 ``:3640``）。

    **区分力**：一个只判第一轮的实现会让最后一轮的分数停在 ``pending_judge``，
    而 item 级仍然「有分数」——只断言 item 级分数的用例对它完全没有区分力。
    """
    http = FakeHttpClient(
        [
            judge_reply('{"actual_action":"answer","reason":"直接回答"}'),
            judge_reply('{"score":1,"reason":"部分正确"}'),
            judge_reply('{"actual_action":"answer","reason":"直接回答"}'),
            judge_reply('{"score":2,"reason":"正确"}'),
        ]
    )
    item = _multi_turn_item()
    _runner(http).judge_item(item, {"answer": "红色"}, {})
    turns = item["runtime_turns"]
    assert turns[0]["judge"]["score"] == 1
    assert turns[1]["judge"]["score"] == 2
    assert item["judge"]["score"] == 2
    assert item["task_judge"]["actual_action"] == "answer"
    assert len(item["task_judges"]) == 2
    assert item["judge_status"] == "completed"
    assert item["judge_ms"] is not None


def test_a_failed_sub_judge_marks_the_item_failed_not_scored():
    """任何一个子判分 failed，item 就是 failed——「没打成」不能看起来像「打了 0 分」。"""
    http = FakeHttpClient(
        [
            judge_reply('{"actual_action":"answer","reason":"直接回答"}'),
            RuntimeError("judge endpoint down"),
            RuntimeError("judge endpoint down"),  # 重试那一次也用同一条错误
        ]
    )
    item = _multi_turn_item()
    item["runtime_turns"] = item["runtime_turns"][:1]
    _runner(http).judge_item(item, {"answer": "红色"}, {})
    assert item["judge_status"] == "failed"
    assert item["judge"]["score"] is None
    assert "judge_error" in item["judge"]["reason"]


def test_the_prompt_constant_is_the_1_0_text():
    """提示词是判分口径本身；这条钉住它没有被「整理」过。"""
    assert JUDGE_PROMPT.startswith("你是通用对话任务的回答质量评测员")
    assert '{"score":0|1|2,"reason":"简短中文理由"}' in JUDGE_PROMPT


@pytest.mark.parametrize(
    "value, expected",
    [(0, 0), (1, 1), (2, 2), (1.0, 1), ("1", 1), ("2", 2), (" 0 ", 0), ("1.0", 1), ("1e0", 1)],
)
def test_score_normalization_accepts_only_explicit_integral_scores(value, expected):
    from photobench.domain.runs.judge import normalize_judge_score

    fact = normalize_judge_score(value)
    assert fact["score"] == expected
    assert type(fact["score"]) is int
    assert fact["converted"] is isinstance(value, str)
    assert fact["rule_version"] == 1


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        "",
        " ",
        float("nan"),
        float("inf"),
        "NaN",
        "Infinity",
        "1.2",
        1.2,
        3,
        -1,
        "得2分",
        [],
        {},
        "1_0",
    ],
)
def test_score_normalization_rejects_invalid_types_and_values(value):
    from photobench.domain.runs.judge import normalize_judge_score

    assert normalize_judge_score(value)["score"] is None


def test_quality_retains_raw_response_and_normalization_facts():
    raw = judge_reply('{"score":"1.0","reason":"部分满足"}')
    result = _runner(FakeHttpClient([raw])).judge("q", "r", "a")
    assert result["score"] == 1
    assert result["score_normalization_version"] == 1
    assert result["score_normalization"]["converted"] is True
    assert result["attempts"][0]["raw_response"] == raw


def test_inconsistent_retry_failure_preserves_both_responses_without_fallback_score():
    first = judge_reply('{"score":"0","reason":"模型回答正确"}')
    second = judge_reply('{"score":true,"reason":"得2分"}')
    result = _runner(FakeHttpClient([first, second])).judge("q", "r", "a")
    assert result["score"] is None
    assert len(result["attempts"]) == 2
    assert result["attempts"][1]["raw_response"] == second
    assert result["raw_text"] == second["choices"][0]["message"]["content"]


@pytest.mark.parametrize(
    "reason",
    [
        "正确地点为甲地，模型未回答正确。",
        "参考答案给出正确地点，但模型回答不正确。",
        "模型没有回答正确，因此得0分。",
    ],
)
def test_reference_and_negative_reason_keep_zero_score(reason):
    import json

    result = _runner(FakeHttpClient([judge_reply(json.dumps({"score": " 0 ", "reason": reason}))])).judge(
        "q", "r", "a"
    )
    assert result["score"] == 0
    assert len(result["attempts"]) == 1


def test_consistency_retry_transport_failure_retains_first_raw_without_fabricating_second():
    first = judge_reply('{"score":0,"reason":"模型回答正确"}')
    result = _runner(FakeHttpClient([first, RuntimeError("offline"), RuntimeError("offline")])).judge(
        "q", "r", "a"
    )
    assert result["score"] is None
    assert result["judge_status"] == "failed"
    assert result["attempts"][0]["raw_response"] == first
    assert "error" in result["attempts"][1]
    assert "raw_response" not in result["attempts"][1]


def test_reference_answer_praise_does_not_describe_the_tested_models_answer():
    text = '{"score":"0","reason":"参考答案已经回答了正确地点但模型未能作答，所以得0分。"}'
    http = FakeHttpClient([judge_reply(text)])
    result = _runner(http).judge("q", "r", "a")
    assert result["score"] == 0
    assert len(http.calls) == 1


def test_request_metrics_count_actual_attempts_and_rate_limits():
    cause = Exception("rate limit")
    cause.response = SimpleNamespace(status_code=429)
    error = RuntimeError("HTTP 429")
    error.__cause__ = cause
    http = FakeHttpClient([error, judge_reply('{"score":2,"reason":"ok"}')])
    runner = _runner(http)
    runner._request({"model": "m"})
    metrics = runner.request_metrics()
    assert metrics["requests_started"] == 2
    assert metrics["http_429"] == metrics["request_errors"] == metrics["responses_received"] == 1
    assert metrics["in_flight"] == metrics["rate_waiting"] == 0
    metrics["requests_started"] = 99
    assert runner.request_metrics()["requests_started"] == 2


@pytest.mark.parametrize("failed_stage", ["task", "quality", "evidence"])
@pytest.mark.parametrize("error_kind", ["ReadTimeout", "WriteTimeout"])
def test_uncertain_cloud_post_is_not_retried_or_hidden(failed_stage, error_kind):
    import httpx

    from photobench.infrastructure.http.client import HttpxJsonClient

    calls = []
    task_response = judge_reply('{"actual_action":"answer","reason":"ok"}')
    quality_response = judge_reply('{"score":2,"reason":"ok"}')

    def handler(request):
        import json

        payload = json.loads(request.content)
        system = payload["messages"][0]["content"]
        calls.append(system)
        if system == failed_stage:
            raise getattr(httpx, error_kind)("request outcome is unknown", request=request)
        return httpx.Response(200, json=task_response if system == "task" else quality_response)

    client = HttpxJsonClient(client=httpx.Client(transport=httpx.MockTransport(handler)))
    runner = JudgeRunner(
        http=client,
        judge_url="http://judge/v1",
        judge_model="m",
        task_judge_prompt="task",
        answer_quality_prompt="quality",
        evidence_judge_prompt="evidence",
        evidence_enabled=failed_stage == "evidence",
        inline_images=lambda *_: [{"type": "image_url", "image_url": {"url": "data:test"}}],
    )
    item = {
        "runtime_turns": [
            {
                "message": "q",
                "answer": "a",
                "expected_action": "answer",
                "termination_reason": "answered",
                "agent_status": "complete",
                "predicted_images": [{"file_name": "a.jpg"}],
            }
        ]
    }
    result = runner.judge_item(item, {}, {})
    assert result["judge_status"] == "outcome_unknown"
    key = {"task": "task_judge", "quality": "judge", "evidence": "evidence_judge"}[failed_stage]
    verdict = result["runtime_turns"][0][key]
    assert verdict["judge_status"] == "outcome_unknown"
    assert verdict["request_failure"]["automatic_replay"] is False
    assert calls.count(failed_stage) == 1
    if failed_stage == "task":
        assert calls == ["task"]  # Later sub-judgements were never dispatched.
    assert runner.request_metrics()["requests_started"] == len(calls)
    assert runner.request_metrics()["request_errors"] == 1


def test_received_server_error_remains_retryable():
    cause = Exception("unavailable")
    cause.response = SimpleNamespace(status_code=503)
    error = RuntimeError("HTTP 503")
    error.__cause__ = cause
    http = FakeHttpClient([error, judge_reply('{"score":2,"reason":"ok"}')])
    runner = _runner(http)
    runner._request({"model": "m"})
    assert len(http.calls) == 2


@pytest.mark.parametrize("method", ["quality", "task", "evidence"])
@pytest.mark.parametrize(
    "finish_reason,kind", [("length", "output_limit"), ("content_filter", "provider_rejected")]
)
def test_cloud_incomplete_finish_even_with_valid_json_never_retries(method, finish_reason, kind):
    raw = judge_reply('{"score":0,"reason":"内容错误","actual_action":"answer"}')
    raw["choices"][0]["finish_reason"] = finish_reason
    http = FakeHttpClient([raw])
    runner = _runner(
        http, inline_images=lambda *_: [{"type": "image_url", "image_url": {"url": "data:test"}}]
    )
    if method == "quality":
        result = runner.judge("q", "ref", "answer")
    elif method == "task":
        result = runner.judge_task_action("q", "answer", "answer", "complete", "answered")
    else:
        result = runner.judge_evidence("q", "answer", [{"file_name": "a"}], {})
    assert result["score"] is None
    assert result["judge_status"] == "failed"
    assert result["operational_issue"]["kind"] == kind
    assert result["reason"] == "judge_" + kind
    assert result["operational_issue"]["evidence"]["value"] == finish_reason
    assert result["operational_issue"]["scope"] == "judge"
    assert result["raw_response"] == raw
    assert len(http.calls) == 1


def test_item_with_operational_failure_never_calls_cloud():
    http = FakeHttpClient([])
    result = _runner(http).judge_item(
        {
            "execution_status": "completed",
            "termination_reason": "step_budget_exhausted",
            "runtime_turns": [{"message": "q", "answer": "a", "expected_action": "answer"}],
        },
        {},
        {},
    )
    assert result["judge"]["score"] is None
    assert result["judge_status"] == "skipped"
    assert http.calls == []


@pytest.mark.parametrize(
    "finish_reason,kind", [("length", "output_limit"), ("content_filter", "provider_rejected")]
)
def test_cloud_incomplete_finish_stops_later_subrequests_and_preserves_raw_turn(finish_reason, kind):
    raw = judge_reply('{"actual_action":"answer","reason":"足够"}')
    raw["choices"][0]["finish_reason"] = finish_reason
    http = FakeHttpClient([raw])
    result = _runner(http).judge_item(
        {
            "execution_status": "completed",
            "runtime_turns": [
                {"message": "q", "answer": "a", "expected_action": "answer", "termination_reason": "answered"}
            ],
        },
        {},
        {},
    )
    assert result["execution_status"] == "completed"
    assert result["judge_status"] == "failed"
    assert result["judge"]["score"] is None
    assert result["judge_issue"]["kind"] == kind
    assert result["runtime_turns"][0]["task_judge"]["raw_response"] == raw
    assert len(http.calls) == 1

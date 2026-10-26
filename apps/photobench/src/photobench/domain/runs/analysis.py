"""run 读侧的纯计算：列表条目 / 汇总 / item 投影。

**这一层为什么在 domain 而不是 application。** 它要被两边调用：infrastructure 的
``FileRunStore.iter_entries`` 在扫描 ``results/`` 时就要把每个 run 压成瘦条目（否则
11 GB 的结果解析完只能整个丢掉），而 infrastructure **不能** import application
（``photobench-infrastructure-purity``）。这些函数全是纯的——不碰文件、不碰网络、
不碰时间以外的环境——所以它们落在 domain 不违反 ``domain-purity``（唯一的
``datetime``/``json``/``re`` 都不是 forbidden 名单上的东西）。

**与 1.0 的关系：逐个函数照搬，只做两处形式改动。**
- 1.0 挂在 ``BenchmarkRun`` / ``OrchestratorRepository`` 上的 ``@staticmethod``
  ``@classmethod`` 变成模块级函数（去掉 ``cls`` 那一层，``cls._percentile`` 变成
  ``percentile``）。**计算过程一行没改**。
- ``hydrate_qa_metadata`` 从读 ``self.qa_metadata`` 改成收一个参数，让它保持纯；
  1.0 的 ``self.qa_metadata`` 就是 ``OrchestratorRepository._load_qa_metadata()``
  在 ``__init__`` 里建的同一张表（``:4884``）。
- ``attach_gpu_sample_history`` 从读文件改成收**原始行**，解析口径（坏行跳过）照搬。

行号注释一律指 1.0 ``services/photobench/backend/benchmark_orchestrator.py``。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from photobench.domain.runs.graph_quality import aggregate_graph_qa_by_type
from photobench.domain.runs.judge import normalize_judge_score, parse_judge_json
from photobench.domain.runtime.metrics import (
    apple_power,
    apple_temperature,
    device_freshness,
    finite_number,
    memory_freshness,
)

# 1.0 ``:933`` / ``:1261``。
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
RANKING_KS = (1, 5, 10)
JUDGE_CONSISTENCY_VERSION = 3


def now_iso() -> str:
    """1.0 ``:572``。带时区偏移的本地时间，写进评审文件与列表条目。"""
    return datetime.now().astimezone().isoformat()


# ---------------------------------------------------------------------------
# Judge 一致性（1.0 ``:701-728``）
# ---------------------------------------------------------------------------


def judge_score_consistency(
    score: int | None, reason: str, *, version: int = JUDGE_CONSISTENCY_VERSION
) -> bool:
    """Reject praise of an answer scored zero; retain v1 for unversioned history."""
    if score != 0:
        return True
    text = str(reason or "")
    if version >= 2:
        # A reference fact in the next clause is not praise of the model answer.
        text = re.sub(r"[，,。；;！？!?\r\n]+", "\n", text)
    # 先把否定式褒义短语剥掉，免得「不符合预期」/「回答不正确」被当成褒义命中。
    cleaned = re.sub(
        r"(?:未|不|没有|并非).{0,10}(?:正确|准确|一致|完整|符合|预期)",
        "\n" if version >= 2 else "",
        text,
    )
    if version >= 3:
        # Praise must name the tested answer, or be an answer-level assertion at
        # the start of a clause. Reference narration is not praise of the model.
        subject = r"(?:模型|助手)(?:的)?(?:回答|答案|结论)"
        clause_subject = r"^\s*(?:(?:该|此|最终)(?:次)?)?(?:回答|答案|结论)"
        assertions = (
            rf"{subject}.{{0,12}}(?:正确|准确)",
            rf"{clause_subject}.{{0,12}}(?:正确|准确)",
            rf"(?:{subject}|{clause_subject}).{{0,12}}与.{{0,12}}(?:参考答案|标准答案).{{0,8}}一致",
            rf"(?:{subject}|{clause_subject}).{{0,8}}符合.{{0,8}}预期",
            r"^\s*与.{0,12}(?:参考答案|标准答案).{0,8}一致",
            r"^\s*符合.{0,8}预期",
            r"^\s*核心信息.{0,12}(?:一致|完整|正确)",
        )
        return not any(re.search(pattern, cleaned, flags=re.MULTILINE) for pattern in assertions)
    positive_patterns = (
        (r"(?<!可)(?<!可以)" if version >= 2 else "") + r"(?:回答|模型回答).{0,12}(?:正确|准确)",
        r"与.{0,24}(?:参考答案|标准答案).{0,12}一致",
        r"(?:参考答案|标准答案).{0,24}(?:回答|结论).{0,12}一致",
        r"符合.{0,8}预期",
        r"核心信息.{0,12}(?:一致|完整|正确)",
    )
    return not any(re.search(pattern, cleaned) for pattern in positive_patterns)


def judge_projection(judge: dict[str, Any] | None) -> dict[str, Any]:
    """Versioned read-only derivation; saved observations and execution status stay intact."""
    result = dict(judge or {})
    if result.get("score_normalization_version") == 1 or result.get("score_derivation"):
        return result
    original = result.get("score")
    fact = normalize_judge_score(original)
    source = "score"
    raw = str(result.get("raw_text") or "")
    if (
        original is None
        and raw
        and result.get("judge_status") == "completed"
        and not str(result.get("reason") or "").startswith(("judge_inconsistent", "judge_error"))
    ):
        parsed = parse_judge_json(raw)
        candidate = normalize_judge_score(parsed.get("score"))
        if candidate["score"] is not None and judge_score_consistency(
            candidate["score"], str(parsed.get("reason") or "")
        ):
            fact, source = candidate, "raw_text"
    # Existing numerical observations keep their historical consistency version.
    if (
        fact["converted"]
        or (source == "raw_text" and fact["score"] is not None)
        or (original is not None and fact["score"] is None)
    ):
        result["score"] = fact["score"]
        result["score_derivation"] = {
            "rule_version": 1,
            "source": source,
            "source_sha256": hashlib.sha256(
                (
                    raw if source == "raw_text" else json.dumps(original, ensure_ascii=False, sort_keys=True)
                ).encode()
            ).hexdigest(),
            "original_score": original,
            "original_judge_status": result.get("judge_status"),
            "score": fact["score"],
            "consistency_version": JUDGE_CONSISTENCY_VERSION,
            "normalization": fact,
        }
    return result


def judge_consistency_status(judge: dict[str, Any] | None) -> str | None:
    """1.0 ``:718``。"""
    judge = judge_projection(judge)
    recorded_version = judge.get("consistency_version")
    version = JUDGE_CONSISTENCY_VERSION if judge.get("score_derivation") else 1
    if not judge.get("score_derivation") and isinstance(recorded_version, int) and recorded_version in (2, 3):
        version = recorded_version
    if not judge_score_consistency(judge.get("score"), str(judge.get("reason") or ""), version=version):
        return "inconsistent"
    value = judge.get("consistency_status")
    return str(value) if value is not None else None


def judge_score_for_summary(judge: dict[str, Any] | None) -> int | None:
    """1.0 ``:725``：只有 0/1/2 且不自相矛盾的分数才进汇总。"""
    judge = judge_projection(judge)
    score = normalize_judge_score(judge.get("score"))["score"]
    return (
        score
        if judge.get("judge_status") != "failed" and judge_consistency_status(judge) != "inconsistent"
        else None
    )


# ---------------------------------------------------------------------------
# 分位数
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float | None:
    """1.0 ``:6609`` 的 ``_percentile``：向上取整的最近秩分位数。"""
    ordered = sorted(float(value) for value in values if isinstance(value, (int, float)))
    if not ordered:
        return None
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1))]


def nearest_rank_percentile(values: Sequence[int | float], p: float) -> int | float | None:
    """1.0 ``:1637``。与 ``percentile`` 的区别是**不转 float**，保持原值的类型。"""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * p) - 1))
    return ordered[index]


def numeric_ms(value: Any) -> float | None:
    """1.0 ``:3707`` 的 ``_numeric_ms``：非负数才要，负数当成「没测到」。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


# ---------------------------------------------------------------------------
# 媒体键（1.0 ``:936-952``）
# ---------------------------------------------------------------------------


def infer_media_type(value: object, explicit: object = None) -> str:
    """1.0 ``:936``。"""
    media_type = str(explicit or "").strip().lower()
    if media_type in {"image", "video"}:
        return media_type
    text = str(value or "").strip()
    suffix = Path(text.split("?", 1)[0].split("#", 1)[0]).suffix.lower()
    if suffix in VIDEO_EXTENSIONS or re.fullmatch(r"video-\d+", Path(text).name, re.I):
        return "video"
    return "image"


def media_key(media_type: object, media_id: object) -> tuple[str, str]:
    """1.0 ``:947``。GT 与召回结果比对的稳定键。"""
    kind = infer_media_type(media_id, media_type)
    name = Path(str(media_id or "").split("?", 1)[0].split("#", 1)[0]).name
    canonical = Path(name).stem if kind == "video" else name
    return kind, canonical.casefold()


# ---------------------------------------------------------------------------
# 检索指标（1.0 ``:1189-1362``）
# ---------------------------------------------------------------------------


def metric_triplet(gt_keys: set[tuple[str, str]], predicted_keys: set[tuple[str, str]]) -> dict[str, Any]:
    """1.0 ``:1189``。"""
    matched = gt_keys & predicted_keys
    recall = len(matched) / len(gt_keys) if gt_keys else None
    precision = len(matched) / len(predicted_keys) if predicted_keys else (0.0 if gt_keys else None)
    f1 = (
        (2 * precision * recall / (precision + recall))
        if precision is not None and recall is not None and precision + recall
        else (0.0 if gt_keys else None)
    )
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "matched": len(matched),
        "predicted": len(predicted_keys),
        "gt": len(gt_keys),
    }


def micro_metrics_from_counts(items: Sequence[dict[str, Any]], field: str) -> dict[str, Any]:
    """1.0 ``:1221``。"""
    rows: list[dict[str, Any]] = [
        row for item in items if isinstance((row := item.get(field)), dict) and int(row.get("gt") or 0) > 0
    ]
    gt = sum(int(row.get("gt") or 0) for row in rows)
    predicted = sum(int(row.get("predicted") or 0) for row in rows)
    matched = sum(int(row.get("matched") or 0) for row in rows)
    precision = matched / predicted if predicted else (0.0 if gt else None)
    recall = matched / gt if gt else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else (0.0 if gt else None)
    )
    return {
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "metric_count": len(rows),
        "matched": matched,
        "predicted": predicted,
        "gt": gt,
    }


def macro_metrics_from_counts(items: Sequence[dict[str, Any]], field: str) -> dict[str, Any]:
    """1.0 ``:1243``。"""
    rows: list[dict[str, Any]] = [
        row for item in items if isinstance((row := item.get(field)), dict) and int(row.get("gt") or 0) > 0
    ]
    values: dict[str, list[float]] = {"precision": [], "recall": [], "f1": []}
    for row in rows:
        gt = int(row.get("gt") or 0)
        predicted = int(row.get("predicted") or 0)
        matched = int(row.get("matched") or 0)
        precision = matched / predicted if predicted else 0.0
        recall = matched / gt
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        values["precision"].append(precision)
        values["recall"].append(recall)
        values["f1"].append(f1)
    return {
        metric: sum(samples) / len(samples) if samples else None for metric, samples in values.items()
    } | {"metric_count": len(rows)}


def ranking_metrics(ranked_keys: Sequence[tuple[str, str]], gt_keys: set[tuple[str, str]]) -> dict[str, Any]:
    """1.0 ``:1264``。"""
    if not gt_keys:
        return {}
    ranked = list(ranked_keys)
    data: dict[str, Any] = {"ranked_count": len(ranked), "gt_count": len(gt_keys)}
    for k in RANKING_KS:
        found = len(gt_keys & set(ranked[:k]))
        data[f"r_at_{k}"] = round(found / len(gt_keys), 4)
        data[f"hit_at_{k}"] = 1.0 if found else 0.0
    first = next((index for index, key in enumerate(ranked, 1) if key in gt_keys), None)
    data["mrr"] = round(1.0 / first, 4) if first else 0.0
    hits = 0
    total = 0.0
    for index, key in enumerate(ranked, 1):
        if key in gt_keys:
            hits += 1
            total += hits / index
    data["average_precision"] = round(total / len(gt_keys), 4)
    data["p_at_5"] = round(len(gt_keys & set(ranked[:5])) / min(5, len(ranked)), 4) if ranked else 0.0
    return data


def mean_ranking_metrics(items: Sequence[dict[str, Any]], field: str = "image_ranking") -> dict[str, Any]:
    """1.0 ``:1294``。"""
    rows = [
        item[field]
        for item in items
        if isinstance(item.get(field), dict) and int(item[field].get("gt_count") or 0) > 0
    ]
    if not rows:
        return {}
    data: dict[str, Any] = {}
    for k in RANKING_KS:
        subset = [row for row in rows if int(row.get("gt_count") or 0) <= k]
        data[f"r_at_{k}_question_count"] = len(subset)
        if subset:
            data[f"r_at_{k}"] = round(sum(row[f"r_at_{k}"] for row in subset) / len(subset), 4)
            data[f"hit_at_{k}"] = round(sum(row[f"hit_at_{k}"] for row in subset) / len(subset), 4)
        else:
            data[f"r_at_{k}"] = None
            data[f"hit_at_{k}"] = None
    r5_rows = [row for row in rows if int(row.get("gt_count") or 0) <= 5]
    data["coverage_short_count"] = sum(
        1 for row in r5_rows if int(row.get("ranked_count") or 0) < int(row.get("gt_count") or 0)
    )
    data["no_candidate_count"] = sum(1 for row in r5_rows if int(row.get("ranked_count") or 0) == 0)
    data["mrr"] = round(sum(row["mrr"] for row in rows) / len(rows), 4)
    data["average_precision"] = round(sum(row["average_precision"] for row in rows) / len(rows), 4)
    p5 = [row for row in rows if int(row.get("gt_count") or 0) <= 5]
    data["p_at_5"] = round(sum(row["p_at_5"] for row in p5) / len(p5), 4) if p5 else None
    data["p_at_5_question_count"] = len(p5)
    data["question_count"] = len(rows)
    data["multi_positive_count"] = sum(1 for row in rows if int(row.get("gt_count") or 0) > 1)
    data["short_candidate_count"] = sum(1 for row in rows if int(row.get("ranked_count") or 0) < 10)
    return data


def ranking_from_item(item: dict[str, Any]) -> dict[str, Any]:
    """1.0 ``:1338``。"""
    recorded = item.get("image_ranking")
    if isinstance(recorded, dict):
        return recorded
    gt_media = [entry for entry in (item.get("gt_images") or []) if entry.get("media_type") == "image"]
    if not gt_media:
        return {}
    candidates = item.get("retrieved_candidate_media") or item.get("retrieved_candidate_images") or []
    return ranking_metrics(
        [
            media_key(value.get("media_type"), value.get("media_id") or value.get("file_name"))
            for value in candidates
            if value.get("media_type") == "image"
        ],
        {media_key(entry.get("media_type"), entry.get("media_id")) for entry in gt_media},
    )


def retrieval_metric_eligible(item: dict[str, Any]) -> bool:
    """1.0 ``:1360``。不可回答的题目不进检索指标的分母。"""
    return str(item.get("answerability") or "").strip().lower() != "unanswerable"


# ---------------------------------------------------------------------------
# 归因 / 轨迹（1.0 ``:3838-4566``）
# ---------------------------------------------------------------------------


def parse_action_payload(value: object) -> dict[str, Any] | None:
    """1.0 ``:3908``。从 trace 载荷里取出结构化 Agent 动作。"""
    if isinstance(value, dict):
        return value if value.get("action") else None
    text = str(value or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        body = text.split("\n", 1)[-1]
        if body.rstrip().endswith("```"):
            body = body.rstrip()[:-3]
        text = body.strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("action"):
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start < 0:
        return None
    try:
        decoded, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) and decoded.get("action") else None


def trace_action(step: dict[str, Any]) -> dict[str, Any] | None:
    """1.0 ``:3940``。"""
    for key in ("detail", "raw", "raw_full"):
        parsed = parse_action_payload(step.get(key))
        if parsed is not None:
            return parsed
    return None


def _native_turn_completion(turn: dict[str, Any]) -> bool | None:
    termination = str(turn.get("termination_reason") or "").lower()
    if termination not in {"answered", "answered_with_gap"}:
        return False if termination else None
    budget = (turn.get("agent2_trace") or {}).get("budget_outcome") or {}
    steps, maximum = budget.get("model_steps"), budget.get("max_steps")
    if (
        not isinstance(steps, int)
        or isinstance(steps, bool)
        or steps < 0
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum < 0
    ):
        return None
    status = str(turn.get("agent_status") or turn.get("status") or turn.get("turn_status") or "").lower()
    return (
        bool(str(turn.get("answer") or "").strip())
        and status not in {"partial", "error", "timeout", "failed", "cancelled", "canceled"}
        and steps <= maximum
    )


def agent_stability(item: dict[str, Any]) -> dict[str, Any]:
    """1.0 ``:4532``。"""
    trace = [step for step in item.get("execution_trace") or [] if isinstance(step, dict)]
    model_steps = [
        step
        for step in trace
        if str(step.get("stage") or step.get("type") or "") == "model"
        and str(step.get("call_type") or "agent") in {"agent", "recovery"}
        and str(step.get("status") or "complete").lower() != "error"
    ]
    native = item.get("metrics_contract_version") == 2 or "agent2_trace" in item
    parse_total = 0 if native else len(model_steps)
    parse_success = sum(1 for step in model_steps if trace_action(step) is not None) if not native else 0
    facts = [
        fact
        for step in model_steps
        for fact in step.get("tool_argument_parse_facts") or []
        if isinstance(fact, dict) and isinstance(fact.get("json_object_valid"), bool)
    ]
    failed_statuses = {"partial", "error", "timeout", "failed", "cancelled", "canceled"}
    status = str(item.get("agent_status") or "").lower()
    termination = str(item.get("termination_reason") or "").lower()
    turn_outcome = item.get("turn_outcome") or (
        (item.get("conversation") or [{}])[-1].get("turn_outcome") if item.get("conversation") else None
    )
    complete: bool | None
    if native:
        turns = item.get("runtime_turns") or [item]
        outcomes = [_native_turn_completion(turn) if isinstance(turn, dict) else None for turn in turns]
        complete = False if False in outcomes else (None if None in outcomes else True)
    else:
        complete = (
            (
                turn_outcome == "final_answer"
                and bool(item.get("answer"))
                and status not in failed_statuses
                and not any(
                    marker in termination for marker in ("step", "budget", "timeout", "error", "failure")
                )
            )
            if turn_outcome
            else None
        )
    return {
        "metrics_contract_version": 2,
        "json_parse_total": parse_total or None,
        "json_parse_success": parse_success if parse_total else None,
        "json_parse_rate": round(parse_success / parse_total, 3) if parse_total else None,
        "tool_argument_json_total": len(facts) or None,
        "tool_argument_json_success": sum(f["json_object_valid"] for f in facts) if facts else None,
        "tool_argument_json_rate": round(sum(f["json_object_valid"] for f in facts) / len(facts), 3)
        if facts
        else None,
        "completed_within_steps": complete,
        "final_turn_outcome": turn_outcome,
    }


def _round_or_none(metrics: dict[str, Any] | None, key: str) -> float | None:
    """从一份 micro 指标里取一格，没有就 ``None``（**不是 0.0**）。"""
    if metrics is None:
        return None
    value = metrics.get(key)
    return round(float(value), 3) if isinstance(value, (int, float)) else None


def derive_attribution(item: dict[str, Any]) -> dict[str, Any]:
    """1.0 ``:4202``。从已存的 turn 字段里判定失败层。"""
    judge_score = judge_score_for_summary(item.get("judge"))
    recall = item.get("retrieval_recall")
    # **R 层问的是「检索找到了吗」，不是「交付了吗」。**
    #
    # ``retrieval_recall`` 那一格的分母是 GT、分子是**交付集**，所以零交付的题上它必然
    # 是 0——实测 424 道题的 R 层亮红，而其中相当一部分的检索其实抓到了正确答案，只是
    # 模型没把图交出来。把两件事混在一个数里，归因表就只是「没交付」的影子。
    # 候选级指标到位之后（``candidate_retrieval_recall``，分子分母都取检索交给模型的那
    # 一批），R 才真的在回答检索。
    candidate_recall = item.get("candidate_retrieval_recall")
    source = "candidate" if isinstance(candidate_recall, (int, float)) else "delivery"
    layer_recall = candidate_recall if source == "candidate" else recall
    tools = item.get("tool_trace") or []
    calls = item.get("model_call_metrics") or []
    guard = item.get("guard_debug") or {}
    layers = {key: "na" for key in ("R", "V", "O", "T", "S", "G", "J")}
    details = {}
    if isinstance(layer_recall, (int, float)):
        layers["R"] = "pass" if layer_recall >= 1 else "fail"
        label = "检索召回率" if source == "candidate" else "召回率（交付口径，无候选集）"
        details["R"] = f"{label} {layer_recall:.1%}"
    if (
        item.get("angle") in {"scene_visual", "food_or_object"}
        or item.get("task_type") == "T1_retrieve_media"
    ):
        layers["V"] = "pass" if item.get("predicted_file_names") else "fail"
        details["V"] = "已返回图片" if item.get("predicted_file_names") else "未返回可识别图片"
    if any(
        str(trace.get("tool") or "").lower() in {"read_photo_text", "ocr", "inspect_photo"} for trace in tools
    ):
        ok = any(
            str(trace.get("status") or "").lower() in {"ok", "complete", "completed", "success"}
            for trace in tools
        )
        layers["O"] = "pass" if ok else "fail"
        details["O"] = "检测到 OCR/图片复核工具轨迹"
    if tools:
        failed_tools = [
            trace
            for trace in tools
            if str(trace.get("status") or "").lower() not in {"ok", "complete", "completed", "success"}
        ]
        layers["T"] = "fail" if failed_tools else "pass"
        details["T"] = f"工具 {len(tools)} 次，失败 {len(failed_tools)} 次"
    if calls:
        layers["S"] = "pass" if item.get("answer") else "fail"
        details["S"] = "已生成回答" if item.get("answer") else "无回答"
    if guard or item.get("termination_reason") or item.get("agent_status"):
        blocked = str(item.get("agent_status") or "").lower() in {"blocked", "blocked_by_guard", "error"}
        layers["G"] = "fail" if blocked else "pass"
        details["G"] = item.get("termination_reason") or item.get("agent_status") or "已记录 Guard"
    if judge_score in {0, 1, 2}:
        layers["J"] = "pass" if judge_score == 2 else "fail"
        details["J"] = f"Judge {judge_score} 分"
    primary = next((key for key in ("G", "R", "V", "O", "T", "S", "J") if layers[key] == "fail"), "PASS")
    return {
        "primary": primary,
        "layers": layers,
        "detail": details,
        "source": "derived_from_turn_result",
        # **R 这一层用的是哪一档口径**，必须能读出来：有候选集的 run 判检索，没有的
        # 退回交付口径。不标出来的话，两批 run 的 R 层会被当成同一个数比较。
        "recall_source": source,
    }


def annotate_model_calls(model_calls: Sequence[Any], execution_trace: Sequence[Any]) -> list[dict[str, Any]]:
    """1.0 ``:3948``。给历史 run 的模型调用补上可读的用途说明。"""
    calls = [dict(call) for call in model_calls if isinstance(call, dict)]
    if not calls:
        return calls
    if all(call.get("call_observation") for call in calls):
        return calls
    if any(call.get("call_type") for call in calls):
        return calls
    trace = [step for step in execution_trace if isinstance(step, dict)]
    if not trace:
        for call in calls:
            call["call_observation"] = {
                "kind": "unknown",
                "label": "历史模型调用",
                "purpose": "旧记录未保存调用用途",
                "trigger": "未记录",
                "outcome": "仅保留原始模型性能指标",
                "source": "historical_unresolved",
            }
        return calls

    assignments: list[tuple[int, str, dict[str, Any], Any, int]] = []
    call_index = 0
    recovery_pending = None
    for trace_index, step in enumerate(trace):
        stage = str(step.get("stage") or step.get("type") or "")
        if stage in {"model", "writer", "judge"}:
            if call_index >= len(calls) or str(calls[call_index].get("role") or "") in {"inspect", "ocr"}:
                assignments = []
                break
            assignments.append((call_index, stage, step, recovery_pending, trace_index))
            call_index += 1
            recovery_pending = None
            if stage == "model":
                next_stage = next(
                    (
                        str(candidate.get("stage") or candidate.get("type") or "")
                        for candidate in trace[trace_index + 1 :]
                        if str(candidate.get("stage") or candidate.get("type") or "")
                        in {"model", "tool", "writer", "guard", "judge"}
                    ),
                    "",
                )
                if next_stage == "model":
                    recovery_pending = "format"
        elif stage == "tool":
            tool = step.get("tool") or (step.get("detail") if isinstance(step.get("detail"), str) else "")
            expected_role = (
                "inspect" if tool == "inspect_photo" else "ocr" if tool == "read_photo_text" else None
            )
            if expected_role:
                while call_index < len(calls) and str(calls[call_index].get("role") or "") == expected_role:
                    assignments.append((call_index, "tool_internal", step, False, trace_index))
                    call_index += 1
        elif stage == "guard" and str(step.get("status") or "").lower() == "fail":
            recovery_pending = "guard"
        if stage == "judge":
            detail = step.get("detail") or {}
            if isinstance(detail, dict) and detail.get("faithful") is False:
                recovery_pending = "guard"
    if call_index != len(calls) or len(assignments) != len(calls):
        for call in calls:
            call["call_observation"] = {
                "kind": "unknown",
                "label": "历史模型调用",
                "purpose": "旧记录没有足够信息完成可靠对齐",
                "trigger": "未记录",
                "outcome": "仅保留原始模型性能指标",
                "source": "historical_unresolved",
            }
        return calls

    for index, stage, step, recovery_reason, trace_index in assignments:
        call = calls[index]
        action = trace_action(step)
        if stage == "tool_internal":
            tool = step.get("tool") or (step.get("detail") if isinstance(step.get("detail"), str) else "")
            observation = {
                "kind": "tool_internal",
                "label": "工具内部模型调用（历史对齐）",
                "purpose": "读取照片文字" if tool == "read_photo_text" else "识别照片视觉细节",
                "trigger": f"工具 {tool} 执行内部模型推理",
                "outcome": f"完成 {tool} 的模型处理",
                "source": "historical_trace_aligned",
                "related_tool": tool,
            }
        elif stage == "judge":
            detail = step.get("detail") or {}
            faithful = detail.get("faithful") if isinstance(detail, dict) else None
            observation = {
                "kind": "faithfulness_judge",
                "label": "L2 事实一致性检查（历史对齐）",
                "purpose": "检查候选回答是否与工具事实一致",
                "trigger": "L1 Guard 通过且回答使用了工具事实",
                "outcome": "判定通过"
                if faithful is True
                else "判定未通过"
                if faithful is False
                else "完成检查",
                "source": "historical_trace_aligned",
            }
        elif stage == "writer":
            observation = {
                "kind": "writer",
                "label": "最终回答重写（历史对齐）",
                "purpose": "基于受控事实调整最终回答的结构和措辞",
                "trigger": "候选回答触发 Final Writer",
                "outcome": f"重写状态：{step.get('status') or '完成'}",
                "source": "historical_trace_aligned",
            }
        else:
            tool = str((action or {}).get("tool") or "")
            is_final = (action or {}).get("action") == "final"
            next_step = next(
                (
                    candidate
                    for candidate in trace[trace_index + 1 :]
                    if str(candidate.get("stage") or candidate.get("type") or "")
                    in {"model", "tool", "writer", "guard", "judge"}
                ),
                {},
            )
            next_stage = str(next_step.get("stage") or next_step.get("type") or "")
            if not tool and next_stage == "tool":
                tool = str(
                    next_step.get("tool")
                    or (next_step.get("detail") if isinstance(next_step.get("detail"), str) else "")
                )
            generated_candidate = is_final or next_stage in {"writer", "guard", "judge"}
            parse_recovery = next_stage == "model"
            is_recovery = recovery_reason in {"format", "guard"}
            observation = {
                "kind": "recovery" if is_recovery else "agent",
                "label": (
                    "Agent 格式恢复调用（历史对齐）"
                    if recovery_reason == "format"
                    else "Agent 校验恢复调用（历史对齐）"
                    if recovery_reason == "guard"
                    else "Agent 决策 / 回答（历史对齐）"
                ),
                "purpose": (
                    "按格式纠正要求重新生成合法动作"
                    if recovery_reason == "format"
                    else "根据 Guard 或 L2 校验反馈修正回答"
                    if recovery_reason == "guard"
                    else "选择下一步工具或生成候选回答"
                ),
                "trigger": (
                    "上一轮模型输出无法解析"
                    if recovery_reason == "format"
                    else "上一轮 Guard 或 L2 事实一致性检查未通过"
                    if recovery_reason == "guard"
                    else "上一工具返回结果"
                    if index
                    else "用户问题"
                ),
                "outcome": (
                    f"决定调用工具 {tool}"
                    if tool
                    else "输出未形成合法动作，触发格式恢复"
                    if parse_recovery
                    else "生成候选回答"
                    if generated_candidate
                    else "完成 Agent 推理"
                ),
                "source": "historical_trace_aligned",
                "related_tool": tool or None,
            }
        call["call_observation"] = observation
    return calls


def bind_tool_calls_to_model_rounds(
    tool_trace: Sequence[Any], retrieval_trace: Sequence[Any], model_calls: Any = None
) -> list[dict[str, Any]]:
    """1.0 ``:3838``。按 step ID 优先、历史 run 退回按顺序对齐工具与模型轮次。"""
    bound = [dict(trace) for trace in tool_trace if isinstance(trace, dict)]
    if not bound:
        return bound
    calls = [call for call in model_calls if isinstance(call, dict)] if isinstance(model_calls, list) else []
    model_call_count = len(calls) if calls else int(model_calls or 0)
    call_index_by_step: dict[tuple[Any, str], int] = {}
    for index, call in enumerate(calls):
        if not call.get("step_id"):
            continue
        key = (call.get("conversation_turn"), str(call.get("step_id")))
        call_index_by_step[key] = index
        call_index_by_step[("any", str(call.get("step_id")))] = index
    for trace in bound:
        parent_step_id = trace.get("parent_step_id")
        key = (trace.get("conversation_turn"), str(parent_step_id))
        if parent_step_id is not None and key in call_index_by_step:
            trace["model_call_index"] = call_index_by_step[key]
            trace["round_binding_source"] = "step_id"
    if not isinstance(retrieval_trace, list) or not retrieval_trace:
        if model_call_count == 1:
            for trace in bound:
                if trace.get("model_call_index") is None:
                    trace["model_call_index"] = 0
                    trace["round_binding_source"] = "inferred_single_model_call"
        return bound
    model_index = None
    ordered_call_index = 0
    tool_index = 0
    for step in retrieval_trace:
        if not isinstance(step, dict):
            continue
        stage = step.get("stage") or step.get("type")
        step_id = step.get("step_id")
        step_key = (step.get("conversation_turn"), str(step_id))
        mapped_index = call_index_by_step.get(step_key)
        if mapped_index is None:
            mapped_index = call_index_by_step.get(("any", str(step_id)))
        if stage in {"planner", "model", "writer", "judge"}:
            if mapped_index is not None:
                model_index = mapped_index
                ordered_call_index = max(ordered_call_index, mapped_index + 1)
            elif ordered_call_index < model_call_count:
                model_index = ordered_call_index
                ordered_call_index += 1
        elif stage == "tool" and tool_index < len(bound):
            if (
                model_index is not None
                and model_index >= 0
                and bound[tool_index].get("model_call_index") is None
            ):
                bound[tool_index]["model_call_index"] = model_index
                bound[tool_index]["round_binding_source"] = "retrieval_trace"
                if step.get("parent_step_id") is not None:
                    bound[tool_index]["parent_step_id"] = step.get("parent_step_id")
            elif model_index is not None and model_index >= 0 and step.get("parent_step_id") is not None:
                bound[tool_index]["model_call_index"] = model_index
                bound[tool_index]["round_binding_source"] = "retrieval_trace_parent"
                bound[tool_index]["parent_step_id"] = step.get("parent_step_id")
            tool_index += 1
    if model_call_count == 1:
        for trace in bound:
            if trace.get("model_call_index") is None:
                trace["model_call_index"] = 0
                trace["round_binding_source"] = "inferred_single_model_call"
    return bound


def summarize_agent2_trace(runtime_turns: Sequence[Any]) -> dict[str, Any]:
    """1.0 ``:583``。"""
    decisions: list[dict[str, Any]] = []
    statuses: dict[str, int] = {}
    terminal_reasons: dict[str, int] = {}
    budget_outcomes: list[dict[str, Any]] = []
    entries = 0
    partial_entries = 0
    available = False
    last_task_declaration = None
    last_task_state = None
    last_evidence_ledger = None

    for turn in runtime_turns or []:
        trace = turn.get("agent2_trace") if isinstance(turn, dict) else None
        if not isinstance(trace, dict) or not trace:
            continue
        available = True
        decisions.extend(item for item in trace.get("planner_decisions") or [] if isinstance(item, dict))
        if trace.get("task_declaration"):
            last_task_declaration = trace.get("task_declaration")
        if trace.get("task_state"):
            last_task_state = trace.get("task_state")
        if trace.get("evidence_ledger"):
            last_evidence_ledger = trace.get("evidence_ledger")

        status_counts = trace.get("requirement_status_counts") or {}
        if not status_counts:
            for requirement in (trace.get("task_state") or {}).get("requirements") or []:
                if isinstance(requirement, dict) and requirement.get("status"):
                    status = str(requirement["status"])
                    status_counts[status] = status_counts.get(status, 0) + 1
        for status, count in status_counts.items():
            try:
                statuses[str(status)] = statuses.get(str(status), 0) + int(count)
            except (TypeError, ValueError):
                continue
        coverage = trace.get("evidence_coverage") or {}
        if not coverage:
            ledger_entries = (trace.get("evidence_ledger") or {}).get("entries") or []
            coverage = {
                "entries": len(ledger_entries),
                "partial_entries": sum(
                    1
                    for entry in ledger_entries
                    if isinstance(entry, dict)
                    and int((entry.get("coverage") or {}).get("processed") or 0)
                    < int((entry.get("coverage") or {}).get("requested") or 0)
                ),
            }
        entries += int(coverage.get("entries") or 0)
        partial_entries += int(coverage.get("partial_entries") or 0)
        reason = str(trace.get("terminal_reason") or "")
        if reason:
            terminal_reasons[reason] = terminal_reasons.get(reason, 0) + 1
        budget = trace.get("budget_outcome")
        if isinstance(budget, dict):
            budget_outcomes.append(dict(budget))
    res: dict[str, Any] = {
        "available": available,
        "planner_decision_count": len(decisions),
        "planner_fallback_count": sum(1 for item in decisions if item.get("status") == "fallback"),
        "requirement_status_counts": statuses,
        "evidence_coverage": {"entries": entries, "partial_entries": partial_entries},
        "terminal_reasons": terminal_reasons,
        "budget_outcomes": budget_outcomes,
    }
    if last_task_declaration:
        res["task_declaration"] = last_task_declaration
    if last_task_state:
        res["task_state"] = last_task_state
    if last_evidence_ledger:
        res["evidence_ledger"] = last_evidence_ledger
    if decisions:
        res["planner_decisions"] = decisions
    return res


def aggregate_tool_performance(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """1.0 ``:6743``。"""
    per_tool: dict[str, dict[str, Any]] = {}
    for item in items:
        for trace in item.get("tool_trace") or []:
            if not isinstance(trace, dict) or not trace.get("tool"):
                continue
            slot = per_tool.setdefault(
                str(trace["tool"]),
                {
                    "calls": 0,
                    "ok": 0,
                    "latencies": [],
                    "providers": set(),
                    "tiles": set(),
                    "cache_hits": 0,
                    "vlm_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "token_samples": 0,
                    "cache_samples": 0,
                    "vlm_samples": 0,
                },
            )
            slot["calls"] += 1
            if trace.get("provider"):
                slot["providers"].add(str(trace["provider"]))
            if trace.get("fallback_used"):
                slot["fallback_count"] = slot.get("fallback_count", 0) + 1
            if str(trace.get("status") or "").lower() in {"ok", "complete", "completed", "success"}:
                slot["ok"] += 1
            latency_s = trace.get("latency_s")
            if isinstance(latency_s, (int, float)) and latency_s >= 0:
                slot["latencies"].append(float(latency_s) * 1000)
        for name, metrics in (item.get("tool_perf") or {}).items():
            if not isinstance(metrics, dict):
                continue
            slot = per_tool.setdefault(
                str(name),
                {
                    "calls": 0,
                    "ok": 0,
                    "latencies": [],
                    "providers": set(),
                    "tiles": set(),
                    "cache_hits": 0,
                    "vlm_calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "token_samples": 0,
                    "cache_samples": 0,
                    "vlm_samples": 0,
                },
            )
            slot["providers"].update(str(value) for value in metrics.get("providers") or [])
            slot["tiles"].update(str(value) for value in metrics.get("tiles") or [])
            for field in (
                "cache_hits",
                "vlm_calls",
                "prompt_tokens",
                "completion_tokens",
                "token_samples",
                "cache_samples",
                "vlm_samples",
            ):
                if isinstance(metrics.get(field), (int, float)):
                    slot[field] += metrics[field]
    result: dict[str, Any] = {}
    for name, slot in per_tool.items():
        latencies = slot.pop("latencies")
        ok = slot.pop("ok")
        calls = slot["calls"]
        result[name] = {
            **slot,
            "ok_rate": round(ok / calls, 3) if calls else None,
            "p50_ms": percentile(latencies, 0.5),
            "p95_ms": percentile(latencies, 0.95),
            "max_ms": max(latencies) if latencies else None,
            "providers": sorted(slot["providers"]),
            "tiles": sorted(slot["tiles"]),
        }
    return result


def aggregate_delivery(items: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """1.0 ``:6799``。"""
    det_count = 0
    det_kinds: dict[str, int] = {}
    ocr_partial_count = 0
    ocr_partial_reasons: dict[str, int] = {}
    for item in items:
        guard = item.get("guard_debug") or {}
        det = guard.get("deterministic_delivery") or {}
        if det.get("rendered"):
            det_count += 1
            kind = str(det.get("kind") or "unknown")
            det_kinds[kind] = det_kinds.get(kind, 0) + 1
        delivery = item.get("delivery_status") or {}
        if delivery.get("ocr_partial"):
            ocr_partial_count += 1
            reason = str(delivery.get("ocr_partial_reason") or "unknown")
            ocr_partial_reasons[reason] = ocr_partial_reasons.get(reason, 0) + 1
    return {
        "deterministic_delivery_count": det_count,
        "deterministic_delivery_kinds": det_kinds,
        "ocr_partial_count": ocr_partial_count,
        "ocr_partial_reasons": ocr_partial_reasons,
    }


# ---------------------------------------------------------------------------
# 汇总（1.0 ``:4639-4872`` / ``:6662`` / ``:6825``）
# ---------------------------------------------------------------------------


def capability_summary(
    items: Sequence[dict[str, Any]], phases: dict[str, Any] | None = None
) -> dict[str, Any]:
    """1.0 ``:4639`` 的 ``BenchmarkRun._capability_summary``。"""
    answer_judges = [item.get("judge") or {} for item in items]
    answer_scores = [
        score for judge in answer_judges if (score := judge_score_for_summary(judge)) is not None
    ]
    answer_dist = {str(score): answer_scores.count(score) for score in (0, 1, 2)}
    metric_items = [item for item in items if retrieval_metric_eligible(item)]
    excluded_unanswerable_count = len(items) - len(metric_items)
    typed_retrieval_items = [item for item in metric_items if "retrieval_media_refs" in item]
    media_metrics: dict[str, Any] | None
    image_metrics: dict[str, Any]
    video_metrics: dict[str, Any] | None
    media_macro: dict[str, Any]
    image_macro: dict[str, Any]
    video_macro: dict[str, Any] | None
    precision: float | None
    recall: float | None
    f1: float | None
    if typed_retrieval_items:
        media_metrics = micro_metrics_from_counts(typed_retrieval_items, "media_retrieval_counts")
        image_metrics = micro_metrics_from_counts(typed_retrieval_items, "image_retrieval_counts")
        video_metrics = micro_metrics_from_counts(typed_retrieval_items, "video_retrieval_counts")
        media_macro = macro_metrics_from_counts(typed_retrieval_items, "media_retrieval_counts")
        image_macro = macro_metrics_from_counts(typed_retrieval_items, "image_retrieval_counts")
        video_macro = macro_metrics_from_counts(typed_retrieval_items, "video_retrieval_counts")
        image_ranking = mean_ranking_metrics(typed_retrieval_items)
        precision, recall, f1 = (media_metrics["precision"], media_metrics["recall"], media_metrics["f1"])
        retrieval_metric_count = media_metrics["metric_count"]
    else:
        # 历史结果早于「带类型的媒体引用」，那些 run 只有图片。
        retrieval_items = [item for item in metric_items if item.get("retrieval_image_ids")]
        tp = sum(
            len(item.get("retrieved_matched_file_names") or item.get("matched_file_names") or [])
            for item in retrieval_items
        )
        predicted = sum(
            len(item.get("retrieved_file_names") or item.get("predicted_file_names") or [])
            for item in retrieval_items
        )
        gt = sum(len(item.get("retrieval_image_ids") or []) for item in retrieval_items)
        precision = tp / predicted if predicted else (0.0 if gt else None)
        recall = tp / gt if gt else None
        f1 = (
            2 * precision * recall / (precision + recall)
            if precision is not None and recall is not None and precision + recall
            else (0.0 if gt else None)
        )
        retrieval_metric_count = len(retrieval_items)
        media_metrics = None
        image_metrics = {
            "precision": precision,
            "recall": recall,
            "f1": f1,
            "metric_count": retrieval_metric_count,
        }
        video_metrics = None
        # 这些 run 从没存过候选顺序，编一个出来只会把 R@K 变成假数。宁可报空。
        image_ranking = mean_ranking_metrics(metric_items)
        legacy_values: dict[str, list[float]] = {
            metric: [
                float(value)
                for item in retrieval_items
                if isinstance((value := item.get(f"retrieval_{metric}")), (int, float))
            ]
            for metric in ("precision", "recall", "f1")
        }
        image_macro = {
            metric: sum(values) / len(values) if values else None for metric, values in legacy_values.items()
        } | {"metric_count": len(legacy_values["recall"])}
        media_macro = image_macro
        video_macro = None
    # 候选级指标的样本是「有候选集可比的题」——没有那一格的 run（这次契约放宽之前跑的）
    # 不进分母，而不是当成零分。
    candidate_items = [item for item in metric_items if "candidate_retrieval_counts" in item]
    candidate_metrics: dict[str, Any] | None = (
        micro_metrics_from_counts(candidate_items, "candidate_retrieval_counts") if candidate_items else None
    )
    candidate_metric_count = len(candidate_items)
    evidence_judges = [item.get("evidence_judge") or {} for item in items]
    evidence_scores = [
        score
        for judge in evidence_judges
        if (score := normalize_judge_score(judge_projection(judge).get("score"))["score"]) is not None
    ]
    evidence_dist = {str(score): evidence_scores.count(score) for score in (0, 1, 2)}
    action_judges = [
        judge
        for item in items
        for judge in (item.get("task_judges") or [item.get("task_judge") or {}])
        if judge.get("expected_action") in {"answer", "refuse", "clarify"}
    ]
    action_valid = [value for value in action_judges if value.get("correct") in {True, False}]
    parse_totals = [item.get("agent_stability", {}).get("json_parse_total") for item in items]
    parse_successes = [item.get("agent_stability", {}).get("json_parse_success") for item in items]
    tool_argument_total = sum(
        int((item.get("agent_stability") or {}).get("tool_argument_json_total") or 0) for item in items
    )
    tool_argument_success = sum(
        int((item.get("agent_stability") or {}).get("tool_argument_json_success") or 0) for item in items
    )
    parse_total = sum(value for value in parse_totals if isinstance(value, int))
    parse_success = sum(value for value in parse_successes if isinstance(value, int))
    completion = [item.get("agent_stability", {}).get("completed_within_steps") for item in items]
    completion_valid = [value for value in completion if isinstance(value, bool)]
    wall_times = [
        float(item["wall_clock_ms"]) for item in items if isinstance(item.get("wall_clock_ms"), (int, float))
    ]

    # **`timing_breakdown` 是 1.0 的存档字段，2.0 的 run 从来不写它。** 只读它的话，
    # `judge_llm_latency_mean_ms` / `agent_task_latency_mean_ms` 恒为 None——
    # 8772 页面上那两行有位置、没数字。2.0 的真实来源是 item 级的 `judge_ms`
    # 与 `wall_clock_ms`（后者减去前者就是 agent 的墙钟）。
    def _flat_ms(item: Mapping[str, Any], key: str) -> float | None:
        value = item.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    judge_times: list[float] = []
    for item in items:
        saved = item.get("timing_breakdown")
        if isinstance(saved, dict) and isinstance(saved.get("judge_ms"), (int, float)):
            judge_times.append(float(saved["judge_ms"]))
        elif isinstance(item.get("judge_ms"), (int, float)):
            judge_times.append(float(item["judge_ms"]))
    judge_exclusive_ms = None
    agent_throughput_ms = None
    agent_throughput_mode = "historical_interval_estimate"
    agent_phase_wall_ms = numeric_ms((phases or {}).get("qa_eval", {}).get("agent_phase_wall_ms"))
    agent_phase_count = (phases or {}).get("qa_eval", {}).get("agent_completed")
    if not isinstance(agent_phase_count, int) or agent_phase_count <= 0:
        agent_phase_count = len(items)
    timelines = [(item.get("timing_breakdown") or {}).get("timeline") or {} for item in items]
    spans = [
        (tl.get("started_at_epoch"), tl.get("judge_started_at_epoch"), tl.get("judge_finished_at_epoch"))
        for tl in timelines
    ]
    ends: list[float] = []
    for s, item in zip(spans, items, strict=False):
        wall_ms = (item.get("timing_breakdown") or {}).get("wall_clock_ms")
        if isinstance(s[0], (int, float)) and isinstance(wall_ms, (int, float)):
            ends.append(float(s[0]) + float(wall_ms) / 1000)
    phase = (phases or {}).get("qa_eval") or {}
    try:
        phase_start: float | None = datetime.fromisoformat(str(phase.get("started_at"))).timestamp()
        phase_end: float | None = datetime.fromisoformat(str(phase.get("finished_at"))).timestamp()
    except (TypeError, ValueError, OSError):
        phase_start = phase_end = None
    valid_item_spans = [value for s in spans if isinstance((value := s[0]), (int, float))]
    wall_start: float | None = None
    wall_end: float | None = None
    if phase_end is not None and phase_start is not None and phase_end > phase_start:
        wall_start, wall_end = phase_start, phase_end
    elif valid_item_spans and ends:
        wall_start = min(valid_item_spans)
        wall_end = max(ends)
    else:
        wall_start = wall_end = None
    judge_spans = sorted(
        (s[1], s[2])
        for s in spans
        if isinstance(s[1], (int, float)) and isinstance(s[2], (int, float)) and s[2] > s[1]
    )
    if wall_start is not None and wall_end is not None and judge_spans:
        merged: list[list[float]] = []
        for a, b in judge_spans:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        judge_exclusive_ms = round(sum(b - a for a, b in merged) * 1000, 1)
    if agent_phase_wall_ms is not None and agent_phase_wall_ms > 0 and agent_phase_count > 0:
        agent_throughput_ms = round(agent_phase_wall_ms / agent_phase_count, 1)
        agent_throughput_mode = "measured_agent_phase"
    # Overlapping Judge intervals cannot be subtracted from Agent wall time.
    # Legacy runs without a measured Agent stage have no defensible throughput.
    else:
        agent_throughput_mode = "unavailable"
    judge_phase = (phases or {}).get("qa_eval") or {}
    judge_phase_wall_ms = numeric_ms(judge_phase.get("judge_phase_wall_ms"))
    agent_wall_times: list[float] = []
    for item in items:
        saved = item.get("timing_breakdown")
        if isinstance(saved, dict) and isinstance(saved.get("agent_wall_ms"), (int, float)):
            agent_wall_times.append(float(saved["agent_wall_ms"]))
            continue
        wall = _flat_ms(item, "wall_clock_ms")
        judge = _flat_ms(item, "judge_ms")
        # PhotoBench 2.0 records the Agent before submitting its asynchronous Judge.
        if wall is not None and ("agent2_trace" in item or item.get("metrics_contract_version") == 2):
            agent_wall_times.append(wall)
        elif wall is not None and judge is not None:
            agent_wall_times.append(max(0.0, wall - judge))
        elif wall is not None:
            agent_wall_times.append(wall)
    agent_loop_counts = [
        sum(
            1
            for call in item.get("model_call_metrics") or []
            if str(call.get("call_type") or "") in {"agent", "recovery"}
        )
        for item in items
        if isinstance(item.get("model_call_metrics"), list)
        and item.get("model_call_metrics")
        and any(call.get("call_type") for call in item["model_call_metrics"])
    ]
    return {
        "judge_distribution": answer_dist,
        "judge_valid_count": len(answer_scores),
        "answer_quality_mean": round(sum(answer_scores) / len(answer_scores), 3) if answer_scores else None,
        "exact_accuracy": round(answer_dist["2"] / len(answer_scores), 3) if answer_scores else None,
        "core_accuracy": round((answer_dist["1"] + answer_dist["2"]) / len(answer_scores), 3)
        if answer_scores
        else None,
        "retrieval_precision_micro": round(precision, 3) if precision is not None else None,
        "retrieval_recall_micro": round(recall, 3) if recall is not None else None,
        "retrieval_f1_micro": round(f1, 3) if f1 is not None else None,
        "retrieval_precision_macro": round(media_macro["precision"], 3)
        if media_macro["precision"] is not None
        else None,
        "retrieval_recall_macro": round(media_macro["recall"], 3)
        if media_macro["recall"] is not None
        else None,
        "retrieval_f1_macro": round(media_macro["f1"], 3) if media_macro["f1"] is not None else None,
        "retrieval_recall_mean": round(media_macro["recall"], 3)
        if media_macro["recall"] is not None
        else None,
        "retrieval_metric_count": retrieval_metric_count,
        "retrieval_metric_scope": "all_media" if typed_retrieval_items else "legacy_image_only",
        "retrieval_excluded_unanswerable_count": excluded_unanswerable_count,
        # **候选级（检索通道）**。与上面那组「交付级」并列，不是替代：交付级回答
        # 「模型交对了没有」，候选级回答「检索找到没有」。零交付的题在候选级上照样
        # 可以得满分——那正是这次要把两者分开的理由。
        #
        # 计数为 0 时给 ``None`` 而不是 0.0：**「没有候选数据」与「召回率为零」必须
        # 分得开**，否则没有候选集的旧 run 会被读成「检索全军覆没」。
        "candidate_metric_count": candidate_metric_count,
        "candidate_retrieval_precision_micro": _round_or_none(candidate_metrics, "precision"),
        "candidate_retrieval_recall_micro": _round_or_none(candidate_metrics, "recall"),
        "candidate_retrieval_f1_micro": _round_or_none(candidate_metrics, "f1"),
        "media_retrieval_precision_micro": round(media_metrics["precision"], 3)
        if media_metrics and media_metrics["precision"] is not None
        else None,
        "media_retrieval_recall_micro": round(media_metrics["recall"], 3)
        if media_metrics and media_metrics["recall"] is not None
        else None,
        "media_retrieval_f1_micro": round(media_metrics["f1"], 3)
        if media_metrics and media_metrics["f1"] is not None
        else None,
        "media_retrieval_precision_macro": round(media_macro["precision"], 3)
        if media_macro["precision"] is not None
        else None,
        "media_retrieval_recall_macro": round(media_macro["recall"], 3)
        if media_macro["recall"] is not None
        else None,
        "media_retrieval_f1_macro": round(media_macro["f1"], 3) if media_macro["f1"] is not None else None,
        "media_retrieval_metric_count": media_metrics["metric_count"] if media_metrics else None,
        "image_retrieval_precision_micro": round(image_metrics["precision"], 3)
        if image_metrics and image_metrics["precision"] is not None
        else None,
        "image_retrieval_recall_micro": round(image_metrics["recall"], 3)
        if image_metrics and image_metrics["recall"] is not None
        else None,
        "image_retrieval_f1_micro": round(image_metrics["f1"], 3)
        if image_metrics and image_metrics["f1"] is not None
        else None,
        "image_retrieval_precision_macro": round(image_macro["precision"], 3)
        if image_macro["precision"] is not None
        else None,
        "image_retrieval_recall_macro": round(image_macro["recall"], 3)
        if image_macro["recall"] is not None
        else None,
        "image_retrieval_f1_macro": round(image_macro["f1"], 3) if image_macro["f1"] is not None else None,
        "image_retrieval_metric_count": image_metrics["metric_count"] if image_metrics else None,
        "image_ranking": image_ranking or None,
        "video_retrieval_precision_micro": round(video_metrics["precision"], 3)
        if video_metrics and video_metrics["precision"] is not None
        else None,
        "video_retrieval_recall_micro": round(video_metrics["recall"], 3)
        if video_metrics and video_metrics["recall"] is not None
        else None,
        "video_retrieval_f1_micro": round(video_metrics["f1"], 3)
        if video_metrics and video_metrics["f1"] is not None
        else None,
        "video_retrieval_precision_macro": round(video_macro["precision"], 3)
        if video_macro and video_macro["precision"] is not None
        else None,
        "video_retrieval_recall_macro": round(video_macro["recall"], 3)
        if video_macro and video_macro["recall"] is not None
        else None,
        "video_retrieval_f1_macro": round(video_macro["f1"], 3)
        if video_macro and video_macro["f1"] is not None
        else None,
        "video_retrieval_metric_count": video_metrics["metric_count"] if video_metrics else None,
        "evidence_distribution": evidence_dist,
        "evidence_valid_count": len(evidence_scores),
        "evidence_mean": round(sum(evidence_scores) / len(evidence_scores), 3) if evidence_scores else None,
        "evidence_fully_supported_rate": round(evidence_dist["2"] / len(evidence_scores), 3)
        if evidence_scores
        else None,
        "evidence_basically_supported_rate": round(
            (evidence_dist["1"] + evidence_dist["2"]) / len(evidence_scores), 3
        )
        if evidence_scores
        else None,
        "task_decision_labeled_count": len(action_judges),
        "task_decision_valid_count": len(action_valid),
        "task_decision_accuracy": round(
            sum(bool(value.get("correct")) for value in action_valid) / len(action_valid), 3
        )
        if action_valid
        else None,
        "json_parse_total": parse_total or None,
        "json_parse_success": parse_success if parse_total else None,
        "json_parse_success_rate": round(parse_success / parse_total, 3) if parse_total else None,
        "tool_argument_json_total": tool_argument_total or None,
        "tool_argument_json_success": tool_argument_success if tool_argument_total else None,
        "tool_argument_json_rate": round(tool_argument_success / tool_argument_total, 3)
        if tool_argument_total
        else None,
        "qa_completion_valid_count": len(completion_valid),
        "qa_completion_within_steps_rate": round(sum(completion_valid) / len(completion_valid), 3)
        if completion_valid
        else None,
        "e2e_latency_mean_ms": round(sum(wall_times) / len(wall_times), 1) if wall_times else None,
        "e2e_latency_p50_ms": nearest_rank_percentile(wall_times, 0.5),
        "e2e_latency_p95_ms": nearest_rank_percentile(wall_times, 0.95),
        "e2e_latency_max_ms": max(wall_times) if wall_times else None,
        "agent_task_latency_mean_ms": round(sum(agent_wall_times) / len(agent_wall_times), 1)
        if agent_wall_times
        else None,
        "judge_llm_latency_mean_ms": round(sum(judge_times) / len(judge_times), 1) if judge_times else None,
        "judge_exclusive_wall_ms": judge_exclusive_ms,
        "judge_phase_wall_ms": judge_phase_wall_ms,
        "judge_concurrency": judge_phase.get("judge_concurrency"),
        "agent_throughput_latency_ms": agent_throughput_ms,
        "agent_throughput_latency_mode": agent_throughput_mode,
        "agent_phase_wall_ms": agent_phase_wall_ms,
        "agent_phase_completed_count": agent_phase_count if agent_phase_wall_ms is not None else None,
        "agent_throughput_qa_per_s": round(1000 * agent_phase_count / agent_phase_wall_ms, 3)
        if agent_phase_wall_ms and agent_phase_count
        else None,
        "agent_throughput_latency_sample_count": agent_phase_count
        if agent_phase_wall_ms is not None
        else len(judge_spans),
        "agent_throughput_latency_total_count": len(items),
        "agent_loop_calls_mean": round(sum(agent_loop_counts) / len(agent_loop_counts), 3)
        if agent_loop_counts
        else None,
        "agent2_trace": summarize_agent2_trace(
            [turn for item in items for turn in (item.get("runtime_turns") or [item])]
        ),
    }


def benchmark_e2e_latency_excluding_judge_ms(
    phases: dict[str, Any], items: Sequence[dict[str, Any]]
) -> float | None:
    """1.0 ``:4844``。"""
    started_at = (phases.get("identity_seed") or {}).get("started_at")
    qa_phase = phases.get("qa_eval") or {}
    finished_at = qa_phase.get("agent_phase_finished_at") or qa_phase.get("finished_at")
    if not started_at or not finished_at:
        return None
    try:
        wall_ms = (
            datetime.fromisoformat(str(finished_at)) - datetime.fromisoformat(str(started_at))
        ).total_seconds() * 1000
    except (TypeError, ValueError):
        return None
    if qa_phase.get("agent_phase_finished_at"):
        return round(max(0.0, wall_ms), 1)
    if any("agent2_trace" in item or item.get("metrics_contract_version") == 2 for item in items):
        return None
    judge_values = [(item.get("timing_breakdown") or {}).get("judge_ms") for item in items]
    if items and not all(isinstance(value, (int, float)) for value in judge_values):
        return None
    judge_ms = sum(float(value) for value in judge_values if isinstance(value, (int, float)))
    return round(max(0.0, wall_ms - judge_ms), 1)


def list_summary(state: dict[str, Any]) -> dict[str, Any]:
    """1.0 ``:6825`` 的 ``OrchestratorRepository._list_summary``。"""
    saved = dict(state.get("summary") or {})
    items = state.get("items") or []
    recalls = [
        item.get("retrieval_recall")
        for item in items
        if isinstance(item.get("retrieval_recall"), (int, float))
    ]
    scores = [score for item in items if (score := judge_score_for_summary(item.get("judge"))) is not None]
    distribution = {str(score): scores.count(score) for score in (0, 1, 2)}
    saved.update(
        {
            "total": saved.get("total") or state.get("qa_count") or len(items),
            "completed": len(items),
            "judge_valid_count": len(scores),
            "judge_distribution": distribution,
            "retrieval_recall_mean": saved.get("retrieval_recall_mean")
            if saved.get("retrieval_recall_mean") is not None
            else (round(sum(recalls) / len(recalls), 3) if recalls else None),
            "answer_quality_mean": round(sum(scores) / len(scores), 3) if scores else None,
        }
    )
    for key, value in capability_summary(items, state.get("phases") or {}).items():
        if (
            key == "retrieval_recall_mean"
            or key.startswith(("retrieval_", "media_retrieval_", "image_retrieval_", "video_retrieval_"))
            or saved.get(key) is None
        ):
            saved[key] = value
    return saved


def _run_valid(state: dict[str, Any], items: list[dict[str, Any]]) -> bool | None:
    """这一轮**是不是一份可信的结果**。1.0 ``:3816-3830`` 的 ``_compute_run_valid``。

    **为什么必须读侧重算。** 2.0 原先只在 ``_phase_qa_eval`` 收尾时写一次
    （``service.py:1310``），而取消时那段根本跑不到——``run_valid`` 于是永远停在
    ``_initial_state`` 的初值 ``False``。后果是一个被取消的 run 与一个真失败的 run
    在界面上长得一样，而两者的处置相反：前者续跑、后者查原因。

    判据逐条照 1.0：没有任何 partial/failed 相位、题数齐、每题都执行成功、每题都判过。
    （1.0 还有一条「基础设施未熔断」——2.0 没有熔断器，那条留给以后接守卫时一起加。）
    """
    if state.get("mode") == "build":
        return None
    phases = state.get("phases") or {}
    invalid_phase = any(
        str((bucket or {}).get("status") or "").lower() in {"partial", "failed", "stalled"}
        for bucket in phases.values()
        if isinstance(bucket, dict)
    )
    total = len(items)
    return bool(
        not invalid_phase
        and total > 0
        and total == int(state.get("qa_count") or total)
        and all(item.get("execution_status") == "completed" for item in items)
        and all(item.get("judge_status") == "completed" for item in items)
    )


def effective_summary(state: dict[str, Any]) -> dict[str, Any]:
    """1.0 ``:6662`` 的 ``OrchestratorRepository._effective_summary``。

    **会就地改写 ``items``**（给每个 item 补 ``agent_stability``）——1.0 如此，
    而之后的 ``get_run_items`` 又要用这份补过的数据，所以照搬。
    """
    saved = dict(state.get("summary") or {})
    items = state.get("items") or []
    if not saved.get("image_ranking"):
        saved["image_ranking"] = (
            mean_ranking_metrics(
                [
                    {"image_ranking": ranking_from_item(item)}
                    for item in items
                    if isinstance(item, dict) and retrieval_metric_eligible(item)
                ]
            )
            or None
        )
    for item in items:
        if isinstance(item, dict) and item.get("execution_trace"):
            item["agent_stability"] = agent_stability(item)
    recalls = [
        float(value) for item in items if isinstance((value := item.get("retrieval_recall")), (int, float))
    ]
    scores = [score for item in items if (score := judge_score_for_summary(item.get("judge"))) is not None]
    calls = [
        call for item in items for call in (item.get("model_call_metrics") or []) if isinstance(call, dict)
    ]
    prompts = [
        float(value)
        for call in calls
        if isinstance((value := call.get("preflight_prompt_tokens", call.get("prompt_tokens"))), (int, float))
    ]
    completions = [
        float(value) for call in calls if isinstance((value := call.get("completion_tokens")), (int, float))
    ]
    contexts = [
        float(prompt) + float(completion)
        for call in calls
        if isinstance(
            (prompt := call.get("preflight_prompt_tokens", call.get("prompt_tokens"))), (int, float)
        )
        and isinstance((completion := call.get("completion_tokens")), (int, float))
    ]
    ttfts = [float(value) for call in calls if isinstance((value := call.get("ttft_ms")), (int, float))]
    rates = [
        float(value) for call in calls if isinstance((value := call.get("tokens_per_second")), (int, float))
    ]
    distribution = {str(score): scores.count(score) for score in (0, 1, 2)}
    attributions = [item.get("attribution") or derive_attribution(item) for item in items]
    attribution_primary: dict[str, int] = {}
    attribution_layers: dict[str, int] = {}
    for attribution in attributions:
        primary = attribution.get("primary")
        if primary:
            attribution_primary[str(primary)] = attribution_primary.get(str(primary), 0) + 1
        for key, value in (attribution.get("layers") or {}).items():
            if value == "fail":
                attribution_layers[str(key)] = attribution_layers.get(str(key), 0) + 1
    saved.update(
        {
            "total": saved.get("total", state.get("qa_count") or len(items)),
            # **按执行状态数，不按 judge 状态数。** 1.0 ``:7286`` 用的是
            # ``judge_status in {completed, failed, skipped}``，而取消掉的题在 2.0 里
            # 正是被标成 ``skipped`` 的那一批（``_phase_qa_eval`` 的失败分支）——于是
            # 一个取消的 run 在列表页显示「487/487 完成」，实际只有 332 题有答案。
            # 1.0 极少遇到取消，2.0 已经遇到两次（10-08、10-10）。
            "completed": sum(1 for item in items if item.get("execution_status") == "completed"),
            # 读侧重算，理由见 ``_run_valid``。
            "run_valid": _run_valid(state, items),
            "judge_valid_count": len(scores),
            "judge_distribution": distribution,
            "retrieval_recall_mean": saved.get(
                "retrieval_recall_mean", round(sum(recalls) / len(recalls), 3) if recalls else None
            ),
            "answer_quality_mean": round(sum(scores) / len(scores), 3) if scores else None,
            "exact_accuracy": round(distribution["2"] / len(scores), 3) if scores else None,
            "core_accuracy": round((distribution["1"] + distribution["2"]) / len(scores), 3)
            if scores
            else None,
            "llm_ttft_ms_mean": saved.get(
                "llm_ttft_ms_mean", round(sum(ttfts) / len(ttfts), 1) if ttfts else None
            ),
            "llm_tokens_per_second_mean": saved.get(
                "llm_tokens_per_second_mean", round(sum(rates) / len(rates), 1) if rates else None
            ),
            "prompt_tokens_total": (
                saved.get("prompt_tokens_total")
                if saved.get("prompt_tokens_total") is not None
                else sum(prompts)
            )
            if calls
            else None,
            "completion_tokens_total": (
                saved.get("completion_tokens_total")
                if saved.get("completion_tokens_total") is not None
                else sum(completions)
            )
            if calls
            else None,
            "llm_prompt_tokens_max": saved.get("llm_prompt_tokens_max", max(prompts) if prompts else None),
            "llm_prompt_tokens_p95": saved.get("llm_prompt_tokens_p95", percentile(prompts, 0.95)),
            "llm_completion_tokens_max": saved.get(
                "llm_completion_tokens_max", max(completions) if completions else None
            ),
            "llm_completion_tokens_p95": saved.get(
                "llm_completion_tokens_p95", percentile(completions, 0.95)
            ),
            "llm_context_tokens_max": saved.get(
                "llm_context_tokens_max", max(contexts) if contexts else None
            ),
            "llm_context_tokens_p95": saved.get("llm_context_tokens_p95", percentile(contexts, 0.95)),
            "llm_context_samples_count": saved.get("llm_context_samples_count", len(contexts)),
            "tool_performance": aggregate_tool_performance(items),
            "attribution": {"primary": attribution_primary, "layer_failures": attribution_layers},
            "delivery_breakdown": saved.get("delivery_breakdown", aggregate_delivery(items)),
            # 历史 run 没有 graph_* 遥测时仍输出题型表，但行内会明确标记
            # ``telemetry_status=not_recorded``，不把普通检索结果冒充图拓展结果。
            "graph_qa_by_type": aggregate_graph_qa_by_type(items),
        }
    )
    for key, value in capability_summary(items, state.get("phases") or {}).items():
        if (
            key == "retrieval_recall_mean"
            or key.startswith(("retrieval_", "media_retrieval_", "image_retrieval_", "video_retrieval_"))
            or key.startswith(
                (
                    "json_parse_",
                    "tool_argument_json_",
                    "agent_",
                    "judge_phase_",
                    "qa_completion_",
                    "completed_within_steps",
                )
            )
            or saved.get(key) is None
        ):
            saved[key] = value
    saved["metrics_contract_version"] = 2
    if items or saved.get("benchmark_e2e_latency_excluding_judge_ms") is None:
        saved["benchmark_e2e_latency_excluding_judge_ms"] = benchmark_e2e_latency_excluding_judge_ms(
            state.get("phases") or {},
            items,
        )
    return saved


# ---------------------------------------------------------------------------
# 列表条目与 item 投影（1.0 ``:5309`` / ``:6559`` / ``:4919``）
# ---------------------------------------------------------------------------


def run_list_entry(state: dict[str, Any]) -> dict[str, Any]:
    """1.0 ``:5309`` 的 ``OrchestratorRepository._run_list_entry``。"""
    summary = list_summary(state)
    summary_keys = {
        "total",
        "completed",
        "judge_valid_count",
        "judge_distribution",
        "retrieval_recall_mean",
        "retrieval_recall_macro",
        "media_retrieval_recall_macro",
        "answer_quality_mean",
    }
    result: dict[str, Any] = {
        key: state.get(key)
        for key in (
            "run_id",
            "mode",
            "scope_source",
            "scope_build",
            "scope_id",
            "scope_name",
            "album_id",
            "model_profile",
            "model_name",
            "endpoint_model",
            "model_identity",
            "execution_budgets",
            "snapshot_source",
            "build_revision",
            "model_source",
            "model_backend",
            "qa_set",
            "qa_count",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "failed_phase",
            "fatal_error",
            "telemetry_source",
            "runtime_framework",
            "persistence",
        )
        if state.get(key) is not None
    }
    result["item_count"] = len(state.get("items") or [])
    result["summary"] = {key: summary.get(key) for key in summary_keys if summary.get(key) is not None}
    result["summary"]["run_valid"] = summary.get("run_valid")
    qa_phase: dict[str, Any] = (state.get("phases") or {}).get("qa_eval") or {}
    if qa_phase:
        result["phases"] = {
            "qa_eval": {
                key: qa_phase.get(key) for key in ("status", "progress") if qa_phase.get(key) is not None
            }
        }
    rejudge: dict[str, Any] = state.get("rejudge") or {}
    if rejudge:
        result["rejudge"] = {
            key: rejudge.get(key)
            for key in (
                "status",
                "completed",
                "total",
                "failed",
                "rejudge_id",
            )
            if rejudge.get(key) is not None
        }
    return result


def item_summary(item: dict[str, Any], index: int, review: dict[str, Any] | None = None) -> dict[str, Any]:
    """1.0 ``:6559`` 的 ``_item_summary``。"""
    judge = judge_projection(item.get("judge"))
    return {
        "index": index,
        "qa_id": item.get("qa_id"),
        "question": item.get("question"),
        "task_type": item.get("task_type"),
        "question_type": item.get("question_type"),
        "tags": item.get("tags") or [],
        "angle": item.get("angle"),
        "difficulty": item.get("difficulty"),
        "answerability": item.get("answerability"),
        "retrieval_recall": item.get("retrieval_recall"),
        "retrieval_precision": item.get("retrieval_precision"),
        "retrieval_f1": item.get("retrieval_f1"),
        "media_retrieval_recall": item.get("media_retrieval_recall"),
        "media_retrieval_precision": item.get("media_retrieval_precision"),
        "media_retrieval_f1": item.get("media_retrieval_f1"),
        "image_retrieval_recall": item.get("image_retrieval_recall"),
        "image_retrieval_precision": item.get("image_retrieval_precision"),
        "image_retrieval_f1": item.get("image_retrieval_f1"),
        "video_retrieval_recall": item.get("video_retrieval_recall"),
        "video_retrieval_precision": item.get("video_retrieval_precision"),
        "video_retrieval_f1": item.get("video_retrieval_f1"),
        "matched_count": len(item.get("matched_file_names") or []),
        "ground_truth_count": len(item.get("retrieval_media_refs") or item.get("retrieval_image_ids") or []),
        "ground_truth_image_count": len(item.get("retrieval_image_ids") or []),
        "ground_truth_video_count": len(item.get("retrieval_video_ids") or []),
        "judge": {
            "score": judge.get("score"),
            "status": judge.get("status"),
            "rejudge_id": judge.get("rejudge_id"),
            "consistency_status": judge_consistency_status(judge),
            "consistency_version": judge.get("consistency_version"),
            **{
                key: judge[key]
                for key in ("score_normalization_version", "score_normalization", "score_derivation")
                if key in judge
            },
        },
        "task_judge": item.get("task_judge") or {},
        "task_judges": item.get("task_judges") or [],
        "evidence_judge": item.get("evidence_judge") or {},
        "agent_stability": item.get("agent_stability") or {},
        "agent_status": item.get("agent_status"),
        "termination_reason": item.get("termination_reason"),
        "has_error": bool(item.get("error")),
        "deterministic_delivery": ((item.get("guard_debug") or {}).get("deterministic_delivery") or {}),
        "delivery_status": item.get("delivery_status") or {},
        "model_call_count": len(item.get("model_call_metrics") or []),
        "tool_call_count": len(item.get("tool_trace") or []),
        "attribution": item.get("attribution") or {},
        "review": review,
    }


def hydrate_qa_metadata(item: dict[str, Any], qa_metadata: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """1.0 ``:4919`` 的 ``_hydrate_qa_metadata``；``qa_metadata`` 从 ``self`` 变成入参。"""
    metadata = qa_metadata.get(str(item.get("qa_id") or "")) or {}
    hydrated = {**metadata, **item}
    tag_fields = {
        "task_type": "task",
        "question_type": "question",
        "angle": "angle",
        "difficulty": "difficulty",
        "answerability": "answerability",
        "expected_action": "action",
    }
    tags = [str(tag).strip() for tag in (hydrated.get("tags") or []) if str(tag).strip()]
    for field, prefix in tag_fields.items():
        value = str(hydrated.get(field) or "").strip()
        if value:
            tags.append(f"{prefix}:{value}")
    hydrated["tags"] = list(dict.fromkeys(tags))
    judge = judge_projection(hydrated.get("judge"))
    if judge:
        hydrated["judge"] = judge
    for key in ("evidence_judge",):
        if hydrated.get(key):
            hydrated[key] = judge_projection(hydrated[key])
    consistency_status = judge_consistency_status(judge)
    if consistency_status:
        hydrated["judge"] = {**judge, "consistency_status": consistency_status}
    # P/R/F1 是确定性的：老 run 只有在 GT 与实际召回的文件名都留下时才重算。
    gt_names = {Path(str(value)).name for value in hydrated.get("retrieval_image_ids") or []}
    predicted_names = {Path(str(value)).name for value in hydrated.get("predicted_file_names") or []}
    if gt_names and "predicted_file_names" in hydrated:
        matched = gt_names & predicted_names
        precision = len(matched) / len(predicted_names) if predicted_names else 0.0
        recall = len(matched) / len(gt_names)
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        if hydrated.get("retrieval_precision") is None:
            hydrated["retrieval_precision"] = precision
        if hydrated.get("retrieval_recall") is None:
            hydrated["retrieval_recall"] = recall
        if hydrated.get("retrieval_f1") is None:
            hydrated["retrieval_f1"] = f1
    if not hydrated.get("attribution"):
        hydrated["attribution"] = derive_attribution(hydrated)
    hydrated["tool_trace"] = bind_tool_calls_to_model_rounds(
        hydrated.get("tool_trace") or [],
        hydrated.get("execution_trace") or [],
        hydrated.get("model_call_metrics") or [],
    )
    hydrated["model_call_metrics"] = annotate_model_calls(
        hydrated.get("model_call_metrics") or [], hydrated.get("execution_trace") or []
    )
    agent_steps = [
        step
        for step in hydrated.get("execution_trace") or []
        if isinstance(step, dict)
        and str(step.get("stage") or step.get("type") or "") == "model"
        and str(step.get("call_type") or "agent") in {"agent", "recovery"}
    ]
    if agent_steps:
        hydrated["agent_stability"] = agent_stability(hydrated)
    return hydrated


# ---------------------------------------------------------------------------
# 遥测采样历史（1.0 ``:5364-5488``）
# ---------------------------------------------------------------------------

GPU_SAMPLE_FIELDS = (
    "temperature_c",
    "cpu_temperature_c",
    "gpu_utilization_pct",
    "memory_used_mib",
    "gpu_allocated_unified_memory_mib",
    "gpu_in_use_unified_memory_mib",
    "model_process_memory_used_mib",
    "kv_cache_usage_pct",
    "model_process_system_memory_used_mib",
    "model_process_host_memory_mib",
    "model_process_gpu_memory_mib",
    "model_process_gpu_memory_source",
    "benchmark_process_memory_used_mib",
    "benchmark_process_gpu_memory_mib",
    "kv_cache_used_tokens",
    "power_draw_w",
    "sm_clock_mhz",
    "other_processes_memory_mib",
    "all_processes_memory_mib",
    "system_memory_used_mib",
    "system_memory_total_mib",
    "system_memory_delta_mib",
    "sentrix_stack_pss_mib",
    "sentrix_product_pss_mib",
    "sentrix_auxiliary_model_pss_mib",
    "sentrix_auxiliary_model_uma_mib",
    "product_stack_memory_mib",
    "model_process_footprint_mib",
    "model_process_mapped_weights_mib",
    "product_stack_footprint_mib",
    "product_stack_mapped_weights_mib",
)


def validated_telemetry_sample(sample: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply identical validity rules to fresh and historical observations; never mutate raw data."""
    result, rejected = device_freshness(sample)
    apple = (
        sample.get("memory_accounting") == "apple_capacity_estimate"
        or sample.get("source") == "apple_unified"
    )
    for key, value in sample.items():
        invalid = isinstance(value, (int, float)) and not math.isfinite(value)
        if apple and isinstance(value, (int, float)):
            invalid |= key == "power_draw_w" and apple_power(value) is None
            invalid |= key in {"temperature_c", "cpu_temperature_c"} and apple_temperature(value) is None
        if key in GPU_AGGREGATE_KEYS and isinstance(value, (int, float)) and key != "system_memory_delta_mib":
            invalid |= value < 0
        if invalid:
            result[key] = None
            rejected.append(key)
    result, memory_rejected = memory_freshness(result)
    return result, list(dict.fromkeys([*rejected, *memory_rejected]))


def telemetry_sampling(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Coverage is described by actual sample intervals, without inventing missing observations."""
    times = sorted(
        {
            float(s.get("t", s.get("_t", 0)))
            for s in samples
            if finite_number(s.get("t", s.get("_t"))) is not None
        }
    )
    intervals = [b - a for a, b in zip(times, times[1:], strict=False) if b > a]
    phases: dict[str, Any] = {}
    for sample in samples:
        sample, _ = memory_freshness(sample)
        phase = str(sample.get("phase") or "unassigned")
        row = phases.setdefault(phase, {"samples": 0, "core_memory_valid": 0})
        row["samples"] += 1
        row["core_memory_valid"] += int(
            all(
                (value := finite_number(sample.get(key))) is not None and value >= 0
                for key in (
                    "system_memory_used_mib",
                    "model_process_system_memory_used_mib",
                    "product_stack_memory_mib",
                )
            )
        )
    return {
        "elapsed_seconds": times[-1] - times[0] if times else None,
        "interval_seconds": series_stats([{"value": value} for value in intervals], "value"),
        "gaps_over_3_seconds": sum(value > 3 for value in intervals),
        "phases": phases,
    }


def series_stats(history: Sequence[dict[str, Any]], key: str) -> dict[str, Any]:
    """1.0 ``:5364`` 的 ``_series_stats``。"""
    values = [
        float(point[key])
        for point in history
        if isinstance(point.get(key), (int, float))
        and not isinstance(point[key], bool)
        and math.isfinite(point[key])
    ]
    if not values:
        return {}
    ordered = sorted(values)
    count = len(ordered)
    return {
        "peak": ordered[-1],
        "mean": round(sum(values) / count, 1),
        "p50": ordered[count // 2],
        "p95": ordered[max(0, math.ceil(count * 0.95) - 1)],
        "valid_count": count,
        "mean_kind": "sample_mean",
        "peak_kind": "sampled_peak",
    }


def downsample_history(history: Sequence[dict[str, Any]], limit: int = 240) -> list[dict[str, Any]]:
    """1.0 ``:5378`` 的 ``_downsample_history``：阶段切换点必须保留。"""
    count = len(history)
    if count <= max(limit, 2):
        return list(history)
    chosen = {0, count - 1}
    for index in range(1, count):
        if history[index].get("phase") != history[index - 1].get("phase"):
            chosen.update((index - 1, index))
    remaining = max(0, limit - len(chosen))
    candidates = [index for index in range(count) if index not in chosen]
    for index in range(remaining):
        chosen.add(candidates[min(len(candidates) - 1, int((index + 0.5) * len(candidates) / remaining))])
    return [history[index] for index in sorted(chosen)]


def publish_full_sample_phase_stats(result: dict[str, Any], live: dict[str, Any]) -> None:
    """1.0 ``:5395`` 的 ``_publish_full_sample_phase_stats``。"""
    stats = live.pop("_full_sample_stats", None) or {}
    phases = result.get("phases")
    if not isinstance(phases, dict) or not stats:
        return
    gpu = phases.get("gpu_metrics")
    if not isinstance(gpu, dict):
        return
    phases = dict(phases)
    gpu = dict(gpu)
    for key, value in stats.items():
        if value:
            gpu[key] = value
    phases["gpu_metrics"] = gpu
    result["phases"] = phases


def _is_telemetry_series_key(key: str) -> bool:
    """曲线键：固定清单之外的 ``runtime_*`` / ``kv_cache_*`` 也自动收。

    1.0 ``:2021`` 有同样的动态兜底。2.0 的 vLLM provider 目前只产出
    ``kv_cache_usage_pct``，但少了这层兜底，以后 provider 补字段时会在读侧
    静默丢——那正是「字段算出来却没进曲线」的一类 bug。
    """
    return (
        key in GPU_SAMPLE_FIELDS
        or key.startswith(("runtime_", "kv_cache_"))
        or key
        in {
            "memory_accounting",
            "memory_complete",
            "memory_unit",
            "source",
            "source_status",
            "source_diagnostics",
            "observed_at_epoch",
            "component_validity",
            "memory_observation_version",
            "memory_observations",
            "memory_dependencies",
            "sampling_diagnostics",
            "process_observed_at_monotonic",
            "device_observation_age_seconds",
            "device_observed_at_monotonic",
            "device_observation_ttl_seconds",
            "device_freshness_version",
            "metrics_contract_version",
            "power_scope",
            "power_rail",
            "power_rails_w",
            "power_rails_average_w",
            "model_process_footprint_mib",
            "model_process_mapped_weights_mib",
            "product_stack_footprint_mib",
            "product_stack_mapped_weights_mib",
        }
    )


def attach_gpu_sample_history(live: dict[str, Any], lines: Sequence[str]) -> None:
    """1.0 ``:5411`` 的 ``_attach_gpu_sample_history``；入参从文件路径换成原始行。

    坏行跳过、整段异常兜底成空表——1.0 的 ``except Exception`` 口径照搬：遥测是增强，
    它读坏了不该让整个 run 打不开。
    """
    history: list[dict[str, Any]] = []
    latest: dict[str, Any] = {}
    peak: dict[str, Any] = {}
    rejected: dict[str, int] = {}
    try:
        for line in lines:
            try:
                item = json.loads(line)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(item, dict):
                continue
            item, invalid = validated_telemetry_sample(item)
            for key in invalid:
                rejected[key] = rejected.get(key, 0) + 1
            point = {k: item[k] for k in item if _is_telemetry_series_key(k)}
            if point:
                history.append(
                    {
                        "t": item.get("_t", time.time()),
                        "phase": item.get("phase", "unassigned"),
                        "phase_label": item.get("phase_label", "未分配阶段"),
                        **point,
                    }
                )
                latest = point
                for key, value in point.items():
                    if (
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and key in GPU_AGGREGATE_KEYS
                    ):
                        peak[key] = max(float(peak.get(key, value)), float(value))
        if history:
            raw_scopes = live.get("scopes")
            scopes: dict[str, Any] = raw_scopes if isinstance(raw_scopes, dict) else {}
            if scopes.get("system_memory"):
                latest.setdefault("system_memory_scope", scopes["system_memory"])
                peak.setdefault("system_memory_scope", scopes["system_memory"])
            if scopes.get("benchmark_process_ram"):
                latest.setdefault("benchmark_process_memory_scope", scopes["benchmark_process_ram"])
                peak.setdefault("benchmark_process_memory_scope", scopes["benchmark_process_ram"])
            live["_full_sample_stats"] = {key: series_stats(history, key) for key in GPU_AGGREGATE_KEYS}
            live["field_stats"] = live["_full_sample_stats"]
            live["rejected_samples"] = rejected
            live["sampling"] = telemetry_sampling(history)
            live["history"] = downsample_history(history, limit=240)
            live["latest"] = latest
            live["peak"] = peak
            live["samples_count"] = max(int(live.get("samples_count") or 0), len(history))
        elif not isinstance(live.get("history"), list):
            live["history"] = []
    except Exception:  # noqa: BLE001 —— 1.0 如此：遥测读坏了不能拖垮整个 run 详情
        if not isinstance(live.get("history"), list):
            live["history"] = []


# ---------------------------------------------------------------------------
# 遥测聚合（S9b-3）：把采样点收成曲线统计
# ---------------------------------------------------------------------------


#: 1.0 ``GpuSampler.aggregate``（``:1790``）逐项求 peak/mean/p50/p95 的那些键。
#: 顺序与 1.0 一致（JSON 里键的顺序不影响读，但差分对照要能逐字段比）。
GPU_AGGREGATE_KEYS = (
    "temperature_c",
    "cpu_temperature_c",
    "gpu_utilization_pct",
    "memory_used_mib",
    "gpu_allocated_unified_memory_mib",
    "gpu_in_use_unified_memory_mib",
    "model_process_memory_used_mib",
    "kv_cache_usage_pct",
    "kv_cache_used_tokens",
    "power_draw_w",
    "sm_clock_mhz",
    "other_processes_memory_mib",
    "all_processes_memory_mib",
    "system_memory_used_mib",
    "system_memory_total_mib",
    "model_process_system_memory_used_mib",
    "model_process_host_memory_mib",
    "model_process_gpu_memory_mib",
    "model_process_gpu_memory_source",
    "system_memory_delta_mib",
    "sentrix_stack_pss_mib",
    "sentrix_product_pss_mib",
    "sentrix_auxiliary_model_pss_mib",
    "sentrix_auxiliary_model_uma_mib",
    "product_stack_memory_mib",
    "model_process_footprint_mib",
    "model_process_mapped_weights_mib",
    "product_stack_footprint_mib",
    "product_stack_mapped_weights_mib",
)


def aggregate_gpu_samples(samples: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """1.0 ``GpuSampler.aggregate``（``:1785``）的纯函数版。

    **为什么能从采样器里搬出来。** 1.0 里它读 ``self.samples`` 并 ``isinstance`` 判
    ``self.provider``——那两样都是「采样器的状态」。2.0 把样本表当入参，把那个
    ``isinstance`` 换成读样本自己的 ``source``：``GpuSampler`` 给每条样本打的
    ``source`` 就是 provider 的 ``gpu_stats()["source"]``（``jetson_local_pss`` /
    ``orin_ssh_pss``），**这正是那两个分支唯一要区分的东西**。搬出来之后聚合能在
    没有 provider、没有线程的用例里被逐字段钉住。

    **三条 memory_profile 分支照搬**（PSS-UMA / Apple footprint / 默认的
    idle−reserved+peak 估算）：它们的分母口径不同（UMA 是共享物理内存、不是显存），
    混用会让两台机器上的曲线看起来可比而实际不可比。
    """
    if not samples:
        return {"samples_count": 0}
    samples = [validated_telemetry_sample(sample)[0] for sample in samples]
    keys = set(GPU_AGGREGATE_KEYS) | {
        key for sample in samples for key in sample if key.startswith(("runtime_", "kv_cache_"))
    }
    metrics: dict[str, Any] = {key: stats for key in sorted(keys) if (stats := series_stats(samples, key))}
    metrics["sampling"] = telemetry_sampling(samples)
    latest = next((sample for sample in reversed(samples) if sample.get("memory_accounting")), samples[-1])
    if samples[-1].get("source") is not None:
        metrics["source"] = samples[-1]["source"]
    for key in (
        "metrics_contract_version",
        "memory_accounting",
        "component_validity",
        "memory_observation_version",
        "memory_observations",
        "memory_dependencies",
        "sampling_diagnostics",
        "process_observed_at_monotonic",
        "device_freshness_version",
        "memory_complete",
        "power_scope",
        "power_rail",
    ):
        metrics[key] = (latest if key == "memory_accounting" else samples[-1]).get(key)
    limits = [
        sample.get("model_process_memory_limit_mib")
        for sample in samples
        if isinstance(sample.get("model_process_memory_limit_mib"), (int, float))
    ]
    over_limit_flags = [
        sample.get("model_process_memory_over_limit")
        for sample in samples
        if isinstance(sample.get("model_process_memory_over_limit"), bool)
    ]
    if (
        latest.get("memory_unit") == "host_smaps_rss_plus_gpu_mib"
        or latest.get("memory_accounting") == "jetson_pss_plus_nvmap_estimate"
        or (
            latest.get("source") in {"jetson_local_pss", "orin_ssh_pss"}
            and latest.get("model_process_host_memory_mib") is not None
        )
    ):
        # 新口径：主机 smaps Rss + GPU/UMA 页。旧 run 的 memory_unit 可能还是
        # process_pss_uma_mib，但只要有拆分字段就按新口径解释，避免前端继续显示
        # VmRSS。历史 run 没有拆分字段时仍走下面的 PSS 分支。
        return {
            "samples_count": len(samples),
            "source": latest.get("source"),
            "memory_profile": {
                "method": "orin_smaps_rss_plus_gpu_uma_v1",
                "memory_unit": "host_smaps_rss_plus_gpu_mib",
                "scope": "host_smaps_rss_plus_gpu_uma",
                "kv_cache_used_peak_tokens": (metrics.get("kv_cache_used_tokens") or {}).get("peak"),
                "kv_cache_usage_peak_pct": (metrics.get("kv_cache_usage_pct") or {}).get("peak"),
                "note": (
                    "Host smaps Rss + GPU/UMA pages (nvmap per PID in preference to "
                    "VmRSS - smaps residual); allocation/attribution estimate, not exact physical residency."
                ),
            },
            **metrics,
        }
    if latest.get("memory_unit") == "process_pss_uma_mib":
        pss_sample_count = len(
            {s["pss_sampled_at_monotonic"] for s in samples if s.get("pss_sampled_at_monotonic") is not None}
        )
        return {
            "samples_count": len(samples),
            "pss_samples_count": pss_sample_count,
            # 1.0 这里判的是 provider 的类；2.0 判样本自己的 source——GpuSampler 就是
            # 从 gpu_stats()["source"] 抄下来的，两者是同一个事实。
            "source": latest.get("source"),
            "memory_profile": {
                "method": "orin_process_pss_uma_v1",
                "memory_unit": "process_pss_uma_mib",
                "kv_cache_used_peak_tokens": (metrics.get("kv_cache_used_tokens") or {}).get("peak"),
                "kv_cache_usage_peak_pct": (metrics.get("kv_cache_usage_pct") or {}).get("peak"),
                "kv_cache_used_peak_gib": None,
                "comparable_workload_memory_gib": None,
                "note": (
                    "PSS is shared physical RAM, not dedicated GPU VRAM; "
                    "KV bytes unavailable without verified per-token allocation."
                ),
            },
            **metrics,
        }
    if latest.get("memory_unit") in {
        "apple_phys_footprint_mib",
        "apple_phys_footprint_plus_mapped_weights_mib",
    }:
        # 旧 run 只有 footprint；新 run 是 footprint + 映射权重。两者的
        # method 必须分开，否则历史曲线会被误读成同一口径。
        plus_weights = latest.get("memory_unit") == "apple_phys_footprint_plus_mapped_weights_mib"
        return {
            "samples_count": len(samples),
            "source": "apple_unified",
            "memory_profile": {
                "method": (
                    "apple_phys_footprint_plus_mapped_weights_v1"
                    if plus_weights
                    else "apple_phys_footprint_v1"
                ),
                "memory_unit": latest.get("memory_unit"),
                "comparable_workload_memory_gib": None,
                "note": (
                    "Process occupancy = phys_footprint + mapped model weights; "
                    "not dedicated GPU VRAM. Per-process GPU-only memory and KV bytes are unavailable."
                ),
            },
            **metrics,
        }
    kv_capacity_gib = latest.get("kv_cache_capacity_gib")
    process_memory = metrics.get("model_process_memory_used_mib") or {}
    kv_usage = metrics.get("kv_cache_usage_pct") or {}
    idle_samples: list[Any] = [
        sample.get("model_process_memory_used_mib")
        for sample in samples
        if isinstance(sample.get("model_process_memory_used_mib"), (int, float))
        and isinstance(sample.get("kv_cache_usage_pct"), (int, float))
        and sample.get("kv_cache_usage_pct") == 0
    ]
    idle_process_mib: Any = min(idle_samples) if idle_samples else process_memory.get("p50")
    fixed_base_gib = (
        max(0.0, idle_process_mib / 1024 - kv_capacity_gib)
        if isinstance(idle_process_mib, (int, float)) and isinstance(kv_capacity_gib, (int, float))
        else None
    )
    kv_peak_gib = (
        kv_capacity_gib * float(kv_usage.get("peak") or 0) / 100
        if isinstance(kv_capacity_gib, (int, float)) and kv_usage
        else None
    )
    return {
        "samples_count": len(samples),
        "model_process_memory_limit_mib": limits[-1] if limits else None,
        "model_process_over_limit_samples": sum(over_limit_flags) if over_limit_flags else None,
        "memory_profile": {
            "method": "idle_process_minus_reserved_kv_plus_peak_used_kv_v1",
            "comparable_memory_is_estimate": True,
            "idle_process_memory_gib": round(idle_process_mib / 1024, 4)
            if isinstance(idle_process_mib, (int, float))
            else None,
            "kv_cache_capacity_gib": kv_capacity_gib,
            "kv_cache_capacity_tokens": latest.get("kv_cache_capacity_tokens"),
            "kv_cache_usage_peak_pct": kv_usage.get("peak"),
            "kv_cache_used_peak_gib": round(kv_peak_gib, 4) if kv_peak_gib is not None else None,
            "fixed_base_memory_gib": round(fixed_base_gib, 4) if fixed_base_gib is not None else None,
            "comparable_workload_memory_gib": round(fixed_base_gib + kv_peak_gib, 4)
            if fixed_base_gib is not None and kv_peak_gib is not None
            else None,
            "weight_gib": latest.get("weight_gib"),
            "peak_activation_gib": latest.get("peak_activation_gib"),
            "non_torch_gib": latest.get("non_torch_gib"),
            "cuda_graph_gib": latest.get("cuda_graph_gib"),
            "formula": (
                "fixed_base = idle_process - kv_capacity; comparable = fixed_base + kv_capacity * kv_peak_pct"
            ),
        },
        **metrics,
    }


def apply_live_sample(
    live: dict[str, Any], sample: dict[str, Any], phase: str, phase_label: str, *, tail: int = 240
) -> None:
    """把一条采样并进 run.json 的 ``telemetry_live``（1.0 ``_persist_gpu_sample`` 中段，
    ``:2110``–``:2208``）。

    **它与 ``gpu_samples.jsonl`` 的分工照搬 1.0**：JSONL 是完整曲线的权威来源（读侧
    ``attach_gpu_sample_history`` 从它重建），``telemetry_live`` 只是给实时卡片看的
    滚动视图——所以这里可以按 ``tail`` 截断，而那个文件不行。

    **旧 run 的字段可能是 null/缺项**（1.0 的注释）：三处 ``isinstance`` 归一化是照搬的，
    一个历史字段不该让整条实时视图瘫痪。
    """
    sample, rejected = validated_telemetry_sample(sample)
    counts = live.setdefault("rejected_samples", {})
    for key in rejected:
        counts[key] = counts.get(key, 0) + 1
    if not isinstance(live.get("history"), list):
        live["history"] = []
    if not isinstance(live.get("peak"), dict):
        live["peak"] = {}
    if not isinstance(live.get("phase_snapshots"), dict):
        live["phase_snapshots"] = {}
    live["status"] = "running"
    live["source"] = sample.get("source") or live.get("source")
    live["samples_count"] = int(live.get("samples_count") or 0) + 1
    live["latest"] = {key: value for key, value in sample.items() if _is_telemetry_series_key(key)}
    live["metrics_contract_version"] = 2
    live["memory_accounting"] = sample.get("memory_accounting")
    live["model_processes"] = sample.get("model_processes") or []
    live["model_system_processes"] = sample.get("model_system_processes") or []
    live["benchmark_processes"] = sample.get("benchmark_processes") or []
    live["benchmark_gpu_processes"] = sample.get("benchmark_gpu_processes") or []
    live["other_processes"] = sample.get("other_processes") or []
    live["all_processes"] = sample.get("all_processes") or []
    live["scopes"] = {
        "system_memory": sample.get("system_memory_scope"),
        "gpu_memory": sample.get("memory_scope"),
        "model_process": sample.get("process_memory_scope"),
        "model_process_ram": sample.get("model_process_system_memory_scope"),
        "benchmark_process_ram": sample.get("benchmark_process_memory_scope"),
        "benchmark_process_gpu": sample.get("benchmark_process_gpu_memory_scope"),
        "all_gpu_processes": sample.get("all_processes_scope"),
        "process_attribution": sample.get("process_attribution"),
    }
    live["history"].append(
        {
            "t": sample.get("_t", time.time()),
            "phase": phase,
            "phase_label": phase_label,
            **live["latest"],
        }
    )
    if tail and len(live["history"]) > tail:
        del live["history"][:-tail]
    peak = live.setdefault("peak", {})
    for key in GPU_SAMPLE_FIELDS:
        value = sample.get(key)
        if isinstance(value, (int, float)):
            peak[key] = max(float(peak.get(key, value)), float(value))
    phase_view = live.setdefault("phase_snapshots", {}).setdefault(
        phase,
        {
            "samples_count": 0,
            "latest": {},
            "peak": {},
        },
    )
    phase_view["samples_count"] = int(phase_view.get("samples_count") or 0) + 1
    phase_view["latest"] = live["latest"]
    phase_peak = phase_view.setdefault("peak", {})
    for key in GPU_SAMPLE_FIELDS:
        value = sample.get(key)
        if isinstance(value, (int, float)):
            phase_peak[key] = max(float(phase_peak.get(key, value)), float(value))


def paginate_rows(rows: list[Any], page: int, page_size: int, *, max_page_size: int = 100) -> dict[str, Any]:
    """1.0 ``:779``。``GET /api/runs`` 的分页形状。"""
    page = max(1, int(page))
    page_size = max(1, min(max_page_size, int(page_size)))
    total = len(rows)
    pages = max(1, math.ceil(total / page_size))
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "runs": rows[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "total": total,
        "pages": pages,
        "has_previous": page > 1,
        "has_next": page < pages,
    }

"""图记忆评测的纯计算。

这份模块只消费 run.json、PhotoBench 的 GT 和主 API 的只读投影，不把评测标注带进
Sentrix 运行时。旧版 ``benchmark_orchestrator`` 的三块图指标原来混在编排器里，2.0
把它们拆成纯函数，方便独立测试，也方便后续替换图构建模块。
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

FACE_CLUSTERING_METRIC_VERSION = 5

QUESTION_TYPE_LABELS = {
    "event_memory_qa": "事件记忆",
    "single_evidence_memory_qa": "单证据记忆",
    "multi_turn_clarify": "多轮澄清",
    "time_sequence": "时间/时序",
    "location_scene": "地点/场景",
    "visual_scene_understanding": "视觉/场景理解",
    "video_media": "视频/媒体",
    "evidence_insufficient": "证据不足",
    "prompt_injection": "提示注入",
    "multi_hop": "多跳/因果",
}


def _walk(value: object) -> Iterator[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _graph_trace(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """找到 search_memories 的图遥测；兼容旧版 graph_rerank 轨迹。"""

    for node in _walk(item.get("tool_trace") or []):
        if "graph_primary_asset_ids" in node or "graph_rerank" in node:
            return node
        result = node.get("result")
        if isinstance(result, Mapping) and ("graph_primary_asset_ids" in result or "graph_rerank" in result):
            return {str(key): value for key, value in result.items()}
    return None


def _gt_asset_ids(item: Mapping[str, Any]) -> set[str]:
    values: list[object] = []
    for key in ("gt_media", "gt_images"):
        values.extend(item.get(key) or [])
    return {
        str(entry.get("asset_id")) for entry in values if isinstance(entry, Mapping) and entry.get("asset_id")
    }


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _judge_score(value: object) -> float | None:
    if not isinstance(value, Mapping):
        return None
    score = value.get("score")
    return float(score) if isinstance(score, (int, float)) else None


def aggregate_graph_qa_by_type(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按题型汇总检索、回答质量和图拓展带来的 GT 召回变化。

    没有 ``graph_*`` 遥测的历史 run 仍然会出表，但 ``graph_effect_count`` 为 0，
    由 ``telemetry_status`` 明确标成 ``not_recorded``，而不是把普通检索结果冒充成
    图检索结果。
    """

    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = str(item.get("question_type") or item.get("task_type") or "other")
        buckets.setdefault(key, []).append(item)

    rows: list[dict[str, Any]] = []
    for key, bucket in buckets.items():
        recalls = [
            float(value)
            for item in bucket
            if isinstance(
                (value := item.get("media_retrieval_recall", item.get("retrieval_recall"))),
                (int, float),
            )
        ]
        precisions = [
            float(value)
            for item in bucket
            if isinstance(
                (value := item.get("media_retrieval_precision", item.get("retrieval_precision"))),
                (int, float),
            )
        ]
        f1s = [
            float(value)
            for item in bucket
            if isinstance((value := item.get("media_retrieval_f1", item.get("retrieval_f1"))), (int, float))
        ]
        judge_scores = [score for item in bucket if (score := _judge_score(item.get("judge"))) is not None]
        evidence_scores = [
            float(score)
            for item in bucket
            if isinstance((score := (item.get("evidence_judge") or {}).get("score")), (int, float))
        ]
        traces = [trace for item in bucket if (trace := _graph_trace(item)) is not None]
        graph_enabled = 0
        intents: dict[str, int] = {}
        deltas: list[float] = []
        improved = worsened = rank_changed = set_changed = 0

        for item, trace in ((item, _graph_trace(item)) for item in bucket):
            if trace is None:
                continue
            channels = trace.get("retrieval_channels") or []
            graph_channel = next(
                (
                    channel
                    for channel in channels
                    if isinstance(channel, Mapping) and channel.get("channel") == "graph"
                ),
                None,
            )
            if isinstance(graph_channel, Mapping) and graph_channel.get("status") == "ok":
                graph_enabled += 1
            policy = trace.get("graph_policy") or {}
            if isinstance(policy, Mapping):
                intent = str(policy.get("intent") or "ordinary")
                intents[intent] = intents.get(intent, 0) + 1

            baseline = trace.get("graph_primary_asset_ids") or []
            final = trace.get("graph_fused_asset_ids") or []
            if not baseline and isinstance(trace.get("graph_rerank"), Mapping):
                rerank = trace["graph_rerank"]
                baseline = (
                    rerank.get("baseline_head_asset_ids")
                    or (rerank.get("baseline_ranked_asset_ids") or [])[:18]
                )
                final = rerank.get("returned_head_asset_ids") or (rerank.get("reranked_asset_ids") or [])[:18]
            gt = _gt_asset_ids(item)
            # 空的 baseline/final 也是有效观测：例如 primary 没召回、或图扩展把
            # 条件结果全部过滤掉时，必须把它计为 0 召回，而不能静默丢掉这道题。
            if not gt:
                continue
            baseline = [str(asset_id) for asset_id in baseline]
            final = [str(asset_id) for asset_id in final]
            if baseline != final:
                rank_changed += 1
            if set(baseline) != set(final):
                set_changed += 1
            delta = len(set(final) & gt) / len(gt) - len(set(baseline) & gt) / len(gt)
            deltas.append(delta)
            if delta > 1e-9:
                improved += 1
            elif delta < -1e-9:
                worsened += 1

        rows.append(
            {
                "type": key,
                "label": QUESTION_TYPE_LABELS.get(key, key),
                "sample_count": len(bucket),
                "retrieval_metric_count": len(recalls),
                "graph_routed_count": len(traces),
                "graph_enabled_count": graph_enabled,
                "graph_intents": intents,
                "graph_effect_count": len(deltas),
                "graph_recall_delta": _mean(deltas),
                "graph_gt_improved_count": improved,
                "graph_gt_worsened_count": worsened,
                "graph_rank_changed_count": rank_changed,
                "graph_candidate_set_changed_count": set_changed,
                "telemetry_status": "recorded" if traces else "not_recorded",
                "retrieval_recall": _mean(recalls),
                "retrieval_precision": _mean(precisions),
                "retrieval_f1": _mean(f1s),
                "answer_quality_mean": (
                    round(sum(judge_scores) / len(judge_scores), 3) if judge_scores else None
                ),
                "judge_valid_count": len(judge_scores),
                "evidence_valid_count": len(evidence_scores),
                "evidence_supported_rate": round(
                    sum(1 for score in evidence_scores if score >= 2) / len(evidence_scores), 4
                )
                if evidence_scores
                else None,
            }
        )
    return sorted(rows, key=lambda row: (-row["sample_count"], row["type"]))


def _metrics(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {
        "available": bool(tp + fp + fn),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
    }


def _image_link_metrics(
    image_to_face_ids: Mapping[str, object],
    predicted_clusters_by_file: Mapping[str, set[str]],
    asset_names: set[str],
) -> dict[str, Any]:
    samples: list[tuple[set[str], set[str]]] = []
    gt_asset_count = detected_image_count = clustered_image_count = 0
    for name, labels in image_to_face_ids.items():
        file_name = Path(str(name)).name.lower()
        if file_name not in asset_names or not isinstance(labels, list) or not labels:
            continue
        gt_asset_count += 1
        clusters = {str(value) for value in predicted_clusters_by_file.get(file_name, set()) if value}
        if file_name in predicted_clusters_by_file:
            detected_image_count += 1
            clustered_image_count += bool(clusters)
        # 未检测到的人脸也必须进入 pairwise 分母；否则漏检只会减少样本数，
        # 不会产生 false negative，最终 link recall 会被系统性高估。
        samples.append(({str(value) for value in labels}, clusters))
    tp = fp = fn = tn = 0
    for left, right in itertools.combinations(samples, 2):
        gt_same = bool(left[0] & right[0])
        predicted_same = bool(left[1] & right[1])
        if gt_same and predicted_same:
            tp += 1
        elif predicted_same:
            fp += 1
        elif gt_same:
            fn += 1
        else:
            tn += 1
    result = _metrics(tp, fp, fn)
    result.update(
        {
            "true_negative": tn,
            "evaluable_pair_count": len(samples) * (len(samples) - 1) // 2,
            "evaluable_image_count": len(samples),
            "gt_asset_count": gt_asset_count,
            "detected_image_count": detected_image_count,
            "clustered_image_count": clustered_image_count,
        }
    )
    return result


def _identity_gallery_metrics(
    image_to_face_ids: Mapping[str, object],
    predicted_identities_by_file: Mapping[str, set[str]],
    asset_names: set[str],
) -> dict[str, Any]:
    samples: list[tuple[set[str], set[str]]] = []
    gt_asset_count = identified_image_count = 0
    for name, labels in image_to_face_ids.items():
        file_name = Path(str(name)).name.lower()
        if file_name not in asset_names or not isinstance(labels, list) or not labels:
            continue
        gt_asset_count += 1
        predicted = {str(value) for value in predicted_identities_by_file.get(file_name, set()) if value}
        identified_image_count += bool(predicted)
        # 没有预测身份时保留空集合，所有 GT identity 都应计为 false negative。
        samples.append(({str(value) for value in labels}, predicted))
    tp = sum(len(gt & predicted) for gt, predicted in samples)
    fp = sum(len(predicted - gt) for gt, predicted in samples)
    fn = sum(len(gt - predicted) for gt, predicted in samples)
    result = _metrics(tp, fp, fn)
    result.update(
        {
            "evaluable_image_count": len(samples),
            "gt_asset_count": gt_asset_count,
            "identified_image_count": identified_image_count,
        }
    )
    return result


def face_clustering_quality(
    clusters: list[dict[str, Any]],
    image_to_face_ids: Mapping[str, object] | None,
    asset_names: set[str] | None = None,
) -> dict[str, Any]:
    """按旧版 v4 口径计算身份 gallery 与图像级 identity-link，并修正漏检分母。

    ``asset_names`` 是当前 scope 实际导入的文件名集合。没有它时为兼容独立纯函数
    调用，回退到预测簇里的文件名；正式报告必须传入资产清单，否则“完全没检测到脸”
    的 GT 图片会从分母中消失。
    """

    if not image_to_face_ids:
        return {
            "available": False,
            "reason": "benchmark identity GT not found",
            "metric_definition_version": FACE_CLUSTERING_METRIC_VERSION,
        }
    if asset_names is None:
        asset_names = {
            Path(str(sample.get("file_name") or "")).name.lower()
            for cluster in clusters
            for sample in (cluster.get("samples") or [])
            if sample.get("file_name")
        }
    else:
        asset_names = {Path(str(name)).name.lower() for name in asset_names if str(name).strip()}
    raw_by_file: dict[str, list[tuple[str, str | None]]] = {}
    verified_by_file: dict[str, list[tuple[str, str | None]]] = {}
    for cluster in clusters:
        cluster_id = str(cluster.get("id") or "")
        for sample in cluster.get("samples") or []:
            file_name = Path(str(sample.get("file_name") or "")).name.lower()
            face_id = str(sample.get("face_instance_id") or "")
            if not file_name or not face_id:
                continue
            pair = (face_id, cluster_id or None)
            raw_by_file.setdefault(file_name, []).append(pair)
            if str(sample.get("validity") or "").lower() == "verified":
                verified_by_file.setdefault(file_name, []).append(pair)

    seed_cluster_by_identity: dict[str, str] = {}
    for file_name, detected in raw_by_file.items():
        stem = Path(file_name).stem
        if not stem.lower().startswith("faceid_"):
            continue
        cluster_ids = {cluster_id for _, cluster_id in detected if cluster_id}
        if len(cluster_ids) == 1:
            seed_cluster_by_identity[stem[7:]] = next(iter(cluster_ids))
    identity_by_seed_cluster = {cluster: identity for identity, cluster in seed_cluster_by_identity.items()}
    predicted_identities = {
        file_name: {
            identity_by_seed_cluster[cluster_id]
            for _, cluster_id in detected
            if cluster_id in identity_by_seed_cluster
        }
        for file_name, detected in verified_by_file.items()
    }
    predicted_clusters = {
        file_name: {cluster_id for _, cluster_id in detected if cluster_id}
        for file_name, detected in verified_by_file.items()
    }
    gallery = _identity_gallery_metrics(image_to_face_ids, predicted_identities, asset_names)
    links = _image_link_metrics(image_to_face_ids, predicted_clusters, asset_names)
    gt_single = {
        Path(str(name)).name.lower(): str(labels[0])
        for name, labels in image_to_face_ids.items()
        if isinstance(labels, list) and len(labels) == 1
    }
    evaluable_gt_single = {name for name in gt_single if name in asset_names}
    detected_single = sum(1 for name in evaluable_gt_single if len(verified_by_file.get(name, [])) == 1)
    quality = {
        "available": bool(gallery.get("available") or links.get("available")),
        "metric_definition_version": FACE_CLUSTERING_METRIC_VERSION,
        "identity_gallery": gallery,
        "image_identity_links": links,
        "gt_single_image_count": len(gt_single),
        "single_face_detection_rate": (
            round(detected_single / len(evaluable_gt_single), 4) if evaluable_gt_single else None
        ),
        "asset_name_coverage_count": sum(1 for name in gt_single if name in asset_names),
        "evaluable_gt_single_image_count": len(evaluable_gt_single),
        "identity_gallery_seed_count": len(seed_cluster_by_identity),
        "verified_detection_image_count": len(verified_by_file),
        "gt_multi_identity_image_count": sum(
            1
            for name, labels in image_to_face_ids.items()
            if Path(str(name)).name.lower() in asset_names and isinstance(labels, list) and len(labels) != 1
        ),
    }
    return quality

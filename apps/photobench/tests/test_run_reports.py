"""``domain/runs/{keyframe_analysis,memory_effectiveness,exports}`` 的用例。

这一层没有 IO（资产清单与视频元数据都由调用方传进来），所以每条断言都定点可杀。
**每个夹具都让两种口径给出不同答案**——这是本仓库反复踩过的坑：一个断言如果在
「血缘对上」与「血缘没对上」两种情况下都通过，它就证明不了任何事。

命名的 ``..._changes_the_answer`` 那一组就是这条规矩的落地：同一份 run 数据，
只换资产清单里的一条父级关系，结论必须翻转。
"""

from __future__ import annotations

from photobench.domain.runs import exports, keyframe_analysis, memory_effectiveness


def _video(asset_id: str, file_name: str = "clip.mp4") -> dict:
    return {"id": asset_id, "file_name": file_name, "media_type": "video", "metadata_json": {}}


def _keyframe(asset_id: str, parent: str, stamp: float | None = None, reason: str | None = None) -> dict:
    meta: dict = {"derived_kind": "video_keyframe", "parent_asset_id": parent}
    if stamp is not None:
        meta["source_timestamp_sec"] = stamp
    if reason is not None:
        meta["keyframe_selection_reason"] = reason
    return {
        "id": asset_id,
        "file_name": f"{asset_id}.jpg",
        "media_type": "image",
        "derived_kind": "video_keyframe",
        "parent_asset_id": parent,
        "source_timestamp_sec": stamp,
        "metadata_json": meta,
    }


def _state(**over: object) -> dict:
    state = {"run_id": "run-1", "scope_id": "scope-1", "items": []}
    state.update(over)
    return state


# ---- keyframe_analysis：命中口径 -------------------------------------------


def _one_item(candidate: list[dict], predicted: list[dict] | None = None, score: int | None = 2) -> dict:
    item: dict = {
        "gt_media": [{"asset_id": "v1", "media_type": "video"}],
        "retrieved_candidate_media": candidate,
        "predicted_media": predicted or [],
    }
    if score is not None:
        item["judge"] = {"score": score}
    return item


def test_keyframe_of_the_gt_video_counts_as_hitting_that_video():
    """1.0 的设计：返回关键帧而不是源片**不算没召回**。"""
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[_one_item([{"asset_id": "k1"}])]),
        [_video("v1"), _keyframe("k1", "v1")],
    )
    assert out["status"] == "ready"
    assert out["retrieval"]["video_gt_targets"] == 1
    assert out["retrieval"]["video_gt_targets_hit_by_keyframe"] == 1
    assert out["retrieval"]["video_parent_recall"] == 1.0
    assert out["retrieval"]["candidate_precision"] == 1.0


def test_a_keyframe_of_another_video_does_not_hit():
    """同一个代码路径，只把父级换掉，结论必须翻转——否则上面的断言什么也没证明。"""
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[_one_item([{"asset_id": "k2"}])]),
        [_video("v1"), _video("v2"), _keyframe("k2", "v2")],
    )
    assert out["retrieval"]["video_gt_targets_hit_by_keyframe"] == 0
    assert out["retrieval"]["video_parent_recall"] == 0.0
    assert out["retrieval"]["candidate_precision"] == 0.0


def test_candidate_and_predicted_precision_are_computed_separately():
    """候选中了一半、交付里一个都没有：两个精确率必须不同。"""
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[_one_item([{"asset_id": "k1"}, {"asset_id": "k2"}])]),
        [_video("v1"), _video("v2"), _keyframe("k1", "v1"), _keyframe("k2", "v2")],
    )
    assert out["retrieval"]["candidate_precision"] == 0.5
    assert out["retrieval"]["predicted_keyframes"] == 0
    assert out["retrieval"]["predicted_precision"] is None


def test_returning_a_source_video_does_not_by_itself_hit_a_keyframe_metric():
    """候选里给的是源视频本身：``candidate_keyframes`` 仍是 0（它数的是帧，不是视频）。"""
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[_one_item([{"asset_id": "v1"}])]),
        [_video("v1"), _keyframe("k1", "v1")],
    )
    assert out["retrieval"]["candidate_keyframes"] == 0
    assert out["retrieval"]["candidate_precision"] is None
    assert out["retrieval"]["video_parent_recall"] == 0.0


# ---- keyframe_analysis：时间采样 -------------------------------------------


def test_temporal_gap_and_span_follow_the_persisted_timestamps():
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[]),
        [_video("v1"), _keyframe("k1", "v1", 1.0), _keyframe("k2", "v1", 5.0)],
    )
    assert out["temporal"]["videos_sampled"] == 1
    assert out["temporal"]["mean_gap_sec"] == 4.0
    assert out["temporal"]["mean_span_sec"] == 4.0
    assert out["temporal"]["selection_reasons"] == {"未记录": 2}


def _keyframe_with_stamp_only_at(asset_id: str, parent: str, stamp: float, where: str) -> dict:
    """把时间戳**只**放在一级上。两级都放的话，这只夹具证明不了回退有没有生效。"""
    asset = _keyframe(asset_id, parent)
    if where == "column":
        asset["source_timestamp_sec"] = stamp
    else:
        asset["metadata_json"] = {**asset["metadata_json"], "source_timestamp_sec": stamp}
    return asset


def test_the_timestamp_falls_back_between_the_column_and_metadata_json():
    """2.0 把时间戳写成**列**（迁移 0010），1.0 写在 ``metadata_json`` 里。

    两级都要认——只认一级，指向另一种数据来源的 run 就会整块 ``temporal`` 空掉。
    """
    for where in ("column", "metadata_json"):
        out = keyframe_analysis.keyframe_analysis(
            _state(items=[]),
            [
                _video("v1"),
                _keyframe_with_stamp_only_at("k1", "v1", 1.0, where),
                _keyframe_with_stamp_only_at("k2", "v1", 5.0, where),
            ],
        )
        assert out["temporal"]["videos_sampled"] == 1, where
        assert out["temporal"]["mean_span_sec"] == 4.0, where


def test_a_single_keyframe_contributes_neither_gap_nor_span():
    """1.0 ``:5811`` 的收集条件是 ``len(values) > 1``：**一个点不构成跨度**。

    写成「一帧的跨度是 0」是很自然的直觉，但那是错的——0 是一个会被读成
    「覆盖了但没散开」的数，而这里的真相是「这个视频根本没被采样到可比较的程度」。
    """
    out = keyframe_analysis.keyframe_analysis(_state(items=[]), [_video("v1"), _keyframe("k1", "v1", 3.0)])
    assert out["temporal"]["videos_sampled"] == 1
    assert out["temporal"]["mean_span_sec"] is None
    assert out["temporal"]["mean_gap_sec"] is None


# ---- keyframe_analysis：处理阶段 -------------------------------------------


def test_processing_metrics_come_from_the_per_video_facts():
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[]),
        [_video("v1"), _keyframe("k1", "v1", 1.0), _keyframe("k2", "v1", 2.0)],
        video_facts={
            "v1": {"duration_sec": 120.0, "frame_count": 3600, "processing_seconds": 60.0},
        },
    )
    assert out["processing"]["source_duration_sec"] == 120.0
    assert out["processing"]["raw_frame_count"] == 3600
    assert out["processing"]["realtime_factor"] == 2.0
    assert out["processing"]["retained_frame_ratio"] == 0.0006
    assert out["processing"]["compression_ratio"] == 0.9994


def test_missing_frame_count_leaves_the_ratio_undefined_instead_of_zero():
    """ffprobe 很多容器不给 ``nb_frames``。缺了就**没有**这个比率，不是 0。"""
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[]),
        [_video("v1"), _keyframe("k1", "v1", 1.0)],
        video_facts={"v1": {"duration_sec": 30.0, "frame_count": 0, "processing_seconds": None}},
    )
    assert out["processing"]["source_duration_sec"] == 30.0
    assert out["processing"]["retained_frame_ratio"] is None
    assert out["processing"]["compression_ratio"] is None
    assert out["processing"]["processing_seconds_total"] == 0.0
    assert out["processing"]["realtime_factor"] is None


# ---- keyframe_analysis：回退与两种失败形态 ---------------------------------


def test_assets_none_is_unavailable_and_empty_assets_falls_back_to_traces():
    """**两种失败必须长得不一样**：取不到 vs 取到了是空的。"""
    unreachable = keyframe_analysis.keyframe_analysis(_state(items=[]), None, error="boom")
    assert unreachable["status"] == "unavailable"
    assert unreachable["error"] == "boom"

    empty = keyframe_analysis.keyframe_analysis(_state(items=[]), [])
    assert empty["status"] == "ready"
    assert empty["asset_source_note"] == "Sentrix 资产清单为空，且 run 未保存关键帧预览"
    assert empty["assets"]["keyframes"] == 0


def test_missing_scope_is_pending_not_an_error():
    out = keyframe_analysis.keyframe_analysis({"run_id": "r", "items": []}, [])
    assert out == {"status": "pending", "reason": "scope_id_missing", "run_id": "r"}


def test_empty_asset_list_recovers_lineage_from_persisted_tool_previews():
    """复用的 run 指向一个已不存在的 scope 时，血缘从工具轨迹里捞回来。"""
    state = _state(
        items=[
            {
                "gt_media": [{"asset_id": "v9", "media_type": "video"}],
                "retrieved_candidate_media": [{"asset_id": "k9"}],
                "judge": {"score": 2},
                "tool_trace": [
                    {
                        "observation": {
                            "preview": [
                                {
                                    "asset_id": "k9",
                                    "media_kind": "video_keyframe",
                                    "source_video_asset_id": "v9",
                                    "source_video_file_name": "trip.mp4",
                                    "source_timestamp_sec": 12.5,
                                    "selection_reason": "场景首帧",
                                }
                            ]
                        }
                    }
                ],
            }
        ]
    )
    out = keyframe_analysis.keyframe_analysis(state, [])
    assert out["status"] == "ready"
    assert out["assets"]["keyframes"] == 1
    assert out["assets"]["source_videos"] == 1
    assert out["retrieval"]["video_parent_recall"] == 1.0
    assert "工具轨迹回退" in out["asset_source_note"]
    # 时间戳是**从预览里捞出来的**（不是从资产清单读的）：它落到了 v9 名下，
    # 所以 v9 才算「被采样过」。这一条同时钉住 source_timestamp_sec 的搬运。
    assert out["temporal"]["videos_sampled"] == 1


def test_the_fallback_ignores_previews_that_are_not_keyframes():
    """原片预览混在轨迹里时不能被当成关键帧——否则回退路径会把每个视频都算成有帧。"""
    state = _state(
        items=[
            {
                "tool_trace": [
                    {
                        "observation": {
                            "preview": [
                                {"asset_id": "a1", "media_kind": "original_image"},
                                {"asset_id": "v1", "media_kind": "original_video"},
                            ]
                        }
                    }
                ]
            }
        ]
    )
    out = keyframe_analysis.keyframe_analysis(state, [])
    assert out["assets"]["keyframes"] == 0
    assert out["asset_source_note"] == "Sentrix 资产清单为空，且 run 未保存关键帧预览"


# ---- keyframe_analysis：回答质量对照 ---------------------------------------


def test_answer_quality_delta_separates_items_with_and_without_keyframes():
    state = _state(
        items=[
            _one_item([{"asset_id": "k1"}], score=2),
            _one_item([{"asset_id": "k1"}], score=2),
            _one_item([{"asset_id": "a1"}], score=0),
        ]
    )
    out = keyframe_analysis.keyframe_analysis(
        state,
        [_video("v1"), _keyframe("k1", "v1"), {"id": "a1", "file_name": "x.jpg", "media_type": "image"}],
    )
    assert out["usefulness"]["qa_with_keyframe"] == 2
    assert out["usefulness"]["qa_without_keyframe"] == 1
    assert out["usefulness"]["answer_quality_with_keyframe"] == 2.0
    assert out["usefulness"]["answer_quality_without_keyframe"] == 0.0
    assert out["usefulness"]["answer_quality_delta"] == 2.0


def test_unjudged_items_do_not_enter_the_quality_comparison():
    out = keyframe_analysis.keyframe_analysis(
        _state(items=[_one_item([{"asset_id": "k1"}], score=None)]),
        [_video("v1"), _keyframe("k1", "v1")],
    )
    assert out["usefulness"]["qa_with_keyframe"] == 0
    assert out["usefulness"]["answer_quality_with_keyframe"] is None
    assert out["usefulness"]["answer_quality_delta"] is None


# ---- memory_effectiveness：命中两维度 --------------------------------------


def _mem_state(items: list[dict], **over: object) -> dict:
    state = _state(items=items)
    state.update(over)
    return state


def test_coverage_map_is_what_turns_a_returned_keyframe_into_a_video_hit():
    """**核心判据**：GT 是视频、召回的是它的帧。只有拿到父级映射才算命中。

    夹具刻意不给 ``media_id`` / ``file_name``，所以 token 直接比对这条路走不通——
    命中**只能**来自血缘覆盖。这正是「两种口径给出不同答案」的那一对。
    """
    items = [
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [{"asset_id": "k1"}],
            "predicted_media": [],
            "judge": {"score": 2},
        }
    ]
    with_assets = memory_effectiveness.memory_effectiveness(
        _mem_state(items), [_video("v1"), _keyframe("k1", "v1")]
    )
    assert with_assets["details"]["memory_answer_attribution"]["tool"]["hit_items"] == 1
    assert (
        with_assets["data_quality"]["hit_match_source"]
        == "使用 scope 资产清单 2 条（含关键帧父级）做命中匹配"
    )

    without_assets = memory_effectiveness.memory_effectiveness(_mem_state(items), [])
    assert without_assets["details"]["memory_answer_attribution"]["tool"]["hit_items"] == 0
    assert "无资产清单" in without_assets["data_quality"]["hit_match_source"]


def test_tool_and_usage_dimensions_can_disagree():
    """工具召回了但模型没用：``tool`` 命中而 ``usage`` 未命中。"""
    items = [
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [{"asset_id": "k1"}],
            "predicted_media": [{"asset_id": "other"}],
            "judge": {"score": 0},
        }
    ]
    out = memory_effectiveness.memory_effectiveness(_mem_state(items), [_video("v1"), _keyframe("k1", "v1")])
    attribution = out["details"]["memory_answer_attribution"]
    assert attribution["tool"]["hit_items"] == 1
    assert attribution["usage"]["hit_items"] == 0
    assert attribution["tool"]["matrix"]["hit_incorrect"] == 1


def test_unanswerable_items_are_excluded_from_the_narrow_scope_only():
    """``answerability=unanswerable`` 的题进 ``_all_with_gt`` 不进默认口径。"""
    items = [
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [{"asset_id": "k1"}],
            "judge": {"score": 2},
            "answerability": "unanswerable",
        }
    ]
    attribution = memory_effectiveness.memory_effectiveness(
        _mem_state(items), [_video("v1"), _keyframe("k1", "v1")]
    )["details"]["memory_answer_attribution"]
    assert attribution["tool"]["eligible"] == 0
    assert attribution["tool_all_with_gt"]["eligible"] == 1


# ---- memory_effectiveness：层级表与错误链 ----------------------------------


def test_the_tool_trace_preview_fallback_feeds_retrieval_hits():
    """候选字段为空时，命中改从工具预览里取——1.0 ``:6082`` 的那条回退。"""
    items = [
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [],
            "judge": {"score": 2},
            "tool_trace": [
                {
                    "tool": "search_media",
                    "status": "ok",
                    "observation": {"preview": [{"asset_id": "k1", "source_video_asset_id": "v1"}]},
                }
            ],
        }
    ]
    out = memory_effectiveness.memory_effectiveness(_mem_state(items), [_video("v1"), _keyframe("k1", "v1")])
    assert out["details"]["memory_answer_attribution"]["tool"]["hit_items"] == 1


def test_error_chain_primary_rows_are_mutually_exclusive_and_cover_every_zero():
    items = [
        # 命中但 0 分
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [{"asset_id": "k1"}],
            "judge": {"score": 0},
            "agent_status": "complete",
        },
        # 未命中且 0 分
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [],
            "judge": {"score": 0},
            "agent_status": "error",
            "tool_trace": [{"tool": "inspect_photo", "status": "error", "observation": {"preview": []}}],
        },
        # 满分，不进任何 0 分口径
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [{"asset_id": "k1"}],
            "judge": {"score": 2},
        },
    ]
    out = memory_effectiveness.memory_effectiveness(_mem_state(items), [_video("v1"), _keyframe("k1", "v1")])
    chain = out["details"]["error_chain_diagnosis"]
    assert chain["wrong_total"] == 2
    rows = {row["key"]: row["count"] for row in chain["primary_rows"]}
    assert rows == {"memory_miss_wrong": 1, "memory_hit_wrong": 1}
    assert sum(rows.values()) == chain["wrong_total"]
    assert all(row["rate"] == 0.5 for row in chain["primary_rows"])
    assert chain["opportunity"]["retrieval_error_share"] == 0.5


def test_cross_cutting_signals_are_counted_separately_from_the_primary_split():
    """``signals`` 与主表可能重叠——1.0 的 ``note`` 专门警告不能相加。"""
    items = [
        {
            "gt_media": [{"asset_id": "v1"}],
            "retrieved_candidate_media": [],
            "judge": {"score": 0},
            "agent_status": "blocked_by_guard",
            "tool_trace": [{"tool": "inspect_photo", "status": "error", "observation": {"preview": []}}],
        }
    ]
    chain = memory_effectiveness.memory_effectiveness(
        _mem_state(items), [_video("v1"), _keyframe("k1", "v1")]
    )["details"]["error_chain_diagnosis"]
    signals = {row["key"]: row["count"] for row in chain["signals"]}
    assert signals["tool_failure"] == 1
    assert signals["execution_error"] == 1
    assert sum(signals.values()) == 2 > chain["wrong_total"] == 1


def test_tool_success_status_covers_the_whole_1_0_whitelist():
    """``ok`` / ``success`` / ``completed`` 三种都算成功；``complete`` **不算**（1.0 没写它）。"""
    ok_names = ["ok", "success", "completed"]
    items = [
        {
            "gt_media": [{"asset_id": "p1"}],
            "judge": {"score": 2},
            "tool_trace": [
                {"tool": "inspect_photo", "status": status, "observation": {"preview": []}}
                for status in ok_names
            ]
            + [{"tool": "inspect_photo", "status": "complete", "observation": {"preview": []}}],
        }
    ]
    out = memory_effectiveness.memory_effectiveness(_mem_state(items), [])
    assert out["details"]["visual_calls"] == {"total": 4, "effective": 3}


def test_levels_track_created_against_effective():
    items = [
        {
            "gt_media": [{"asset_id": "v1"}],
            "judge": {"score": 2},
            "agent_status": "complete",
            "evidence_asset_ids": ["k1"],
            "tool_trace": [
                {
                    "tool": "inspect_photo",
                    "status": "ok",
                    "observation": {"preview": [{"asset_id": "k1", "source_scene_index": 0}]},
                }
            ],
        },
        {
            "gt_media": [{"asset_id": "v1"}],
            "judge": {"score": 1},
            "agent_status": "partial",
            "tool_trace": [{"tool": "inspect_photo", "status": "error", "observation": {"preview": []}}],
        },
    ]
    out = memory_effectiveness.memory_effectiveness(
        _mem_state(items, input_integrity={"files_checked": 5}),
        [_video("v1"), _keyframe("k1", "v1")],
    )
    levels = {row["key"]: row for row in out["levels"]}
    assert levels["L0"]["created"] == 5
    assert levels["L0"]["effective"] == 1
    # L1 的 created 数的是**调用次数**，不是成功次数：两次 inspect_photo 都算「造出来了」，
    # 失败的那次进 ineffective。场景节点按去重后的 source_scene_index 计（只有 0 一个）。
    assert levels["L1"]["created"] == 3
    assert levels["L1"]["effective"] == 2
    assert levels["L1"]["ineffective"] == 1
    assert levels["L4+"]["created"] == 2
    assert levels["L4+"]["effective"] == 1
    assert levels["L4+"]["rate"] == 0.5
    assert out["details"]["answer_correctness"]["exact_accuracy"] == 0.5


def test_raw_total_falls_back_to_distinct_gt_media_when_integrity_is_absent():
    items = [
        {"gt_media": [{"asset_id": "a"}, {"asset_id": "b"}], "judge": {"score": 2}},
        {"gt_media": [{"asset_id": "a"}], "judge": {"score": 2}},
    ]
    out = memory_effectiveness.memory_effectiveness(_mem_state(items), [])
    assert out["levels"][0]["created"] == 2


# ---- exports ---------------------------------------------------------------


def _trace_item(**over: object) -> dict:
    item = {
        "qa_id": "q1",
        "question": "这是谁",
        "judge": {"score": 2},
        "runtime_turns": [
            {
                "index": 0,
                "message": "这是谁",
                "debug_trace": [
                    {"type": "planner", "prompt": [{"role": "user", "content": "planner"}], "raw_full": "P"},
                    {
                        "type": "model",
                        "step_id": "s1",
                        "prompt": [{"role": "user", "content": "a"}],
                        "raw_full": "A1",
                    },
                    {
                        "type": "model",
                        "step_id": "s2",
                        "prompt": [{"role": "user", "content": "b"}],
                        "raw_full": "A2",
                    },
                ],
            }
        ],
    }
    item.update(over)
    return item


def test_export_traces_keeps_only_the_planner_and_the_last_model_step():
    out = exports.export_traces(_state(items=[_trace_item()]))
    assert len(out) == 1
    assert out[0]["planner"] == {
        "type": "planner",
        "prompt": [{"role": "user", "content": "planner"}],
        "raw_full": "P",
    }
    assert out[0]["final_trace"]["step_id"] == "s2"


def test_export_traces_scores_and_min_score_both_apply():
    """1.0 这里是两个**独立的** ``if``——与 ``export_sft`` 的 ``if/elif`` 不同。"""
    items = [_trace_item(qa_id="q0", judge={"score": 0}), _trace_item(qa_id="q1", judge={"score": 1})]
    assert [row["qa_id"] for row in exports.export_traces(_state(items=items), scores=[1])] == ["q1"]
    assert [row["qa_id"] for row in exports.export_traces(_state(items=items), min_score=1)] == ["q1"]
    assert exports.export_traces(_state(items=items), scores=[0], min_score=1) == []


def test_export_sft_prefers_scores_over_min_score():
    """1.0 ``export_sft`` 是 ``if/elif``：给了 ``scores`` 就**不**看 ``min_score``。"""
    items = [_trace_item(qa_id="q0", judge={"score": 0}), _trace_item(qa_id="q1", judge={"score": 1})]
    out = exports.export_sft(_state(items=items), scores=[0], min_score=1)
    assert [row["qa_id"] for row in out["items"]] == ["q0"]


def test_export_sft_builds_prompt_to_response_samples():
    out = exports.export_sft(_state(items=[_trace_item()]))
    assert out["count"] == 2
    assert out["item_count"] == 1
    assert out["samples"][0]["messages"][-1] == {"role": "assistant", "content": "A1"}
    assert out["samples"][1]["step"] == "s2"
    assert out["filtered"] is False


def test_export_sft_parses_repr_strings_back_into_structures():
    """旧 run.json 里 ``arguments`` / ``observation`` 是 repr 字符串，1.0 会还原它们。"""
    item = _trace_item()
    item["runtime_turns"][0]["debug_trace"] = [
        {
            "type": "model",
            "step_id": "s1",
            "prompt": [{"role": "user", "content": "a"}],
            "raw_full": "A1",
            "arguments": "{'q': 'x'}",
            "observation": "not-a-literal",
        }
    ]
    step = exports.export_sft(_state(items=[item]))["items"][0]["turns"][0]["steps"][0]
    assert step["arguments"] == {"q": "x"}
    assert step["observation"] == "not-a-literal"


def test_export_filters_parse_only_the_documented_values():
    assert exports.parse_filter("0,1,2") == [0, 1, 2]
    assert exports.parse_filter("3,x,,") == []
    assert exports.parse_filter(" 2 , 1 ") == [2, 1]
    assert exports.parse_min_score("2") == 2
    assert exports.parse_min_score("0") is None
    assert exports.parse_min_score("") is None


def test_export_sft_retrieval_block_carries_every_documented_field():
    out = exports.export_sft(_state(items=[_trace_item(retrieval_f1=0.5)]))
    retrieval = out["items"][0]["retrieval"]
    assert set(retrieval) == set(exports._RETRIEVAL_FIELDS)
    assert retrieval["retrieval_f1"] == 0.5
    assert retrieval["gt_media"] is None


def _debug_calls_item() -> dict:
    """2.0 的完整轨迹：``trace_bundle.debug_calls``，一条模型调用一条。"""
    return {
        "qa_id": "q1",
        "question": "这是谁",
        "judge": {"score": 2},
        "runtime_turns": [
            {
                "index": 0,
                "message": "这是谁",
                "trace_bundle": {
                    "debug_calls": [
                        {
                            "step_id": "planner_1",
                            "call_type": "planner",
                            "messages": [{"role": "user", "content": "planner"}],
                            "tools": [],
                            "response": {"content": "P"},
                        },
                        {
                            "step_id": "step_1",
                            "call_type": "agent",
                            "messages": [{"role": "user", "content": "a"}],
                            "tools": [],
                            "response": {"content": "A1"},
                        },
                        {
                            "step_id": "step_2",
                            "call_type": "agent",
                            "messages": [{"role": "user", "content": "b"}],
                            "tools": [],
                            "response": {"content": "A2"},
                        },
                        {
                            "step_id": "step_8",
                            "call_type": "faithfulness_judge",
                            "messages": [{"role": "user", "content": "judge"}],
                            "tools": [],
                            "response": {"content": "J"},
                        },
                    ]
                },
            }
        ],
    }


def test_export_traces_prefers_the_2_0_debug_calls():
    """2.0 的 debug_calls 有完整 messages/response；导出必须用它，而不是空的 debug_trace。"""
    out = exports.export_traces(_state(items=[_debug_calls_item()]))
    assert out[0]["planner"] == {
        "type": "planner",
        "step_id": "planner_1",
        "prompt": [{"role": "user", "content": "planner"}],
        "raw_full": "P",
    }
    assert out[0]["final_trace"] == {
        "step_id": "step_2",
        "prompt": [{"role": "user", "content": "b"}],
        "raw_full": "A2",
    }


def test_export_sft_keeps_every_2_0_debug_call_and_builds_samples():
    out = exports.export_sft(_state(items=[_debug_calls_item()]))
    steps = out["items"][0]["turns"][0]["steps"]
    assert [step["call_type"] for step in steps] == [
        "planner",
        "agent",
        "agent",
        "faithfulness_judge",
    ]
    assert out["count"] == 4
    assert out["samples"][0]["messages"][-1] == {"role": "assistant", "content": "P"}
    assert out["samples"][-1]["step"] == "step_8"

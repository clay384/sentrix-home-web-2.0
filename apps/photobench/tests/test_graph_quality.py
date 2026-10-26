from photobench.domain.runs.graph_quality import aggregate_graph_qa_by_type, face_clustering_quality


def test_graph_qa_marks_historical_items_without_graph_telemetry():
    rows = aggregate_graph_qa_by_type(
        [
            {
                "question_type": "fact_retrieval",
                "media_retrieval_recall": 1.0,
                "judge": {"score": 2},
            }
        ]
    )

    assert rows[0]["telemetry_status"] == "not_recorded"
    assert rows[0]["graph_routed_count"] == 0
    assert rows[0]["retrieval_recall"] == 1.0


def test_graph_qa_compares_graph_head_with_primary_head():
    rows = aggregate_graph_qa_by_type(
        [
            {
                "question_type": "multi_hop",
                "gt_media": [{"asset_id": "gt"}],
                "tool_trace": [
                    {
                        "result": {
                            "retrieval_channels": [{"channel": "graph", "status": "ok"}],
                            "graph_primary_asset_ids": ["other"],
                            "graph_fused_asset_ids": ["gt"],
                        }
                    }
                ],
            }
        ]
    )

    assert rows[0]["telemetry_status"] == "recorded"
    assert rows[0]["graph_effect_count"] == 1
    assert rows[0]["graph_recall_delta"] == 1.0
    assert rows[0]["graph_gt_improved_count"] == 1


def test_graph_qa_counts_empty_heads_as_zero_recall():
    rows = aggregate_graph_qa_by_type(
        [
            {
                "question_type": "multi_hop",
                "gt_media": [{"asset_id": "gt"}],
                "tool_trace": [
                    {
                        "result": {
                            "retrieval_channels": [{"channel": "graph", "status": "ok"}],
                            "graph_primary_asset_ids": ["gt"],
                            "graph_fused_asset_ids": [],
                        }
                    }
                ],
            }
        ]
    )

    assert rows[0]["graph_effect_count"] == 1
    assert rows[0]["graph_recall_delta"] == -1.0
    assert rows[0]["graph_gt_worsened_count"] == 1


def test_graph_qa_does_not_call_degraded_graph_enabled():
    rows = aggregate_graph_qa_by_type(
        [
            {
                "question_type": "multi_hop",
                "gt_media": [{"asset_id": "gt"}],
                "tool_trace": [
                    {
                        "result": {
                            "retrieval_channels": [{"channel": "graph", "status": "degraded"}],
                            "graph_primary_asset_ids": ["gt"],
                            "graph_fused_asset_ids": ["gt"],
                        }
                    }
                ],
            }
        ]
    )

    assert rows[0]["graph_routed_count"] == 1
    assert rows[0]["graph_enabled_count"] == 0


def test_face_clustering_quality_uses_identity_gallery_and_image_links():
    quality = face_clustering_quality(
        [
            {
                "id": "cluster-1",
                "samples": [
                    {"face_instance_id": "seed", "file_name": "faceid_1.jpg", "validity": "uncertain"},
                    {"face_instance_id": "a-face", "file_name": "a.jpg", "validity": "verified"},
                    {"face_instance_id": "b-face", "file_name": "b.jpg", "validity": "verified"},
                ],
            }
        ],
        {"a.jpg": ["1"], "b.jpg": ["1"]},
    )

    assert quality["available"] is True
    assert quality["identity_gallery"]["f1"] == 1.0
    assert quality["image_identity_links"]["f1"] == 1.0


def test_face_clustering_quality_counts_imported_images_without_detections():
    quality = face_clustering_quality(
        [
            {
                "id": "cluster-1",
                "samples": [
                    {"face_instance_id": "seed", "file_name": "faceid_1.jpg", "validity": "uncertain"},
                    {"face_instance_id": "a-face", "file_name": "a.jpg", "validity": "verified"},
                ],
            }
        ],
        {"a.jpg": ["1"], "b.jpg": ["1"]},
        asset_names={"a.jpg", "b.jpg"},
    )

    assert quality["identity_gallery"]["gt_asset_count"] == 2
    assert quality["identity_gallery"]["false_negative"] == 1
    assert quality["identity_gallery"]["evaluable_image_count"] == 2
    assert quality["image_identity_links"]["false_negative"] == 1
    assert quality["image_identity_links"]["evaluable_image_count"] == 2

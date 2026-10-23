"""``ReadRuns`` + ``RunIndex`` + ``FileRunStore`` 的用例。

数据是**临时目录里的真文件**（不是内存假件）：这一层的职责本来就是「1.0 那批
``run.json`` 读出来还是原来的形状」，用假件就把它最该验的东西换掉了。
HTTP 走 ``FakeJsonHttpClient``，所以用例不依赖任何服务。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes_read import FakeJsonHttpClient

from photobench.application.runs.index import RunIndex
from photobench.application.runs.read_runs import ReadRuns
from photobench.infrastructure.results.store import FileRunStore


class _NoQaMetadata:
    def metadata_by_qa_id(self) -> dict:
        return {}


def _state(run_id: str, **over: object) -> dict:
    state = {
        "run_id": run_id,
        "album_id": "album3",
        "status": "completed",
        "created_at": "2026-01-01T00:00:00+08:00",
        "started_at": "2026-01-01T00:00:01+08:00",
        "qa_count": 2,
        "items": [
            {
                "qa_id": "q1",
                "question": "一加一",
                "judge": {"score": 2},
                "task_type": "T1",
                "answerability": "answerable",
            },
            {
                "qa_id": "q2",
                "question": "二加二",
                "judge": {"score": 0},
                "task_type": "T2",
                "answerability": "answerable",
            },
        ],
        "phases": {"qa_eval": {"status": "done"}},
    }
    state.update(over)
    return state


def _write(
    results_root: Path,
    run_id: str,
    state: dict,
    *,
    reviews: dict | None = None,
    gpu_lines: list[str] | None = None,
) -> None:
    run_dir = results_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    if reviews is not None:
        (run_dir / "reviews.json").write_text(json.dumps(reviews, ensure_ascii=False), encoding="utf-8")
    if gpu_lines is not None:
        (run_dir / "gpu_samples.jsonl").write_text("\n".join(gpu_lines), encoding="utf-8")


@pytest.fixture
def results(tmp_path: Path) -> Path:
    root = tmp_path / "results"
    _write(
        root,
        "run-a",
        _state("run-a"),
        reviews={"reviews": {"q1": {"verdict": "correct"}}},
        gpu_lines=[json.dumps({"_t": 1.0, "temperature_c": 40.0})],
    )
    _write(root, "run-b", _state("run-b", started_at="2026-02-01T00:00:00+08:00", status="running"))
    return root


def _read_runs(
    results: Path,
    http: FakeJsonHttpClient | None = None,
    live: dict | None = None,
) -> ReadRuns:
    store = FileRunStore(results)
    index = RunIndex()
    index.refresh(store)
    return ReadRuns(
        store=store,
        index=index,
        qa_metadata=_NoQaMetadata(),
        http=http or FakeJsonHttpClient(),
        sentrix_url=lambda: "http://sentrix:9598",
        live_states=(lambda: live) if live is not None else None,
    )


def test_the_index_reports_ready_and_the_scan_is_cheap_on_an_empty_root(tmp_path: Path):
    index = RunIndex()
    index.refresh(FileRunStore(tmp_path / "missing"))
    assert index.status() == {"state": "ready", "scanned": 0, "total": 0, "error": None}


def test_a_broken_run_json_only_drops_that_run(tmp_path: Path):
    """1.0 ``:5043``：一条坏文件不该让另外 266 个也看不见。"""
    root = tmp_path / "results"
    _write(root, "good", _state("good"))
    (root / "bad").mkdir(parents=True)
    (root / "bad" / "run.json").write_text("{ not json", encoding="utf-8")
    index = RunIndex()
    index.refresh(FileRunStore(root))
    assert [entry["run_id"] for entry in index.entries()] == ["good"]


def test_list_runs_sorts_by_started_at_descending_and_counts_active(results: Path):
    response = _read_runs(results).list_runs()
    assert [run["run_id"] for run in response["runs"]] == ["run-b", "run-a"]
    assert response["active_count"] == 1  # run-b 是 running
    assert response["total"] == 2
    assert response["scan"]["state"] == "ready"


def test_a_run_that_is_still_going_shows_up_in_the_list(results: Path):
    """**还在跑的 run 必须在列表里。**（2026-10-09 修）

    索引里没有它们——写侧只在 run 结束时 ``upsert``，在那之前索引不知道这一轮存在。
    而前端没有 URL 路由（``App.tsx`` 用 ``useState`` 选 run），所以「不在列表里」等于
    **整轮评测期间界面上完全够不着**：看不到进度，也点不到取消。实测跑了一轮 487 题的
    评测一个多小时，界面上一直什么都没有。

    这条断言的是**它出现在列表里**，不是某个具体字段——字段由
    ``analysis.run_list_entry`` 决定，那是另一件事。
    """
    live = _state("run-live", status="running", started_at="2026-03-01T00:00:00+08:00")

    response = _read_runs(results, live={"run-live": live}).list_runs()

    assert [run["run_id"] for run in response["runs"]] == ["run-live", "run-b", "run-a"]
    assert response["active_count"] == 2  # run-live 与 run-b


def test_a_finished_run_is_not_listed_twice(results: Path):
    """索引里已经有了的 run 不再叠一遍——**两个来源，一份列表**。

    真机上这两个来源会重叠一瞬：run 刚结束时写侧 ``upsert`` 与 ``cancel``/``delete``
    的清理之间有窗口。叠重了前端会看到两行一模一样的。
    """
    live = _state("run-a", status="running")

    response = _read_runs(results, live={"run-a": live}).list_runs()

    assert [run["run_id"] for run in response["runs"]] == ["run-b", "run-a"]


def test_list_runs_paginates(results: Path):
    response = _read_runs(results).list_runs(page=1, page_size=1)
    assert len(response["runs"]) == 1 and response["pages"] == 2 and response["has_next"] is True


def test_get_run_drops_items_but_keeps_the_count_and_a_recomputed_summary(results: Path):
    run = _read_runs(results).get_run("run-a")
    assert "items" not in run
    assert run["item_count"] == 2
    assert run["summary"]["answer_quality_mean"] == 1.0
    assert run["summary"]["judge_distribution"] == {"0": 1, "1": 0, "2": 1}


def test_get_run_merges_the_gpu_sample_history(results: Path):
    """1.0 ``:5372``：``telemetry_live`` 里并进 ``gpu_samples.jsonl`` 的采样。"""
    _write(
        results,
        "run-c",
        {
            "run_id": "run-c",
            "qa_count": 0,
            "items": [],
            "telemetry_live": {"samples_count": 0},
        },
        gpu_lines=[json.dumps({"_t": 1.0, "temperature_c": 55.0})],
    )
    run = _read_runs(results).get_run("run-c")
    assert run["telemetry_live"]["latest"]["temperature_c"] == 55.0
    assert run["telemetry_live"]["peak"]["temperature_c"] == 55.0
    assert run["telemetry_live"]["samples_count"] == 1


def test_a_run_without_gpu_samples_still_loads(results: Path):
    """没有采样文件时 ``history`` 是空表，不是缺字段——1.0 ``:5486`` 那一支。"""
    run = _read_runs(results).get_run("run-a")
    assert run.get("telemetry_live") is None  # run-a 根本没有 telemetry_live


def test_an_unknown_run_is_a_key_error(results: Path):
    """HTTP 层把 ``KeyError`` 映射成 404——那是 1.0 的形状。"""
    with pytest.raises(KeyError):
        _read_runs(results).get_run("nope")


def test_a_run_id_that_escapes_the_results_root_is_treated_as_missing(tmp_path: Path):
    """``results/`` 的**兄弟目录**里真的放一份 ``run.json``。

    只断言「抛 KeyError」是不够的——没有拦截时那个文件不存在、一样抛 KeyError，
    变异杀不掉。这条用例让越权读到的结果**存在**，于是拦截器成了唯一让它失败的东西。
    """
    root = tmp_path / "results"
    _write(root, "run-a", _state("run-a"))
    outside = tmp_path / "secrets"
    outside.mkdir()
    (outside / "run.json").write_text(json.dumps({"run_id": "secrets", "items": []}), encoding="utf-8")
    with pytest.raises(KeyError):
        _read_runs(root).get_run("../secrets")


def test_reviews_default_to_empty_and_are_returned_verbatim(results: Path):
    runs = _read_runs(results)
    # 有文件时 1.0 连 ``updated_at`` 一起回（``:4982``，这里那份文件里没有这个键）；
    # 没有文件时只回一个空 ``reviews``——两个分支的形状不同，1.0 如此。
    assert runs.get_reviews("run-a") == {
        "reviews": {"q1": {"verdict": "correct"}},
        "updated_at": None,
    }
    assert runs.get_reviews("run-b") == {"reviews": {}}


def test_get_run_items_filters_and_reports_facets(results: Path):
    response = _read_runs(results).get_run_items("run-a", page=1, page_size=10)
    assert response["total"] == 2
    assert response["unfiltered_total"] == 2
    assert response["facets"]["task_types"] == ["T1", "T2"]
    filtered = _read_runs(results).get_run_items("run-a", score="0")
    assert filtered["total"] == 1
    assert filtered["items"][0]["qa_id"] == "q2"
    assert filtered["filters"]["score"] == 0


def test_get_run_items_searches_the_question_text(results: Path):
    response = _read_runs(results).get_run_items("run-a", search="二加二")
    assert response["total"] == 1 and response["items"][0]["qa_id"] == "q2"


def test_get_run_items_attaches_reviews(results: Path):
    response = _read_runs(results).get_run_items("run-a")
    by_id = {item["qa_id"]: item for item in response["items"]}
    assert by_id["q1"]["review"] == {"verdict": "correct"}
    assert by_id["q2"]["review"] is None


def test_get_run_items_clamps_page_size_to_100(results: Path):
    assert _read_runs(results).get_run_items("run-a", page_size=9999)["page_size"] == 100


def test_get_run_item_returns_the_full_item_and_rejects_a_bad_index(results: Path):
    runs = _read_runs(results)
    assert runs.get_run_item("run-a", 1)["item"]["qa_id"] == "q2"
    with pytest.raises(KeyError):
        runs.get_run_item("run-a", 99)


def test_legacy_runs_get_their_ground_truth_images_hydrated_over_http(results: Path):
    """``_hydrate_legacy_gt_images``（1.0 ``:6851``）：本切片里 ``request_json`` 的唯一调用者。"""
    _write(
        results,
        "run-d",
        _state(
            "run-d",
            scope_id="scope-1",
            items=[
                {
                    "qa_id": "q1",
                    "question": "x",
                    "retrieval_image_ids": ["a.jpg"],
                    "matched_file_names": ["a.jpg"],
                    "judge": {"score": 2},
                }
            ],
        ),
    )
    http = FakeJsonHttpClient({"assets": {"assets": [{"id": "asset-1", "file_name": "a.jpg"}]}})
    item = _read_runs(results, http).get_run_item("run-d", 0)["item"]
    assert item["gt_images"] == [
        {
            "image_id": "a.jpg",
            "file_name": "a.jpg",
            "asset_id": "asset-1",
            "matched": True,
            "mapping_status": "ok",
        }
    ]
    assert "scope-1" in http.calls[0][0] and http.calls[0][3] == 15


def test_a_broken_asset_lookup_does_not_break_the_items_route(results: Path):
    """1.0 的 ``except Exception: return``：补不上 GT 图片不影响读题目。"""
    _write(
        results,
        "run-e",
        _state(
            "run-e",
            scope_id="scope-1",
            items=[
                {
                    "qa_id": "q1",
                    "question": "x",
                    "retrieval_image_ids": ["a.jpg"],
                }
            ],
        ),
    )
    http = FakeJsonHttpClient()
    http.fail_with = RuntimeError("HTTP 404 Not Found: 没有")
    response = _read_runs(results, http).get_run_items("run-e")
    assert response["total"] == 1
    assert "gt_images" not in response["items"][0]


def test_runs_without_a_scope_id_never_touch_the_network(results: Path):
    """没有 ``scope_id`` 的老 run 连一次请求都不该发。

    断言的是**调用记录**而不是「没抛异常」：补全那段外面裹着 ``except Exception``
    （1.0 的 ``:6862``），一次真的网络请求失败也会被吞掉——只有看调用记录才杀得掉。
    """
    http = FakeJsonHttpClient()
    assert _read_runs(results, http).get_run_items("run-a")["total"] == 2
    assert http.calls == []


def test_a_running_run_is_not_served_from_a_stale_cache(results: Path):
    """跑着的时候读到的那份快照，在它跑完之后**不许**继续被端出去。

    **来历不是推演**：S9c 真机验收时 ``run.json`` 已经 ``completed``、八个相位全
    ``done``，而 ``GET /api/runs/{id}`` 一直回 ``running``——读侧把第一次读到的
    在跑快照缓存住了，按它轮询的驱动脚本白等了十分钟。

    **区分力**：不按状态筛选缓存的实现会在第二次读到 ``running``。终态之后缓存
    照常生效（这条用例顺带钉住「修好了，但没把缓存整个砍掉」）。
    """
    reader = _read_runs(results)
    assert reader.get_run("run-b")["status"] == "running"  # 第一次：跑着，不入缓存
    _write(results, "run-b", _state("run-b", status="completed"))
    assert reader.get_run("run-b")["status"] == "completed"  # 第二次必须看到新状态
    assert reader.get_run("run-b")["status"] == "completed"  # 终态之后走缓存

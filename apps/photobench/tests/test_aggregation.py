"""遥测聚合用例：``aggregate_gpu_samples``（1.0 ``GpuSampler.aggregate``）与
``apply_live_sample``（1.0 ``_persist_gpu_sample`` 中段）。

**为什么这两个纯函数值得单独一份用例**：它们决定 run.json 里那份曲线读起来是
「这台机器占了多少内存」还是「一堆看着像内存的 0」。数字错了不会有异常——它会
静静地让两台机器看起来可比。所以这里逐个分支、逐个数地钉。
"""

from __future__ import annotations

from typing import Any

from photobench.domain.runs.analysis import aggregate_gpu_samples, apply_live_sample

#: 24 个采样点，值就是 1..24 —— 足够越过 ``p95`` 的 ``n >= 20`` 门槛，
#: 于是 peak/mean/p50/p95 四个数各不相同，一条断言就能把它们全部分开。
_TWENTY_FOUR = [{"model_process_memory_used_mib": float(value)} for value in range(1, 25)]


def test_an_empty_sample_table_is_zero_samples_not_an_error():
    assert aggregate_gpu_samples([]) == {"samples_count": 0}


def test_peak_mean_p50_and_p95_are_computed_from_the_sample_table():
    """四个统计量：peak=24、mean=12.5、p50=13、p95=23（``int(24*0.95)=22`` → 值 23）。

    **区分力**：这条把「四个量都在算」与「只算 peak」分开——把任何一个换成另一个的
    实现都会在对应的那个数上错。
    """
    aggregated = aggregate_gpu_samples(_TWENTY_FOUR)
    stats = aggregated["model_process_memory_used_mib"]
    assert stats["peak"] == 24
    assert stats["mean"] == 12.5
    assert stats["p50"] == 13
    assert stats["p95"] == 23


def test_p95_uses_the_rank_index_not_the_maximum_once_there_are_past_twenty_samples():
    """n=21 时 ``p95 = sorted[int(21*0.95)] = sorted[19] = 20``（值域 1..21），**不是 21**。

    **区分力**：这是唯一能把「按秩取分位」与「干脆返回最大值」分开的样本数——n ≤ 20
    时 ``int(n*0.95)`` 恰好等于 ``n-1``，两种实现给出同一个数（这一点是被算出来的，
    不是猜的：``n*0.95 >= n-1`` ⇔ ``n <= 20``）。所以这条用例刻意用 21 个点。
    """
    samples = [{"model_process_memory_used_mib": float(value)} for value in range(1, 22)]
    stats = aggregate_gpu_samples(samples)["model_process_memory_used_mib"]
    assert stats["peak"] == 21
    assert stats["p95"] == 20


def test_a_missing_metric_is_absent_not_zero():
    """没有一条样本带这个指标时，键**不出现**——补 0 会让曲线掉到地上。

    **区分力**：一个用 ``.get(key, 0)`` 的实现会产出 ``peak: 0``，这条把它顶回来。
    """
    aggregated = aggregate_gpu_samples([{"model_process_memory_used_mib": 100.0}])
    assert "gpu_utilization_pct" not in aggregated
    assert "power_draw_w" not in aggregated


def test_the_uma_profile_is_selected_by_the_sample_source():
    """``memory_unit=process_pss_uma_mib`` → 走 UMA 分支，并按 ``source`` 报是哪一台。

    **区分力**：这一支决定了 run.json 里那条 note（「PSS 不是显存」）在不在。
    """
    jetson = aggregate_gpu_samples(
        [{"memory_unit": "process_pss_uma_mib", "source": "jetson_local_pss", "kv_cache_usage_pct": 10.0}]
    )
    assert jetson["source"] == "jetson_local_pss"
    assert jetson["memory_profile"]["memory_unit"] == "process_pss_uma_mib"
    orin = aggregate_gpu_samples(
        [{"memory_unit": "process_pss_uma_mib", "source": "orin_ssh_pss", "kv_cache_usage_pct": 10.0}]
    )
    assert orin["source"] == "orin_ssh_pss"
    assert "pss_samples_count" in orin


def test_the_apple_profile_is_selected_by_its_memory_unit():
    aggregated = aggregate_gpu_samples([{"memory_unit": "apple_phys_footprint_mib"}])
    assert aggregated["source"] == "apple_unified"
    assert aggregated["memory_profile"]["method"] == "apple_phys_footprint_v1"


def test_the_apple_profile_accepts_both_memory_units():
    """新旧两种 apple 口径都走 apple 分支：jsonl 只追加，历史样本永远是旧 unit。"""
    old = aggregate_gpu_samples([{"memory_unit": "apple_phys_footprint_mib"}])
    new = aggregate_gpu_samples([{"memory_unit": "apple_phys_footprint_plus_weight_map_mib"}])
    assert old["memory_profile"]["method"] == "apple_phys_footprint_v1"
    assert new["memory_profile"]["method"] == "apple_phys_footprint_plus_weight_map_v1"


def test_the_default_profile_estimates_comparable_workload_memory():
    """默认分支的公式：``fixed_base = idle - kv_capacity``，``comparable = fixed_base + capacity * peak%``。

    这一条用一组能心算的数：idle=8192 MiB、kv_capacity=4 GiB、峰值占用 50% →
    fixed_base = 8 - 4 = 4 GiB，comparable = 4 + 4*0.5 = 6 GiB。

    **区分力**：把 peak 当成 GiB 直接用的实现会给出 4+2=6 之外的值；漏掉 idle 取最小
    的实现在这里只有一个 idle 点、看不出来，所以下面另开一条。
    """
    samples = [
        {"model_process_memory_used_mib": 8192.0, "kv_cache_usage_pct": 0.0, "kv_cache_capacity_gib": 4.0},
        {"model_process_memory_used_mib": 10000.0, "kv_cache_usage_pct": 50.0, "kv_cache_capacity_gib": 4.0},
    ]
    profile = aggregate_gpu_samples(samples)["memory_profile"]
    assert profile["idle_process_memory_gib"] == 8.0
    assert profile["fixed_base_memory_gib"] == 4.0
    assert profile["kv_cache_used_peak_gib"] == 2.0
    assert profile["comparable_workload_memory_gib"] == 6.0


def test_the_idle_baseline_uses_the_minimum_of_the_zero_kv_samples():
    """idle 取的是「KV 占用为 0」的样本里**最小**的进程内存，不是第一个也不是 p50。

    **区分力**：这条的两个 0-KV 样本值不同（9000 / 8000），取 min 得 8 GiB，
    取第一个或 p50 得 9 GiB——两种口径在 ``fixed_base_memory_gib`` 上分得开。
    """
    samples = [
        {"model_process_memory_used_mib": 9000.0, "kv_cache_usage_pct": 0.0, "kv_cache_capacity_gib": 4.0},
        {"model_process_memory_used_mib": 8000.0, "kv_cache_usage_pct": 0.0, "kv_cache_capacity_gib": 4.0},
        {"model_process_memory_used_mib": 12000.0, "kv_cache_usage_pct": 80.0, "kv_cache_capacity_gib": 4.0},
    ]
    profile = aggregate_gpu_samples(samples)["memory_profile"]
    assert profile["idle_process_memory_gib"] == 8000.0 / 1024


def test_no_kv_capacity_leaves_the_estimate_null():
    """拿不到 ``kv_cache_capacity_gib`` 时**不给估算**——估算的分母错了比没有更糟。"""
    samples = [{"model_process_memory_used_mib": 8192.0, "kv_cache_usage_pct": 0.0}]
    profile = aggregate_gpu_samples(samples)["memory_profile"]
    assert profile["comparable_workload_memory_gib"] is None
    assert profile["fixed_base_memory_gib"] is None


def test_process_memory_limit_and_over_limit_flags_are_carried():
    samples = [
        {"model_process_memory_limit_mib": 2048.0, "model_process_memory_over_limit": False},
        {"model_process_memory_limit_mib": 3000.0, "model_process_memory_over_limit": True},
    ]
    aggregated = aggregate_gpu_samples(samples)
    assert aggregated["model_process_memory_limit_mib"] == 3000.0
    assert aggregated["model_process_over_limit_samples"] == 1


# ---- 实时视图 -------------------------------------------------------------


def _live() -> dict[str, Any]:
    return {
        "status": "running",
        "source": "host_nvidia_smi",
        "samples_count": 0,
        "latest": {},
        "peak": {},
        "phase_snapshots": {},
        "history": [],
    }


def test_apply_live_sample_keeps_a_history_point_and_a_running_peak():
    live = _live()
    apply_live_sample(live, {"temperature_c": 50.0, "gpu_utilization_pct": 10.0}, "qa_eval", "QA 测评")
    apply_live_sample(live, {"temperature_c": 70.0, "gpu_utilization_pct": 5.0}, "qa_eval", "QA 测评")
    assert live["samples_count"] == 2
    assert len(live["history"]) == 2
    assert live["history"][0]["phase"] == "qa_eval"
    assert live["latest"]["temperature_c"] == 70.0
    # peak 是历史最大，不是最新值——这条把它们分开。
    assert live["peak"]["temperature_c"] == 70.0
    assert live["peak"]["gpu_utilization_pct"] == 10.0
    phase = live["phase_snapshots"]["qa_eval"]
    assert phase["samples_count"] == 2
    assert phase["peak"]["temperature_c"] == 70.0


def test_apply_live_sample_trims_the_history_to_the_tail():
    """run.json 里的实时曲线只留尾部 ``tail`` 个点；**完整曲线在 gpu_samples.jsonl**。

    **区分力**：不截断的实现在一个小时的 run 上会把 run.json 撑到几百 MB——这条
    断言的是长度，不是「有没有 history」。
    """
    live = _live()
    for index in range(5):
        apply_live_sample(live, {"temperature_c": float(index)}, "qa_eval", "QA 测评", tail=3)
    assert len(live["history"]) == 3
    assert [point["temperature_c"] for point in live["history"]] == [2.0, 3.0, 4.0]
    # 但累积计数与峰值看的是全部样本，不是被截断后的那三个。
    assert live["samples_count"] == 5
    assert live["peak"]["temperature_c"] == 4.0


def test_apply_live_sample_normalizes_a_legacy_null_history():
    """老 run 里 ``history`` 可能是 ``null``（1.0 的 ``isinstance`` 归一化照搬）。"""
    live: dict[str, Any] = {"status": "running", "history": None, "peak": None, "phase_snapshots": None}
    apply_live_sample(live, {"temperature_c": 40.0}, "qa_eval", "QA 测评")
    assert isinstance(live["history"], list)
    assert live["peak"]["temperature_c"] == 40.0
    assert live["phase_snapshots"]["qa_eval"]["samples_count"] == 1


def test_the_live_scopes_are_picked_up_from_the_sample():
    """六个 scope 字段决定界面上每个数字属于哪台机器/哪个进程。"""
    live = _live()
    apply_live_sample(
        live,
        {
            "temperature_c": 40.0,
            "system_memory_scope": "host_all_processes",
            "memory_scope": "gpu_device",
            "process_memory_scope": "gpu_compute_processes",
        },
        "qa_eval",
        "QA 测评",
    )
    assert live["scopes"]["system_memory"] == "host_all_processes"
    assert live["scopes"]["gpu_memory"] == "gpu_device"
    assert live["scopes"]["model_process"] == "gpu_compute_processes"

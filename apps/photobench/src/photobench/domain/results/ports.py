"""``results/`` 读侧的端口：列出 run、读单个 run 的完整状态、读评审。

对应 1.0 ``OrchestratorRepository``（``:4872``）里 **只读** 的那几个入口：
``_load_existing``（``:5013``）/ ``list_runs``（``:5343``）/ ``get_run``（``:5354``）/
``get_reviews``（``:4976``）。

**为什么是「流式产出」而不是「一次返回一张表」。** 1.0 在 ``__init__`` 里把 ``results/``
下 **270 个 run 目录全部解析进内存**，然后才绑端口——实测（2026-10-04，267 个
``run.json``、11.07 GB）：**读字节只要 2.9 s，``json.loads`` 要 75.0 s**，差距全在解析。
1.0 因此要等到第 75 秒才开始服务，而进程里同时压着 11 GB 的 JSON 对象。
2.0 拆成两件事：``iter_entries`` 在**后台**逐个产出列表页要的瘦条目（见
``domain.runs.analysis.run_list_entry``，不带 items），``load_state`` 只在**某个 run
被真的点开时**才解析它。启动因此不必阻塞，内存也不必装下全部结果。

**条目里只有列表页需要的字段**，这是有意的：``get_run`` 要的完整状态（含 items）
走 ``load_state``，两者不共用一份缓存——共用就会把 447 MB 的单个 run 常驻在内存里。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class RunStore(Protocol):
    """结果目录的只读面。**本切片不含写侧**（建 run / 取消 / 重跑 / 评审写入）。"""

    def iter_entries(self) -> Iterator[dict[str, Any]]:
        """逐个产出 run 的**瘦列表条目**（``run_list_entry`` 的结果）。

        顺序不作保证——``GET /api/runs`` 的排序（``started_at`` 倒序）在 application 里
        做，因为那是接口的排序契约，不是目录扫描的属性。
        """
        ...

    def load_state(self, run_id: str) -> dict[str, Any]:
        """解析该 run 的 ``run.json``（含 ``items``）。

        未登记的 run 抛 ``KeyError``——1.0 的 ``get_run`` / ``get_run_items`` 就是这么
        表达 404 的（``:5357`` / ``:6447``），HTTP 层把它映射成 404 + ``{"error": ...}``。
        """
        ...

    def read_reviews(self, run_id: str) -> dict[str, Any]:
        """读 ``reviews.json``；文件不存在或损坏时返回 ``{"reviews": {}}``。

        与 1.0 ``get_reviews``（``:4976``）逐字一致：损坏的评审文件**不当错误**，只当
        「还没有评审」——它是个只读的展示维度，一个坏文件不该让整个 run 打不开。
        run 不存在时抛 ``KeyError``。
        """
        ...

    def read_gpu_sample_lines(self, run_id: str) -> list[str]:
        """读 ``gpu_samples.jsonl`` 的**原始行**；文件不存在时返回空表。

        解析留给 ``domain.runs.analysis.attach_gpu_sample_history``——1.0 是逐行
        ``json.loads`` 且坏行跳过（``:5412-5417``），那条「坏行不是错误」的口径要与
        解析放在一起，不能拆到 IO 层。run 不存在时抛 ``KeyError``。
        """
        ...


@runtime_checkable
class RunResultWriter(Protocol):
    """结果目录的**写**面：run-execution 主链把一次评测落盘的那三件文件。

    对应 1.0 ``BenchmarkRun`` 的持久化：``_write_persisted_snapshot``（``:2049``）写
    ``run.json`` + ``results.jsonl``，``_reset_gpu_samples_file``（``:2105``）与
    ``_persist_gpu_sample``（``:2110``）写 ``gpu_samples.jsonl``。

    **为什么与 ``RunStore``（读面）分开**：读面被 api 层的只读路由依赖，而写面只有主链
    依赖。合成一个协议会让 ``ReadRuns`` 的测试假件也被迫实现三个写方法——那三个方法
    在只读用例里只会是「不许被调用」的噪声。1.0 是同一个仓储类既读又写，2.0 按用途
    拆开；**目录形状一个字没改**，所以指到 1.0 的 ``results/`` 仍然两边都能读。
    """

    def write_snapshot(self, run_id: str, state: dict[str, Any]) -> None:
        """把状态写成 ``run.json`` 与 ``results.jsonl``（1.0 ``:2049``）。

        ``results.jsonl`` 是 ``state["items"]`` 的逐行 JSON，**不是**另一份数据——
        它存在的理由是 447 MB 的 ``run.json`` 无法被下游逐行消费。
        """
        ...

    def reset_gpu_samples(self, run_id: str) -> None:
        """把 ``gpu_samples.jsonl`` 截空并建出目录（1.0 ``:2105``）。

        采样在 run 开始时调用一次：追加式的文件必须从零开始，否则上一次同名 run
        的曲线会与本轮混在一起。
        """
        ...

    def append_gpu_sample(self, run_id: str, sample: dict[str, Any]) -> None:
        """追加一条采样（1.0 ``:2110``）。**立即落盘**：取消或崩溃时内存里的采样全丢，
        而这个文件是完整曲线的唯一权威来源。"""
        ...

    def save_reviews(self, run_id: str, stored: dict[str, Any]) -> None:
        """原子写 ``reviews.json``（1.0 ``save_reviews`` ``:4988`` 的那次 ``atomic_json``）。

        **筛选（哪些 qa_id 合法、verdict 白名单）不在这里**——那是规则，属 application。
        写侧只保证「一份完整的状态被原子地换上去」：``reviews.json`` 被读到一半就是
        「这个 run 的评审全丢了」，而那些评审是人工做的，丢了不可重建。
        """
        ...

    def delete_run(self, run_id: str) -> None:
        """删除一个 run 的整个目录（1.0 ``delete_run`` ``:7010`` 的 ``shutil.rmtree``）。

        1.0 先确认 ``run_dir`` 真在 ``results_root`` 之下才删；``_run_dir`` 的路径穿越
        拦截在这里是**保护性**的：删错目录比读错文件严重得多。
        """
        ...


class RunStartupRecovery(Protocol):
    """Own the result-root instance lease before recovering interrupted runs.

    acquire_and_recover refuses a second live owner. Original nonterminal states
    are backed up before an interrupted transition; terminal history is untouched.
    """

    def acquire_and_recover(self) -> int: ...

    def close(self) -> None: ...


@runtime_checkable
class RunRevisions(Protocol):
    """Opaque committed file identity; deletion raises KeyError, IO errors propagate.

    New read capability; the legacy RunStore methods remain supported.
    """

    def revision(self, run_id: str) -> str:
        """Cacheable immutable revision, or empty when no reliable revision exists."""
        ...


@runtime_checkable
class RunReadViews(Protocol):
    """Optional lightweight committed view, without loading item traces.

    load_overview preserves item_count/effective summary at its checkpoint.
    Old directories may fall back to the full reader, without rewriting raw data.
    telemetry_revision changes on append, replacement or truncation (absent is
    distinct). Missing run raises KeyError; storage/corruption errors propagate.
    Legacy RunStore methods remain available for at least one release.
    """

    def load_overview(self, run_id: str) -> dict[str, Any]: ...

    def telemetry_revision(self, run_id: str) -> str: ...

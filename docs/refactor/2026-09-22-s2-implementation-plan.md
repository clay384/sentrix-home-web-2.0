# Sentrix Home Web 2.0 — S2 实施计划（视觉记忆）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通第二条端到端竖切——导入照片后，在 Web 上输入一个内容词（"桥"、"海边"、"红色"），能搜出正确的照片。

**Architecture:** 沿用四层（domain / application / infrastructure / api），依赖方向由 `import-linter` 的 5 条契约强制。本切片新增一条独立的检索域：`domain/retrieval`（融合规则、向量端口、相似度）、`application/retrieval`（导入即向量化 + 视觉召回）、`infrastructure/{models,vector}`（Chinese-CLIP 适配器、Qdrant 派生索引）。SQLite 的 `memory_vectors` 是向量的**权威**，Qdrant 是**可重建的派生镜像**。

**Tech Stack:** Python 3.11 / FastAPI / 标准库 sqlite3 / Pillow · chinese-clip-ViT-L-14（进程内 GPU 推理，768 维）+ torch / qdrant-client（嵌入式）· pytest / ruff / mypy --strict / import-linter · React 19 / TypeScript / Vite

**依据文档:** `docs/architecture/2026-09-21-sentrix-home-web-2-implementation-design.md`（§2.2 目录、§2.3 分层、§3.1 表、§5.2 数据目录、§6 工程标准、§8 切片表）

---

## 本文档是怎么写出来的

这份计划**不是照着意图写的**。做法反过来：

1. 在 153 上建一个隔离副本 `/home/asus/s2verify/repo`（真实仓库一个字节都没动）；
2. 在副本里按下面的 Task 25 → 33 顺序**真的写代码**，每个任务先写失败测试、看到红灯、再实现、再绿灯；
3. 每个任务结束后跑一遍 `make ci`，全绿才提交；
4. 全部做完后，逐任务做红灯复核（只留测试、去掉实现，确认真的红）与跨任务回归（在终态树上重跑每个任务的绿灯命令）；
5. **最后**把副本里的实际代码抽出来写成这份文档。

由此有三条硬保证：

- 计划里的**每一个代码块都是逐字节实际跑通过的那一版**（ruff 格式化之后的）；
- 每一个 **Expected 都是实际观察到的输出**，不是推测；
- 每个任务的红灯与绿灯都实际跑过，结果见文末「验证记录」。

**必须写明的代价**：Task 25–33 的代码**此刻还没有进真实仓库**，只存在于验证副本里。执行阶段要做的是把这份计划重新落地一遍——不是"确认一下"，是逐任务真写、真跑。

### 与 S0–S1 那份计划的关系

S0–S1（`docs/refactor/2026-09-21-s0-s1-implementation-plan.md`）是照着意图写的，代价是 19 条缺陷——每一个"意图与现实之间的缝隙"都只有执行时才暴露。那份文档的格式与详细度仍然是本计划的基准（`**Files:**` / `- [ ] **Step N**` / `Run:` / `Expected:`），只是内容来源不同。

### 任务编号

S0–S1 用了 Task 1–24，全局连续。S2 顺着往下排：**Task 25–33**，不另起前缀。

---

## 前置条件

远端仓库 `/home/asus/Github/sentrix-home-web2.0`，分支 `psh2.0`，起点提交 `456f6b3`（S1 结束时的工作树干净）。

S2 会往 `apps/api` 的依赖里加四个包：`qdrant-client` 在 **Task 27**（它第一次需要它的地方）声明，
`torch` / `torchvision` / `cn-clip` 在 **Task 28** 声明。第一次装比较慢（torch 约 2.5GB）：

```bash
cd /home/asus/Github/sentrix-home-web2.0 && .venv/bin/pip install -e "apps/api[dev]"
```

模型权重不在仓库里，需要一次性确认（153 上已有）：

```bash
ls -la ~/.cache/clip/clip_cn_vit-l-14.pt    # 1,625,135,766 字节
```

> **通用规则（文档同步）**：凡是新增或删除 `apps/api/src/sentrix/` 下 `.py` 文件的任务，都必须同步更新 `docs/architecture/module-map.md` 中该文件的**整行**——路径 / 职责 / 层 / **依赖谁** / **被谁依赖**。两列依赖由 `scripts/check_module_map.py` 用 `ast` 解析真实 import 后校验：少写、多写、与转置不符都会红。加了新文件而**没有**改被它依赖的旧行的「被谁依赖」列，同样会红——这是最容易漏的一步。
>
>
> **通用规则（包的 `__init__.py` 也是一条边）**：`scripts/check_module_map.py` 用 `ast` 解析包内 import，`import sentrix.domain.retrieval.scheme` 这类**子模块导入会被同时记成两条依赖**——`sentrix/domain/retrieval/scheme.py` **和** `sentrix/domain/retrieval/__init__.py`（导入子模块必然先执行包的 `__init__`，而 `__init__` 自己也有 import）。所以新文件只要落在某个包下，被它依赖的那两行「被谁依赖」列就**都要加**：少加包 `__init__.py` 那行同样会红。
>
> 例：Task 28 的 `sentrix/infrastructure/models/chinese_clip.py` 只写了 `from sentrix.domain.retrieval import scheme` 一行，但 `sentrix/domain/retrieval/__init__.py` 与 `sentrix/domain/retrieval/scheme.py` **两行**的「被谁依赖」都要记它。
> **通用规则（能力登记）**：每个新能力在 `docs/refactor/capability-map.jsonl` 里加一条记录，`status=verified` 时 `verification` 指向的测试文件必须真实存在（`scripts/check_capability_map.py` 会查）。本切片新增 9 条。
>
> **通用规则（OpenAPI）**：改了路由或 schema 就重新导出 `packages/contracts/openapi/sentrix-v1.yaml`（`scripts/export_openapi.py`），`make ci` 里带 `--check`。Task 29 与 Task 31 各需要一次。
>
> **通用规则（TDD）**：每个任务先只落地测试 → `Run` 确认红 → 再落地实现 → `Run` 确认绿。跳过红灯那一步，计划里剩下的就只是"打算做什么"。

## 文件结构

本切片新增/改动的文件（其余保持 S1 的原样）：

```text
sentrix-home-web2.0/
├── apps/api/
│   ├── pyproject.toml                     +torch / torchvision / cn-clip / qdrant-client
│   ├── src/sentrix/
│   │   ├── config.py                      +qdrant_dir（由 data_dir 派生）
│   │   ├── main.py                        +注册 search 路由
│   │   ├── bootstrap/container.py         +vector_repository / vector_index / visual_embedder
│   │   ├── api/
│   │   │   ├── routes/search.py           GET /api/v1/search
│   │   │   ├── routes/media.py            导入时传入 visual_indexer，响应加 indexed
│   │   │   └── schemas/{media,search}.py
│   │   ├── domain/retrieval/
│   │   │   ├── models.py                  CandidateHit / FusedCandidate
│   │   │   ├── fusion.py                  加权 RRF + 证据分级
│   │   │   ├── similarity.py              余弦与维度一致性
│   │   │   ├── scheme.py                  钉死的嵌入方案常量
│   │   │   └── ports.py                   向量 / 嵌入端口
│   │   ├── application/
│   │   │   ├── media/import_assets.py     +visual_indexer（导入即向量化）
│   │   │   └── retrieval/{index_visual,retrieve_visual}.py
│   │   └── infrastructure/
│   │       ├── models/chinese_clip.py     Chinese-CLIP 适配器
│   │       ├── vector/qdrant_index.py     Qdrant 派生索引
│   │       └── persistence/
│   │           ├── migrations/0003_memory_vectors.sql
│   │           └── repositories/vectors.py
│   └── tests/{unit,contract,integration,e2e}/  （见各任务）
├── apps/web/src/                          SearchBox / SearchResults + client.searchAssets
├── pyproject.toml                         mypy overrides +cn_clip
└── packages/contracts/openapi/sentrix-v1.yaml
```

---

## 依赖图与执行顺序

先把顺序画出来再写代码。S0 踩过两次「两个任务循环依赖」（Task 4/5、Task 17/18），共同特征是「单独看都对，顺序错了」。

```text
Task 25  domain/retrieval：融合与模型（纯规则）
   │
Task 26  memory_vectors 迁移 + 向量端口 + SQLite 仓储
   │
   ├── Task 27  Qdrant 派生索引适配器
   │
   └── Task 28  Chinese-CLIP 嵌入适配器 + 方案常量
          │
          ├── Task 29  导入即向量化（application + HTTP 契约）
          │
          └── Task 30  视觉召回用例（融合骨架落地）
                 │
              Task 31  GET /api/v1/search
                 │
              Task 32  Web 搜索框
                 │
              Task 33  端到端验收（真模型 + 真 Qdrant）
```

**为什么不能换序：**

| 约束 | 原因 |
|---|---|
| 25 必须最先 | 26 的 `ports.py` 与 `fusion.py` 都放在 `domain/retrieval/` 包里，包得先存在 |
| 26 在 27/28 之前 | `VectorIndex` / `VisualEmbedder` 两个 Protocol 定义在 26 的 `ports.py`；27/28 是它们的实现 |
| 27 与 28 之间无依赖 | 可以互换；本文档按 27 → 28 排，因为索引适配器的验证不需要 GPU |
| 29、30 都在 26/27/28 之后 | 两者都要同时用到「嵌入」与「存储」两端口 |
| 31 依赖 30，32 依赖 31 | 逐层向上，没有回头路 |
| 33 最后 | 它要的是完整的 HTTP 链路 |

**依赖图里没有环**：每一步只依赖它上面的东西；反向依赖一条都没有。

---

# S2 — 视觉记忆（Task 25–33）

## Task 25: 融合排序规则（domain/retrieval）

**依赖：** 无。这是 S2 的起点。

**Files:**
- Create: `apps/api/src/sentrix/domain/retrieval/__init__.py`
- Create: `apps/api/src/sentrix/domain/retrieval/models.py`
- Create: `apps/api/src/sentrix/domain/retrieval/fusion.py`
- Create: `apps/api/tests/unit/test_fusion.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

先把**纯规则**立起来：融合的数值全部来自 `docs/refactor/behaviour-baseline.md` 的冻结项（RRF_K=60、anchor +1.0、六路权重），它们来自 1.0 的 R7 消融实验（chinese-clip 视觉 r10=0.887、词法 0.373、文本 0.158）。平权 RRF 会让弱通道把强通道稀释掉，混合反而低于纯视觉。

这一步只写规则、不接数据：`fuse()` 吃的是「每条通道的排名列表」，与它从哪来无关。S2 只会有一条通道（`visual_ann`），但骨架按六路留好，S6 补其余通道时不必再动它。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/unit/test_fusion.py`:

```python
"""融合排序规则的单元测试。

这些断言钉的是 `docs/refactor/behaviour-baseline.md` 里冻结的数值：RRF_K=60、
anchor +1.0、六路通道权重。1.0 的这套权重来自 R7 消融实验（chinese-clip 视觉
r10=0.887、词法 0.373、文本 0.158），平权 RRF 会让弱通道把强通道稀释掉，所以
改任何一个数字都必须先改基线文档。
"""

from sentrix.domain.retrieval.fusion import (
    ANCHOR_BOOST,
    DEFAULT_CHANNEL_WEIGHTS,
    RRF_K,
    evidence_class_for,
    fuse,
    weighted_rrf_score,
)
from sentrix.domain.retrieval.models import CandidateHit


def hit(asset_id: str, channel: str, rank: int, score: float = 1.0) -> CandidateHit:
    return CandidateHit(asset_id=asset_id, channel=channel, rank=rank, raw_score=score)


def test_frozen_constants_match_behaviour_baseline():
    assert RRF_K == 60
    assert ANCHOR_BOOST == 1.0
    assert DEFAULT_CHANNEL_WEIGHTS == {
        "visual_ann": 2.5,
        "lexical": 1.0,
        "text_ann": 0.5,
        "metadata": 1.0,
        "entity": 1.0,
        "adjacency": 0.5,
    }


def test_weighted_rrf_matches_cormack_formula():
    """weight/(k+rank)，k=60。rank 从 1 开始，所以 rank=1 时是 weight/61。"""
    assert weighted_rrf_score({"visual_ann": 1}, DEFAULT_CHANNEL_WEIGHTS) == 2.5 / 61
    assert weighted_rrf_score({"lexical": 1}, DEFAULT_CHANNEL_WEIGHTS) == 1.0 / 61


def test_visual_weight_dominates_lexical_at_equal_rank():
    channels = {
        "visual_ann": [hit("asset_visual", "visual_ann", 1)],
        "lexical": [hit("asset_lexical", "lexical", 1)],
    }

    fused = fuse(channels)

    assert [c.asset_id for c in fused] == ["asset_visual", "asset_lexical"]
    # 反向验证：权重真的生效了。若 fuse() 退回平权 RRF，两条分数会相等，上面那条
    # 断言就只剩「插入顺序」在决定名次——那正是本测试要挡住的恒真形态。
    assert fused[0].score > fused[1].score


def test_multi_channel_agreement_outranks_single_channel():
    """同一张图出现在两条通道上，RRF 自然累加。"""
    channels = {
        "visual_ann": [hit("asset_both", "visual_ann", 2), hit("asset_visual_only", "visual_ann", 1)],
        "lexical": [hit("asset_both", "lexical", 1)],
    }

    fused = fuse(channels)

    assert fused[0].asset_id == "asset_both"
    assert fused[0].channels == {"visual_ann": 2, "lexical": 1}


def test_anchor_channels_get_deterministic_boost():
    channels = {
        "visual_ann": [hit("asset_visual", "visual_ann", 1)],
        "metadata": [hit("asset_anchor", "metadata", 1)],
    }

    fused = fuse(channels)

    by_id = {c.asset_id: c for c in fused}
    assert by_id["asset_anchor"].evidence_class == "anchor"
    assert by_id["asset_visual"].evidence_class == "semantic"
    # 1.0/61 = 0.01639，anchor 再加 1.0 —— 元数据精确命中永远压在语义命中之上。
    assert by_id["asset_anchor"].score == 1.0 / 61 + ANCHOR_BOOST
    assert fused[0].asset_id == "asset_anchor"


def test_expander_channel_is_classified_separately():
    assert evidence_class_for("adjacency") == "expander"
    assert evidence_class_for("entity") == "anchor"
    assert evidence_class_for("lexical") == "semantic"
    assert evidence_class_for("visual_ann") == "semantic"


def test_channel_insertion_order_does_not_change_the_result():
    forward = fuse(
        {
            "visual_ann": [hit("asset_b", "visual_ann", 1)],
            "lexical": [hit("asset_a", "lexical", 1)],
        }
    )
    backward = fuse(
        {
            "lexical": [hit("asset_a", "lexical", 1)],
            "visual_ann": [hit("asset_b", "visual_ann", 1)],
        }
    )

    # visual 权重更高，所以两条路径下 asset_b 都该在前。
    assert [c.asset_id for c in forward] == ["asset_b", "asset_a"]
    assert [c.asset_id for c in backward] == ["asset_b", "asset_a"]


def test_ties_break_on_asset_id_not_channel_insertion_order():
    """同分时按 asset_id 稳定排序。

    1.0 直接 sort(reverse=True)，同分名次取决于 dict 的插入顺序，也就是调用方
    传通道的先后——同一份数据换个调用顺序就换一套结果。检索必须可复现。
    """
    forward = fuse(
        {
            "metadata": [hit("asset_z", "metadata", 1)],
            "entity": [hit("asset_y", "entity", 1)],
        }
    )
    backward = fuse(
        {
            "entity": [hit("asset_y", "entity", 1)],
            "metadata": [hit("asset_z", "metadata", 1)],
        }
    )

    scores = {c.asset_id: c.score for c in forward}
    assert scores["asset_y"] == scores["asset_z"], "构造前提：两条必须同分"
    assert [c.asset_id for c in forward] == ["asset_y", "asset_z"]
    assert [c.asset_id for c in backward] == ["asset_y", "asset_z"]


def test_rank_comes_from_the_hit_not_the_list_position():
    """命中自报的 rank 才是权威，列表下标不算数。

    1.0 用 ``enumerate`` 取下标：命中列表一旦没排好，分数与自报名次就悄悄对不上，
    且没有任何征兆。这里把同一个 asset 放两次、顺序颠倒，断言用的是 rank=2。
    """
    fused = fuse({"visual_ann": [hit("asset_b", "visual_ann", 2), hit("asset_b", "visual_ann", 1)]})

    assert fused[0].channels == {"visual_ann": 1}
    assert fused[0].score == 2.5 / 61


def test_empty_input_yields_no_candidates():
    assert fuse({}) == []
    assert fuse({"visual_ann": []}) == []
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_fusion.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.domain.retrieval'
ERROR apps/api/tests/unit/test_fusion.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 实现 domain/retrieval**

`apps/api/src/sentrix/domain/retrieval/__init__.py`:

```python
"""检索域：查询契约、候选融合与排序规则。纯规则，零 IO。"""

from sentrix.domain.retrieval.fusion import (
    ANCHOR_BOOST,
    DEFAULT_CHANNEL_WEIGHTS,
    RRF_K,
    evidence_class_for,
    fuse,
    weighted_rrf_score,
)
from sentrix.domain.retrieval.models import CandidateHit, FusedCandidate

__all__ = [
    "ANCHOR_BOOST",
    "DEFAULT_CHANNEL_WEIGHTS",
    "RRF_K",
    "CandidateHit",
    "FusedCandidate",
    "evidence_class_for",
    "fuse",
    "weighted_rrf_score",
]
```
`apps/api/src/sentrix/domain/retrieval/models.py`:

```python
"""检索领域模型：候选命中与融合结果。纯数据与规则，零 IO。

分值不是裸 float。1.0 的 ``retrieval/base.py`` 把这件事写成了契约（P0-19）：
``CandidateHit`` 必须同时带 ``raw_score`` / ``score_kind`` / ``higher_is_better`` /
``rank``，因为跨通道的原始分数不可比——余弦相似度 0.3 与词法命中次数 1 放一起比
大小没有任何意义。融合只消费 ``rank``，原始分数只用于展示与探针校准。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CandidateHit:
    """一条通道召回的一条候选。``rank`` 从 1 开始，与 RRF 公式一致。"""

    asset_id: str
    channel: str
    rank: int
    raw_score: float
    score_kind: str = "cosine_similarity"
    higher_is_better: bool = True

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError(f"rank 从 1 开始，收到 {self.rank}")


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """融合后的候选。``channels`` 记录它被哪些通道以第几名召回。"""

    asset_id: str
    score: float
    channels: Mapping[str, int] = field(default_factory=dict)
    raw_scores: Mapping[str, float] = field(default_factory=dict)
    evidence_class: str = "semantic"
```
`apps/api/src/sentrix/domain/retrieval/fusion.py`:

```python
"""候选融合：加权 RRF + 证据分级。纯函数，零 IO。

对应 1.0 ``backend/retrieval/fusion.py``。数值全部取自
``docs/refactor/behaviour-baseline.md`` 的冻结项：

- ``RRF_K = 60``（Cormack 2009 的常数）
- anchor 通道 +1.0
- 通道权重 visual 2.5 / lexical 1.0 / text 0.5 / metadata 1.0 / entity 1.0 / adjacency 0.5

权重不是拍脑袋：R7 消融里 chinese-clip 视觉 r10=0.887、词法 0.373、文本 0.158，
平权 RRF 会让弱通道把强通道稀释掉，混合反而低于纯视觉。改任何一个数字前先改基线。

通道分三类：
- **anchor**（metadata / entity）：确定性锚点，融合后加固定加成；
- **semantic**（lexical / visual_ann / text_ann）：只按 RRF 名次融合；
- **expander**（adjacency）：继承种子，不独立提升，默认关闭。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sentrix.domain.retrieval.models import CandidateHit, FusedCandidate

RRF_K = 60
ANCHOR_BOOST = 1.0

ANCHOR_CHANNELS = ("metadata", "entity")
EXPANDER_CHANNELS = ("adjacency",)

DEFAULT_CHANNEL_WEIGHTS: Mapping[str, float] = {
    "visual_ann": 2.5,
    "lexical": 1.0,
    "text_ann": 0.5,
    "metadata": 1.0,
    "entity": 1.0,
    "adjacency": 0.5,
}


def weighted_rrf_score(ranks: Mapping[str, int], weights: Mapping[str, float], k: int = RRF_K) -> float:
    """``sum(weight[channel] / (k + rank))``；未登记的通道按 1.0 计权。"""
    return sum(weights.get(channel, 1.0) / (k + rank) for channel, rank in ranks.items())


def evidence_class_for(channel: str) -> str:
    if channel in ANCHOR_CHANNELS:
        return "anchor"
    if channel in EXPANDER_CHANNELS:
        return "expander"
    return "semantic"


def fuse(
    channel_hits: Mapping[str, Sequence[CandidateHit]],
    *,
    k: int = RRF_K,
    anchor_boost: float = ANCHOR_BOOST,
    channel_weights: Mapping[str, float] | None = None,
) -> list[FusedCandidate]:
    """把各通道的排名结果合并成一份融合排名。

    与 1.0 的两处行为差异：

    1. 名次取自 ``hit.rank``，不取列表下标。1.0 用 ``enumerate``，命中列表一旦
       不是按名次排好的，算出来的分数与命中自报的名次就会对不上，而且不报错。
    2. 同分时按 ``asset_id`` 升序稳定排序。1.0 用 ``sort(reverse=True)``，同分
       名次取决于调用方传通道的先后，同一份数据换个调用顺序就换一套结果。
    """
    weights = DEFAULT_CHANNEL_WEIGHTS if channel_weights is None else channel_weights
    candidates: dict[str, FusedCandidate] = {}

    for channel, hits in channel_hits.items():
        for hit in hits:
            candidate = candidates.get(hit.asset_id)
            if candidate is None:
                candidate = FusedCandidate(
                    asset_id=hit.asset_id,
                    score=0.0,
                    channels={},
                    raw_scores={},
                    evidence_class=evidence_class_for(channel),
                )
                candidates[hit.asset_id] = candidate
            # dataclass 是 frozen 的，累加通过重建完成——避免把可变状态漏给调用方。
            channels = {**candidate.channels, channel: hit.rank}
            raw_scores = {**candidate.raw_scores, channel: hit.raw_score}
            score = weighted_rrf_score(channels, weights, k=k)
            if candidate.evidence_class == "anchor":
                score += anchor_boost
            candidates[hit.asset_id] = FusedCandidate(
                asset_id=hit.asset_id,
                score=score,
                channels=channels,
                raw_scores=raw_scores,
                evidence_class=candidate.evidence_class,
            )

    return sorted(candidates.values(), key=lambda item: (-item.score, item.asset_id))
```

两处与 1.0 的**刻意差异**，都写在 `fusion.py` 的 docstring 里：

1. 名次取自 `hit.rank`，不取列表下标。1.0 用 `enumerate`，命中列表一旦没排好，算出的分数与命中自报的名次就会悄悄对不上。
2. 同分时按 `asset_id` 升序稳定排序。1.0 的 `sort(reverse=True)` 让同分名次取决于调用方传通道的先后——同一份数据换个顺序就换一套结果。

- [ ] **Step 4: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_fusion.py -p no:warnings`

Expected:

```text
..........                                                               [100%]
10 passed
```

- [ ] **Step 5: 更新 module-map.md**

在 `docs/architecture/module-map.md` 的表格里加入三行（放在 `sentrix/domain/media/` 那几行之后）：

```markdown
| `sentrix/domain/retrieval/__init__.py` | 包标记与检索域导出 | domain | `sentrix/domain/retrieval/fusion.py`、`sentrix/domain/retrieval/models.py` | — |
| `sentrix/domain/retrieval/models.py` | 候选命中 `CandidateHit` 与融合结果 `FusedCandidate`；分值不是裸 float | domain | — | `sentrix/domain/retrieval/__init__.py`、`sentrix/domain/retrieval/fusion.py` |
| `sentrix/domain/retrieval/fusion.py` | 加权 RRF 融合与证据分级；权重/RRF_K/anchor 加成来自行为基线 | domain | `sentrix/domain/retrieval/models.py` | `sentrix/domain/retrieval/__init__.py` |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 6: 登记能力**

追加到 `docs/refactor/capability-map.jsonl`：

```json
{"capability":"六路检索加权 RRF 融合","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/retrieval/fusion.py","source_symbol":"fuse","target_path":"apps/api/src/sentrix/domain/retrieval/fusion.py","verification":"apps/api/tests/unit/test_fusion.py","status":"verified"}
```

Run: `.venv/bin/python scripts/check_capability_map.py`
Expected: `capability-map: 0 个问题`

- [ ] **Step 7: 全量门禁**

Run: `make ci`
Expected:

```text
Required test coverage of 85.0% reached. Total coverage: 96.37%
75 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

（`make ci` 这一条每个任务都要跑。后面的任务不再重复贴 Expected 的覆盖率数字，只贴实际结果。）

---

## Task 26: 向量权威存储（迁移 0003 + 端口 + SQLite 仓储）

**依赖：** Task 25（`domain/retrieval/` 包已存在）。

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/persistence/migrations/0003_memory_vectors.sql`
- Create: `apps/api/src/sentrix/domain/retrieval/ports.py`
- Create: `apps/api/src/sentrix/domain/retrieval/similarity.py`
- Create: `apps/api/src/sentrix/infrastructure/persistence/repositories/vectors.py`
- Create: `apps/api/tests/integration/test_vector_repository.py`
- Create: `apps/api/tests/unit/test_similarity.py`
- Modify: `apps/api/src/sentrix/domain/retrieval/__init__.py`
- Modify: `apps/api/src/sentrix/bootstrap/container.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

`memory_vectors` 在设计 §3.1 的保留清单里，语义取自 1.0 `backend/db.py:782`，两处结构改进：新增 `dimension` 列（1.0 只能把 `vector_json` 解析出来才知道维度，"已写入的向量维度是否与当前模型一致"在 SQL 层看不见）；索引从单个 `space` 列改成 `(space, model_name)` 与 `(space, source_type, source_id)`。

`ports.py` 一次把三个端口都定义好：`VectorRepository`（权威）/ `VectorIndex`（派生镜像）/ `VisualEmbedder`（跨模态）。Task 27、28 分别是后两者的实现。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/integration/test_vector_repository.py`:

```python
"""真 SQLite + 真迁移文件下的 memory_vectors 仓储测试。"""

from __future__ import annotations

import pytest

from sentrix.domain.retrieval.ports import VectorRecord
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository

MODEL = "chinese-clip-ViT-L-14"


@pytest.fixture
def repository(migrations_ready_db: Database) -> SqliteVectorRepository:
    return SqliteVectorRepository(migrations_ready_db)


def record(
    source_id: str,
    vector: tuple[float, ...],
    *,
    scope_id: str = "home-default",
    source_type: str = "asset",
    model_name: str = MODEL,
    metadata: dict[str, object] | None = None,
) -> VectorRecord:
    return VectorRecord(
        scope_id=scope_id,
        space="visual",
        source_type=source_type,
        source_id=source_id,
        model_name=model_name,
        vector=vector,
        metadata=metadata or {},
    )


def test_upsert_then_has_vector(repository: SqliteVectorRepository):
    assert (
        repository.has_vector(space="visual", source_type="asset", source_id="asset_1", model_name=MODEL)
        is False
    )

    repository.upsert(record("asset_1", (1.0, 0.0, 0.0)))

    assert (
        repository.has_vector(space="visual", source_type="asset", source_id="asset_1", model_name=MODEL)
        is True
    )
    assert repository.count(space="visual") == 1


def test_upsert_same_key_updates_in_place(repository: SqliteVectorRepository):
    """同一 (space, source_type, source_id, model_name) 只该有一行。"""
    repository.upsert(record("asset_1", (1.0, 0.0, 0.0)))
    repository.upsert(record("asset_1", (0.0, 1.0, 0.0)))

    assert repository.count(space="visual") == 1
    hits = repository.search_exact(space="visual", vector=(0.0, 1.0, 0.0), limit=5)
    assert len(hits) == 1
    assert hits[0].score == pytest.approx(1.0)


def test_two_models_coexist_as_separate_rows(repository: SqliteVectorRepository):
    """同一张图两套模型的向量必须并存。

    1.0 就是因为 env 切换导致多套向量并存、索引对不上才把方案钉死的；并存本身
    不是问题，**查询时不带模型名**才是。这条钉住并存，下面那条钉住按模型过滤。
    """
    repository.upsert(record("asset_1", (1.0, 0.0, 0.0), model_name="chinese-clip-ViT-L-14"))
    repository.upsert(record("asset_1", (0.0, 1.0, 0.0), model_name="ViT-B-32"))

    assert repository.count(space="visual") == 2


def test_search_exact_filters_by_model_name(repository: SqliteVectorRepository):
    repository.upsert(record("asset_old", (0.0, 1.0, 0.0), model_name="ViT-B-32"))
    repository.upsert(record("asset_new", (1.0, 0.0, 0.0), model_name=MODEL))

    hits = repository.search_exact(space="visual", vector=(1.0, 0.0, 0.0), limit=5, model_name=MODEL)

    assert [hit.source_id for hit in hits] == ["asset_new"]


def test_search_exact_orders_by_cosine_descending(repository: SqliteVectorRepository):
    repository.upsert(record("asset_near", (0.9, 0.1, 0.0)))
    repository.upsert(record("asset_far", (0.0, 0.0, 1.0)))

    hits = repository.search_exact(space="visual", vector=(1.0, 0.0, 0.0), limit=5)

    assert [hit.source_id for hit in hits] == ["asset_near", "asset_far"]
    assert hits[0].score > hits[1].score


def test_search_exact_ties_break_on_source_id(repository: SqliteVectorRepository):
    repository.upsert(record("asset_z", (1.0, 0.0, 0.0)))
    repository.upsert(record("asset_a", (1.0, 0.0, 0.0)))

    hits = repository.search_exact(space="visual", vector=(1.0, 0.0, 0.0), limit=5)

    assert [hit.source_id for hit in hits] == ["asset_a", "asset_z"]


def test_search_exact_skips_rows_with_a_different_dimension(
    repository: SqliteVectorRepository,
):
    """维度不一致的脏行必须被排除，不能以「相似度 0」的形态混进结果。"""
    repository.upsert(record("asset_512", (1.0,) * 512))
    repository.upsert(record("asset_768", (1.0,) * 768))

    hits = repository.search_exact(space="visual", vector=(1.0,) * 768, limit=5)

    assert [hit.source_id for hit in hits] == ["asset_768"]


def test_search_exact_filters_by_scope(repository: SqliteVectorRepository):
    repository.upsert(record("asset_a", (1.0, 0.0, 0.0), scope_id="album1"))
    repository.upsert(record("asset_b", (1.0, 0.0, 0.0), scope_id="album2"))

    hits = repository.search_exact(space="visual", vector=(1.0, 0.0, 0.0), limit=5, scope_id="album1")

    assert [hit.source_id for hit in hits] == ["asset_a"]


def test_search_exact_returns_empty_for_an_empty_query(repository: SqliteVectorRepository):
    repository.upsert(record("asset_1", (1.0, 0.0, 0.0)))

    assert repository.search_exact(space="visual", vector=(), limit=5) == []


def test_empty_vector_is_rejected_loudly(repository: SqliteVectorRepository):
    """1.0 的空向量会被照单 upsert，整条链路不报错，事后手工清点才发现。

    这里改成硬拒绝：宁可在写入点炸，也不要在检索侧表现为「这张图就是搜不到」。
    """
    with pytest.raises(ValueError):
        repository.upsert(record("asset_empty", ()))


def test_metadata_round_trips(repository: SqliteVectorRepository):
    repository.upsert(record("asset_1", (1.0, 0.0, 0.0), metadata={"observation_id": "obs_1"}))

    hits = repository.search_exact(space="visual", vector=(1.0, 0.0, 0.0), limit=5)

    assert hits[0].metadata == {"observation_id": "obs_1"}
```
`apps/api/tests/unit/test_similarity.py`:

```python
"""余弦相似度的边界行为。"""

from __future__ import annotations

import pytest

from sentrix.domain.retrieval.similarity import cosine_similarity, dimension_matches


def test_identical_vectors_score_one():
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_opposite_vectors_score_minus_one():
    assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)


def test_orthogonal_vectors_score_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_scale_does_not_matter():
    assert cosine_similarity([1.0, 1.0], [10.0, 10.0]) == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ([], [1.0]),
        ([1.0], []),
        ([1.0, 2.0], [1.0]),
    ],
)
def test_length_mismatch_scores_zero(left, right):
    """维度不一致返回 0.0 而不是抛异常；是否跳过该候选由调用方决定。"""
    assert cosine_similarity(left, right) == 0.0


def test_zero_vector_scores_zero():
    assert cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_dimension_matches():
    assert dimension_matches([1.0] * 768, 768) is True
    assert dimension_matches([1.0] * 512, 768) is False
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_vector_repository.py apps/api/tests/unit/test_similarity.py -q -p no:warnings`

Expected:

```text
____ ERROR collecting apps/api/tests/integration/test_vector_repository.py _____
apps/api/tests/integration/test_vector_repository.py:7: in <module>
    from sentrix.domain.retrieval.ports import VectorRecord
E   ModuleNotFoundError: No module named 'sentrix.domain.retrieval.ports'
___________ ERROR collecting apps/api/tests/unit/test_similarity.py ____________
apps/api/tests/unit/test_similarity.py:7: in <module>
    from sentrix.domain.retrieval.similarity import cosine_similarity, dimension_matches
E   ModuleNotFoundError: No module named 'sentrix.domain.retrieval.similarity'
=========================== short test summary info ============================
ERROR apps/api/tests/integration/test_vector_repository.py
ERROR apps/api/tests/unit/test_similarity.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 写迁移文件**

`apps/api/src/sentrix/infrastructure/persistence/migrations/0003_memory_vectors.sql`:

```sql
-- 向量事实源。语义取自 1.0 backend/db.py:782 的 memory_vectors，两处结构改进：
--   1. 新增 dimension 列。1.0 只能把 vector_json 解析出来才知道维度，
--      「已写入的向量维度是否与当前模型一致」这件事在 SQL 层看不见。
--   2. 索引改成 (space, model_name) 与 (space, source_type, source_id)：
--      1.0 只索引了 space 单列，带模型名的查询用不上索引。
-- Qdrant 是派生索引、可重建；SQLite 才是权威。删库只需重跑一次重嵌。
CREATE TABLE memory_vectors (
    id TEXT PRIMARY KEY,
    scope_id TEXT NOT NULL DEFAULT 'home-default',
    space TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    model_name TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    vector_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(space, source_type, source_id, model_name)
);

CREATE INDEX idx_memory_vectors_space_model ON memory_vectors(space, model_name);
CREATE INDEX idx_memory_vectors_source ON memory_vectors(space, source_type, source_id);
CREATE INDEX idx_memory_vectors_scope ON memory_vectors(scope_id);
```

- [ ] **Step 4: 写端口与相似度**

`apps/api/src/sentrix/domain/retrieval/ports.py`（**替换为**）:

```python
"""检索域端口：权威向量存储、派生向量索引、视觉嵌入器。

三者的分工是 1.0 已经验证过的契约，2.0 原样保留：

- ``VectorRepository`` 是**权威**（SQLite 里的 memory_vectors），写入必须先到它；
- ``VectorIndex`` 是**派生镜像**（Qdrant），可重建、可缺席；
- 索引缺席或锁被占用时，检索必须回退到权威存储，并把「正在降级」这件事报出去。

1.0 的教训是降级被写进了日志但没人看：Qdrant 目录锁被 8091 独占时，第二个进程
只打印一行 ``vector search degrades to SQLite full scans``，接口照常返回空结果。
所以这里的 ``VectorIndexStatus`` 是端口的一部分，降级原因必须能一路传到 HTTP 响应。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """一条待写入的向量。``(space, source_type, source_id, model_name)`` 唯一。"""

    scope_id: str
    space: str
    source_type: str
    source_id: str
    model_name: str
    vector: tuple[float, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def dimension(self) -> int:
        return len(self.vector)


@dataclass(frozen=True, slots=True)
class VectorHit:
    source_id: str
    scope_id: str
    source_type: str
    model_name: str
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorIndexStatus:
    """索引健康度。``degraded`` 为真时必须是可行动的信息，不是一个布尔摆设。"""

    backend: str
    available: bool
    degraded: bool = False
    reason: str = ""


class VectorRepository(Protocol):
    """权威向量存储。SQLite 是唯一实现，也是索引重建的数据来源。"""

    def upsert(self, record: VectorRecord) -> None: ...

    def has_vector(self, *, space: str, source_type: str, source_id: str, model_name: str) -> bool: ...

    def search_exact(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def count(self, *, space: str) -> int: ...


class VectorIndex(Protocol):
    """派生向量索引。缺席不是错误，但必须被看见。"""

    def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    def search(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def status(self) -> VectorIndexStatus: ...


class VisualEmbedder(Protocol):
    """跨模态视觉嵌入：同一空间里的文本查询向量与图像向量。

    只管两个方向，不带设备、批次、模型元数据——``model_id`` 与 ``dimension``
    是索引兼容性校验的全部依据。
    """

    model_id: str
    dimension: int

    def embed_image(self, path: Path) -> Sequence[float]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...
```
`apps/api/src/sentrix/domain/retrieval/similarity.py`:

```python
"""向量相似度。纯数学，零 IO，放 domain 而不是仓储里。

1.0 把它写进 ``MemoryStore._cosine``（db.py:3515 附近），于是「怎么算相似」这件事
只存在于 SQLite 适配器内部，回退路径与主路径无法共用同一份判定。抽出来后，
精确回退与任何未来的内存索引用的是同一个函数。
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """余弦相似度；长度不等、任一为空、或任一模长为 0 时返回 0.0。

    返回 0.0 而不是抛异常：向量维度不一致是数据问题，检索应当跳过这条候选继续，
    而不是让整次查询崩掉——但调用方必须能看见它（见 ``dimension_matches``）。
    """
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = 0.0
    left_norm = 0.0
    right_norm = 0.0
    for a, b in zip(left, right, strict=True):
        dot += a * b
        left_norm += a * a
        right_norm += b * b
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot / (math.sqrt(left_norm) * math.sqrt(right_norm))


def dimension_matches(vector: Sequence[float], expected: int) -> bool:
    """查询向量与索引维度是否一致——不一致时检索必须停下来说清楚，不能返回空。"""
    return len(vector) == expected
```

`VisualEmbedder` 的 `model_id` / `dimension` 声明成**只读 property** 而不是裸属性：适配器用 `@property` 实现，mypy 的 `arg-type` 不允许只读 property 去满足"可写变量"的协议约定。这一点在 Task 29 接容器时会以 `error: Argument "embedder" ... has incompatible type` 的形式暴露出来——那时改的是协议，不是实现。

- [ ] **Step 5: 写 SQLite 仓储**

`apps/api/src/sentrix/infrastructure/persistence/repositories/vectors.py`（**替换为**）:

```python
"""memory_vectors 的 SQLite 仓储。SQL 只出现在这一层。

精确回退（``search_exact``）是**功能**不是兜底：Qdrant 目录被别的进程锁住、
collection 还没建、或者维度对不上时，检索必须还能给出正确答案，只是慢。
1.0 的同类回退存在但把降级写进了没人看的日志；这里由调用方（application 层）
拿 ``VectorIndexStatus`` 决定要不要告诉用户。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sentrix.domain.retrieval.ports import VectorHit, VectorRecord
from sentrix.domain.retrieval.similarity import cosine_similarity
from sentrix.infrastructure.persistence.database import Database

_COLUMNS = (
    "id, scope_id, space, source_type, source_id, model_name, dimension,"
    " vector_json, metadata_json, created_at, updated_at"
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode_list(raw: str | None) -> list[float]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [float(item) for item in value]


def _decode_map(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return dict(value) if isinstance(value, dict) else {}


class SqliteVectorRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    def upsert(self, record: VectorRecord) -> None:
        if not record.vector:
            raise ValueError("拒绝写入空向量：1.0 的空向量会被静默 upsert，事后无人发现")
        timestamp = _now()
        with self._database.connect() as connection, connection:
            connection.execute(
                "INSERT INTO memory_vectors ("
                f" {_COLUMNS}"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(space, source_type, source_id, model_name) DO UPDATE SET"
                " scope_id = excluded.scope_id, dimension = excluded.dimension,"
                " vector_json = excluded.vector_json, metadata_json = excluded.metadata_json,"
                " updated_at = excluded.updated_at",
                (
                    f"vec_{uuid.uuid4().hex[:12]}",
                    record.scope_id,
                    record.space,
                    record.source_type,
                    record.source_id,
                    record.model_name,
                    record.dimension,
                    json.dumps([float(value) for value in record.vector]),
                    json.dumps(dict(record.metadata), ensure_ascii=False, sort_keys=True),
                    timestamp,
                    timestamp,
                ),
            )

    def has_vector(self, *, space: str, source_type: str, source_id: str, model_name: str) -> bool:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM memory_vectors WHERE space = ? AND source_type = ?"
                " AND source_id = ? AND model_name = ? LIMIT 1",
                (space, source_type, source_id, model_name),
            ).fetchone()
        return row is not None

    def count(self, *, space: str) -> int:
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM memory_vectors WHERE space = ?", (space,)
            ).fetchone()
        return int(row[0]) if row else 0

    def search_exact(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]:
        if not vector or limit < 1:
            return []
        rows = self._fetch(space=space, scope_id=scope_id, model_name=model_name)
        scored: list[VectorHit] = []
        for row in rows:
            candidate = _decode_list(row["vector_json"])
            # 维度不一致的行直接跳过：拿 768 维查询去和 512 维旧向量算余弦，
            # 结果恒为 0.0，会以「相似度很低」的形态混进结果里而不是被识别为脏数据。
            if len(candidate) != len(vector):
                continue
            scored.append(
                VectorHit(
                    source_id=row["source_id"],
                    scope_id=row["scope_id"],
                    source_type=row["source_type"],
                    model_name=row["model_name"],
                    score=cosine_similarity(vector, candidate),
                    metadata=_decode_map(row["metadata_json"]),
                )
            )
        # 同分按 source_id 兜底，保证同一份数据每次给出同一个名次。
        scored.sort(key=lambda item: (-item.score, item.source_id))
        return scored[:limit]

    def _fetch(self, *, space: str, scope_id: str | None, model_name: str | None) -> list[sqlite3.Row]:
        sql = f"SELECT {_COLUMNS} FROM memory_vectors WHERE space = ?"
        params: list[Any] = [space]
        if scope_id:
            sql += " AND scope_id = ?"
            params.append(scope_id)
        if model_name:
            sql += " AND model_name = ?"
            params.append(model_name)
        with self._database.connect() as connection:
            return list(connection.execute(sql, tuple(params)).fetchall())
```

- [ ] **Step 6: 更新 domain/retrieval/__init__.py 与容器**

`apps/api/src/sentrix/domain/retrieval/__init__.py`（**替换为**）:

```python
"""检索域：查询契约、候选融合、向量端口与相似度。纯规则，零 IO。"""

from sentrix.domain.retrieval.fusion import (
    ANCHOR_BOOST,
    DEFAULT_CHANNEL_WEIGHTS,
    RRF_K,
    evidence_class_for,
    fuse,
    weighted_rrf_score,
)
from sentrix.domain.retrieval.models import CandidateHit, FusedCandidate
from sentrix.domain.retrieval.ports import (
    VectorHit,
    VectorIndex,
    VectorIndexStatus,
    VectorRecord,
    VectorRepository,
    VisualEmbedder,
)
from sentrix.domain.retrieval.similarity import cosine_similarity, dimension_matches

__all__ = [
    "ANCHOR_BOOST",
    "DEFAULT_CHANNEL_WEIGHTS",
    "RRF_K",
    "CandidateHit",
    "FusedCandidate",
    "VectorHit",
    "VectorIndex",
    "VectorIndexStatus",
    "VectorRecord",
    "VectorRepository",
    "VisualEmbedder",
    "cosine_similarity",
    "dimension_matches",
    "evidence_class_for",
    "fuse",
    "weighted_rrf_score",
]
```
`apps/api/src/sentrix/bootstrap/container.py`（**替换为**）:

```python
"""The single wiring point.

Nothing else in the codebase constructs an adapter.  Domain and application code
receive ports; only this module knows which concrete class implements them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sentrix.config import Settings
from sentrix.domain.media.ports import PreviewEncoder
from sentrix.infrastructure.media.image_io import PillowPreviewEncoder
from sentrix.infrastructure.media.metadata import PillowMetadataReader
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.spaces import SqliteSpaceRepository
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "persistence" / "migrations"


@dataclass(frozen=True)
class Container:
    settings: Settings
    database: Database
    spaces: SqliteSpaceRepository
    asset_repository: SqliteAssetRepository
    observation_repository: SqliteObservationRepository
    media_storage: FileSystemMediaStorage
    metadata_reader: PillowMetadataReader
    preview_encoder: PreviewEncoder
    vector_repository: SqliteVectorRepository

    @classmethod
    def build(cls, settings: Settings) -> Container:
        database = Database(settings.database_path, MIGRATIONS_DIR)
        return cls(
            settings=settings,
            database=database,
            spaces=SqliteSpaceRepository(database),
            asset_repository=SqliteAssetRepository(database),
            observation_repository=SqliteObservationRepository(database),
            media_storage=FileSystemMediaStorage(settings.media_dir),
            metadata_reader=PillowMetadataReader(),
            preview_encoder=PillowPreviewEncoder(),
            vector_repository=SqliteVectorRepository(database),
        )

    def database_healthy(self) -> bool:
        try:
            with self.database.connect() as connection:
                connection.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
```

- [ ] **Step 7: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_vector_repository.py apps/api/tests/unit/test_similarity.py -p no:warnings`

Expected:

```text
....................                                                     [100%]
20 passed
```

- [ ] **Step 8: 更新 module-map.md**

加入三行：

```markdown
| `sentrix/domain/retrieval/ports.py` | 向量端口：`VectorRepository`（权威）/ `VectorIndex`（派生镜像）/ `VisualEmbedder`（跨模态），以及 `VectorRecord` / `VectorHit` / `VectorIndexStatus` | domain | — | `sentrix/domain/retrieval/__init__.py`、`sentrix/infrastructure/persistence/repositories/vectors.py` |
| `sentrix/domain/retrieval/similarity.py` | 余弦相似度与维度一致性判定；1.0 把它埋在 `MemoryStore._cosine` 里，精确回退与主路径各算一套 | domain | — | `sentrix/domain/retrieval/__init__.py`、`sentrix/infrastructure/persistence/repositories/vectors.py` |
| `sentrix/infrastructure/persistence/repositories/vectors.py` | `SqliteVectorRepository`：memory_vectors 的权威存储与精确回退检索 | infrastructure | `sentrix/domain/retrieval/ports.py`、`sentrix/domain/retrieval/similarity.py`、`sentrix/infrastructure/persistence/database.py` | `sentrix/bootstrap/container.py` |
```

并**改旧行**（新文件是它们的反向依赖，漏改就红）：

```markdown
| `sentrix/domain/retrieval/__init__.py` | 包标记与检索域导出 | domain | `sentrix/domain/retrieval/fusion.py`、`sentrix/domain/retrieval/models.py`、`sentrix/domain/retrieval/ports.py`、`sentrix/domain/retrieval/similarity.py` | — |
| `sentrix/infrastructure/persistence/database.py` | SQLite 连接工厂与版本化迁移执行器 | infrastructure | — | `sentrix/bootstrap/container.py`、`sentrix/infrastructure/persistence/repositories/assets.py`、`sentrix/infrastructure/persistence/repositories/spaces.py`、`sentrix/infrastructure/persistence/repositories/vectors.py` |
| `sentrix/bootstrap/container.py` | 唯一依赖装配点；只有这里构造适配器 | bootstrap | `sentrix/config.py`、`sentrix/domain/media/ports.py`、`sentrix/infrastructure/media/image_io.py`、`sentrix/infrastructure/media/metadata.py`、`sentrix/infrastructure/media/storage.py`、`sentrix/infrastructure/persistence/database.py`、`sentrix/infrastructure/persistence/repositories/assets.py`、`sentrix/infrastructure/persistence/repositories/spaces.py`、`sentrix/infrastructure/persistence/repositories/vectors.py` | `sentrix/api/deps.py`、`sentrix/api/routes/media.py`、`sentrix/main.py` |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 9: 登记能力**

```json
{"capability":"向量权威存储（memory_vectors）","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/db.py","source_symbol":"upsert_vector","target_path":"apps/api/src/sentrix/infrastructure/persistence/repositories/vectors.py","verification":"apps/api/tests/integration/test_vector_repository.py","status":"verified"}
```

- [ ] **Step 10: 全量门禁**

Run: `make ci`

Expected:

```text
Required test coverage of 85.0% reached. Total coverage: 97.19%
95 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

---

## Task 27: Qdrant 派生索引适配器

**依赖：** Task 26（`VectorIndex` 端口已定义）；Step 1 在本任务补 `VectorIndexUnavailable`。

**Files:**
- Create: `apps/api/src/sentrix/infrastructure/vector/__init__.py`
- Create: `apps/api/src/sentrix/infrastructure/vector/qdrant_index.py`
- Create: `apps/api/tests/integration/test_qdrant_index.py`
- Modify: `apps/api/src/sentrix/config.py`
- Modify: `apps/api/pyproject.toml`（声明 `qdrant-client`）
- Modify: `apps/api/src/sentrix/bootstrap/container.py`
- Modify: `apps/api/src/sentrix/domain/retrieval/ports.py`（补 `VectorIndexUnavailable`）
- Modify: `apps/api/src/sentrix/domain/retrieval/__init__.py`（导出 `VectorIndexUnavailable`）
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

**S2 的第一条硬约束在这里落地：Qdrant 必须用独立目录。**

1.0 用嵌入式模式 `QdrantClient(path="data/qdrant")`，该目录的锁被 8091 进程长期持有。实测（qdrant-client 1.19.1）：第二个进程打开同一目录会抛 `RuntimeError: Storage folder ... is already accessed by another instance of Qdrant client.`——**是抛异常，不是"启动不了"**。1.0 把这件事变成一行日志然后继续，接口照常返回空结果；2.0 把它翻成 `VectorIndexUnavailable`，由 application 层决定降级并把原因报到 HTTP 响应里。

另外两处与 1.0 的差异：collection 按 `(space, model, dimension)` 分而不是按 scope 分（1.0 在生产上长到 634 个 collection）；`status()` / `count()` 是端口的一部分，让"索引落后于权威"可判定（1.0 的 `data/ann/visual.hnsw` 只有 1,170 条而真实 10,590 条，就是因为没人比对过这两个数）。

- [ ] **Step 1: 在 ports.py 补 `VectorIndexUnavailable`**

`VectorIndexUnavailable` 是「派生索引不可用」这个**预期内失败**的类型化出口。
它必须在适配器之前定义好——下一步的测试就从 `ports` 里 import 它，而不是从适配器里，
所以缺了它红灯会变成 `ImportError` 而不是 `ModuleNotFoundError`，指错方向。

`apps/api/src/sentrix/domain/retrieval/ports.py`（**替换为**）:

```python
"""检索域端口：权威向量存储、派生向量索引、视觉嵌入器。

三者的分工是 1.0 已经验证过的契约，2.0 原样保留：

- ``VectorRepository`` 是**权威**（SQLite 里的 memory_vectors），写入必须先到它；
- ``VectorIndex`` 是**派生镜像**（Qdrant），可重建、可缺席；
- 索引缺席或锁被占用时，检索必须回退到权威存储，并把「正在降级」这件事报出去。

1.0 的教训是降级被写进了日志但没人看：Qdrant 目录锁被 8091 独占时，第二个进程
只打印一行 ``vector search degrades to SQLite full scans``，接口照常返回空结果。
所以这里的 ``VectorIndexStatus`` 是端口的一部分，降级原因必须能一路传到 HTTP 响应。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """一条待写入的向量。``(space, source_type, source_id, model_name)`` 唯一。"""

    scope_id: str
    space: str
    source_type: str
    source_id: str
    model_name: str
    vector: tuple[float, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def dimension(self) -> int:
        return len(self.vector)


@dataclass(frozen=True, slots=True)
class VectorHit:
    source_id: str
    scope_id: str
    source_type: str
    model_name: str
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorIndexStatus:
    """索引健康度。``degraded`` 为真时必须是可行动的信息，不是一个布尔摆设。"""

    backend: str
    available: bool
    degraded: bool = False
    reason: str = ""


class VectorRepository(Protocol):
    """权威向量存储。SQLite 是唯一实现，也是索引重建的数据来源。"""

    def upsert(self, record: VectorRecord) -> None: ...

    def has_vector(self, *, space: str, source_type: str, source_id: str, model_name: str) -> bool: ...

    def search_exact(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def count(self, *, space: str) -> int: ...


class VectorIndexUnavailable(RuntimeError):
    """派生索引不可用（目录被占用、权限、磁盘满）。

    这是**预期内**的失败，所以必须是一个调用方能按类型捕获的异常，而不是
    「返回 0 / 返回空列表」——后者在调用点看不出区别，1.0 的静默降级就是这么来的。
    """


class VectorIndex(Protocol):
    """派生向量索引。缺席不是错误，但必须被看见。

    ``upsert`` / ``search`` / ``count`` 在索引不可用时抛 ``VectorIndexUnavailable``，
    由 application 层决定是回退到权威存储还是把错误暴露出去——适配器不替它决定。
    """

    def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    def search(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def count(self, *, space: str, model_name: str | None = None) -> int: ...

    def status(self) -> VectorIndexStatus: ...


class VisualEmbedder(Protocol):
    """跨模态视觉嵌入：同一空间里的文本查询向量与图像向量。

    只管两个方向，不带设备、批次、模型元数据——``model_id`` 与 ``dimension``
    是索引兼容性校验的全部依据。
    """

    model_id: str
    dimension: int

    def embed_image(self, path: Path) -> Sequence[float]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...
```
`apps/api/src/sentrix/domain/retrieval/__init__.py`（**替换为**）:

```python
"""检索域：查询契约、候选融合、向量端口与相似度。纯规则，零 IO。"""

from sentrix.domain.retrieval.fusion import (
    ANCHOR_BOOST,
    DEFAULT_CHANNEL_WEIGHTS,
    RRF_K,
    evidence_class_for,
    fuse,
    weighted_rrf_score,
)
from sentrix.domain.retrieval.models import CandidateHit, FusedCandidate
from sentrix.domain.retrieval.ports import (
    VectorHit,
    VectorIndex,
    VectorIndexStatus,
    VectorIndexUnavailable,
    VectorRecord,
    VectorRepository,
    VisualEmbedder,
)
from sentrix.domain.retrieval.similarity import cosine_similarity, dimension_matches

__all__ = [
    "ANCHOR_BOOST",
    "DEFAULT_CHANNEL_WEIGHTS",
    "RRF_K",
    "CandidateHit",
    "FusedCandidate",
    "VectorHit",
    "VectorIndex",
    "VectorIndexStatus",
    "VectorIndexUnavailable",
    "VectorRecord",
    "VectorRepository",
    "VisualEmbedder",
    "cosine_similarity",
    "dimension_matches",
    "evidence_class_for",
    "fuse",
    "weighted_rrf_score",
]
```

- [ ] **Step 2: 先写失败测试**

`apps/api/tests/integration/test_qdrant_index.py`:

```python
"""真 Qdrant（嵌入式、临时目录）下的派生索引测试。

最后一条是本切片的核心：**目录被别人占用时必须降级得响亮**。1.0 的对应行为是
打印一行日志、接口照常返回空结果，于是「检索变差了」这件事在用户侧完全不可见。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sentrix.domain.retrieval.ports import VectorIndexUnavailable, VectorRecord
from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex

MODEL = "chinese-clip-ViT-L-14"
DIM = 4

# 子进程：打开目录、拿走锁、告诉父进程「就绪」，然后一直握着。
_LOCK_HOLDER = """
import sys, time
from qdrant_client import QdrantClient
client = QdrantClient(path=sys.argv[1])
client.get_collections()
print("ready", flush=True)
time.sleep(60)
"""


@pytest.fixture
def index(tmp_path: Path):
    instance = QdrantVectorIndex(tmp_path / "qdrant")
    yield instance
    instance.close()


def record(
    source_id: str,
    vector: tuple[float, ...] = (1.0, 0.0, 0.0, 0.0),
    *,
    scope_id: str = "home-default",
    source_type: str = "asset",
    model_name: str = MODEL,
    metadata: dict[str, object] | None = None,
) -> VectorRecord:
    return VectorRecord(
        scope_id=scope_id,
        space="visual",
        source_type=source_type,
        source_id=source_id,
        model_name=model_name,
        vector=vector,
        metadata=metadata or {},
    )


def test_upsert_then_search_returns_cosine_hits(index: QdrantVectorIndex):
    index.upsert([record("asset_near", (1.0, 0.0, 0.0, 0.0)), record("asset_far", (0.0, 0.0, 0.0, 1.0))])

    hits = index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=2, model_name=MODEL)

    assert [hit.source_id for hit in hits] == ["asset_near", "asset_far"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[0].metadata == {}


def test_upsert_is_idempotent_on_the_source_identity(index: QdrantVectorIndex):
    """point id 由 (space, source_type, source_id, model) 决定，重复写是覆盖不是新增。"""
    index.upsert([record("asset_1", (1.0, 0.0, 0.0, 0.0))])
    index.upsert([record("asset_1", (0.0, 1.0, 0.0, 0.0))])

    assert index.count(space="visual", model_name=MODEL) == 1
    hits = index.search(space="visual", vector=(0.0, 1.0, 0.0, 0.0), limit=1, model_name=MODEL)
    assert hits[0].score == pytest.approx(1.0)


def test_search_filters_by_scope(index: QdrantVectorIndex):
    index.upsert([record("asset_a", scope_id="album1"), record("asset_b", scope_id="album2")])

    hits = index.search(
        space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, scope_id="album1", model_name=MODEL
    )

    assert [hit.source_id for hit in hits] == ["asset_a"]


def test_models_do_not_share_a_collection(index: QdrantVectorIndex):
    """两套模型的向量维度可能相同，混在一个 collection 里就再也分不开。"""
    index.upsert([record("asset_new", model_name=MODEL)])
    index.upsert([record("asset_old", (0.0, 1.0, 0.0, 0.0), model_name="ViT-B-32")])

    hits = index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, model_name=MODEL)

    assert [hit.source_id for hit in hits] == ["asset_new"]
    assert index.count(space="visual", model_name=MODEL) == 1
    assert index.count(space="visual", model_name="ViT-B-32") == 1


def test_dimension_changes_the_collection(index: QdrantVectorIndex):
    index.upsert([record("asset_768", (1.0,) * DIM)])
    index.upsert([record("asset_1024", (1.0,) * 8)])

    hits = index.search(space="visual", vector=(1.0,) * DIM, limit=5, model_name=MODEL)

    assert [hit.source_id for hit in hits] == ["asset_768"]


def test_metadata_round_trips_through_the_payload(index: QdrantVectorIndex):
    index.upsert([record("asset_1", metadata={"observation_id": "obs_1"})])

    hits = index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=1, model_name=MODEL)

    assert hits[0].metadata == {"observation_id": "obs_1"}
    assert hits[0].scope_id == "home-default"


def test_status_is_available_on_a_fresh_directory(index: QdrantVectorIndex):
    status = index.status()

    assert status.backend == "qdrant"
    assert status.available is True
    assert status.degraded is False


def test_empty_vector_is_rejected_loudly(index: QdrantVectorIndex):
    with pytest.raises(ValueError):
        index.upsert([record("asset_empty", ())])


def test_search_on_an_empty_index_returns_no_hits(index: QdrantVectorIndex):
    assert index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, model_name=MODEL) == []


def test_locked_directory_degrades_loudly(tmp_path: Path):
    """目录被另一个进程占用：status 报 degraded + 原因，操作抛 VectorIndexUnavailable。

    qdrant-client 在嵌入式模式下对目录加独占锁（1.0 的 8091 就长期握着
    ``data/qdrant``）。2.0 必须把它变成一个**能被看见**的状态，而不是一行日志。
    """
    qdrant_dir = tmp_path / "locked"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(qdrant_dir)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready", "持锁子进程没有就绪"

        index = QdrantVectorIndex(qdrant_dir)
        status = index.status()

        assert status.available is False
        assert status.degraded is True
        assert "already accessed" in status.reason

        with pytest.raises(VectorIndexUnavailable):
            index.search(space="visual", vector=(1.0, 0.0, 0.0, 0.0), limit=5, model_name=MODEL)
        with pytest.raises(VectorIndexUnavailable):
            index.upsert([record("asset_1")])
        with pytest.raises(VectorIndexUnavailable):
            index.count(space="visual", model_name=MODEL)
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_2_0_qdrant_dir_is_not_1_0_s(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """默认目录由 data_dir 派生，永远不会指到 1.0 的 data/qdrant。"""
    from sentrix.config import Settings

    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    settings = Settings()

    assert settings.qdrant_dir == tmp_path / "qdrant"
    assert settings.qdrant_dir != Path("/home/asus/Github/Sentrix-Home-Web/data/qdrant")
```

- [ ] **Step 3: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_qdrant_index.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.infrastructure.vector'
ERROR apps/api/tests/integration/test_qdrant_index.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 4: 写适配器**

`apps/api/src/sentrix/infrastructure/vector/__init__.py`:

```python
"""向量索引适配器。索引是派生实现，权威始终在 SQLite。"""
```
`apps/api/src/sentrix/infrastructure/vector/qdrant_index.py`:

```python
"""Qdrant 派生向量索引（嵌入式本地模式）。

对应 1.0 ``backend/qdrant_memory.py``，三处刻意的差异：

1. **目录必须独立**。嵌入式 Qdrant 对目录加独占锁，1.0 的 ``data/qdrant`` 被
   8091 进程长期持有。第二个进程打开同一目录时 qdrant-client 会抛
   ``RuntimeError: Storage folder ... is already accessed by another instance``。
   2.0 把这个异常翻成 ``VectorIndexUnavailable``——**降级可以，静默不行**。
   1.0 是打印一行日志然后继续，接口照常返回空结果。
2. **collection 按 (space, model, dimension) 分，不按 scope 分**。1.0 按 scope 切，
   生产上长到 634 个 collection；每张相册一个 HNSW 图的收益抵不过这个数量级。
   scope 过滤改走 payload，权威性由 SQLite 侧的 scope 列保证。
3. **可观测**：``status()`` 与 ``count()`` 是端口的一部分，应用层据此判断索引是否
   落后于权威存储，从而决定是否回退——1.0 的 ``data/ann/visual.hnsw`` 只有 1,170
   条而真实 10,590 条，就是因为没人比对过这两个数。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sentrix.domain.retrieval.ports import (
    VectorHit,
    VectorIndexStatus,
    VectorIndexUnavailable,
    VectorRecord,
)

DEFAULT_PREFIX = "sentrix_home"
# 与 1.0 的 uuid 命名空间不同：同一个 source_id 在 2.0 的索引里是不同的 point，
# 免得两套系统的点互相覆盖。
_POINT_NAMESPACE = uuid.UUID("1f6f2a4e-0f9c-4d2f-9a3b-6c9f1f2b7d51")
_BATCH_SIZE = 256


def _safe(value: str, limit: int = 24) -> str:
    text = re.sub(r"[^a-zA-Z0-9_]+", "_", str(value or "").lower()).strip("_")
    return (text or "default")[:limit]


class QdrantVectorIndex:
    def __init__(self, path: Path, prefix: str = DEFAULT_PREFIX) -> None:
        self.path = Path(path)
        self.prefix = prefix
        self._client: Any = None
        self._error = ""

    # ---- 客户端 ----------------------------------------------------------

    def _get_client(self) -> Any:
        """惰性取客户端。失败后缓存原因，不再每次重试——重试只是反复抛同一个异常。"""
        if self._client is not None:
            return self._client
        if self._error:
            raise VectorIndexUnavailable(self._error)
        from qdrant_client import QdrantClient

        try:
            self.path.mkdir(parents=True, exist_ok=True)
            self._client = QdrantClient(path=str(self.path))
        except Exception as error:  # noqa: BLE001 - 目录锁/权限/磁盘满都归为「索引不可用」
            self._error = f"{type(error).__name__}: {error}"
            raise VectorIndexUnavailable(self._error) from error
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ---- 端口 ------------------------------------------------------------

    def status(self) -> VectorIndexStatus:
        try:
            self._get_client()
        except VectorIndexUnavailable as error:
            return VectorIndexStatus(backend="qdrant", available=False, degraded=True, reason=str(error))
        return VectorIndexStatus(backend="qdrant", available=True)

    def upsert(self, records: Sequence[VectorRecord]) -> int:
        if not records:
            return 0
        for record in records:
            if not record.vector:
                raise ValueError("拒绝写入空向量：空向量在检索侧表现为「这张图就是搜不到」")
        client = self._get_client()
        from qdrant_client import models

        grouped: dict[str, list[VectorRecord]] = {}
        for record in records:
            grouped.setdefault(
                self._collection(record.space, record.model_name, record.dimension), []
            ).append(record)

        written = 0
        for collection, group in grouped.items():
            sample = group[0]
            self._ensure_collection(client, collection, sample.space, sample.dimension)
            points = [
                models.PointStruct(
                    id=self._point_id(record),
                    vector={record.space: [float(value) for value in record.vector]},
                    payload={
                        "scope_id": record.scope_id,
                        "space": record.space,
                        "source_type": record.source_type,
                        "source_id": record.source_id,
                        "model_name": record.model_name,
                        "metadata": dict(record.metadata),
                    },
                )
                for record in group
            ]
            for offset in range(0, len(points), _BATCH_SIZE):
                batch = points[offset : offset + _BATCH_SIZE]
                client.upsert(collection_name=collection, points=batch, wait=True)
                written += len(batch)
        return written

    def search(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]:
        if not vector or limit < 1:
            return []
        client = self._get_client()
        from qdrant_client import models

        query_filter = None
        if scope_id:
            query_filter = models.Filter(
                must=[models.FieldCondition(key="scope_id", match=models.MatchValue(value=scope_id))]
            )

        hits: list[VectorHit] = []
        for collection in self._collections_for(client, space, model_name, len(vector)):
            response = client.query_points(
                collection_name=collection,
                query=[float(value) for value in vector],
                using=space,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
            hits.extend(self._hit(point) for point in response.points)
        hits.sort(key=lambda item: (-item.score, item.source_id))
        return hits[:limit]

    def count(self, *, space: str, model_name: str | None = None) -> int:
        client = self._get_client()
        total = 0
        for collection in self._collections_for(client, space, model_name, None):
            info = client.get_collection(collection)
            total += int(info.points_count or 0)
        return total

    # ---- 内部 ------------------------------------------------------------

    def _collection(self, space: str, model_name: str, dimension: int) -> str:
        return f"{self.prefix}_{_safe(space)}_{dimension}_{_safe(model_name)}"

    def _collection_prefix(self, space: str) -> str:
        return f"{self.prefix}_{_safe(space)}_"

    def _collections_for(
        self,
        client: Any,
        space: str,
        model_name: str | None,
        dimension: int | None,
    ) -> list[str]:
        prefix = self._collection_prefix(space)
        names = [item.name for item in client.get_collections().collections if item.name.startswith(prefix)]
        if model_name:
            suffix = f"_{_safe(model_name)}"
            names = [name for name in names if name.endswith(suffix)]
        if dimension is not None:
            marker = f"_{dimension}_"
            names = [name for name in names if marker in name]
        return sorted(names)

    def _ensure_collection(self, client: Any, collection: str, space: str, dimension: int) -> None:
        from qdrant_client import models

        existing = {item.name for item in client.get_collections().collections}
        if collection in existing:
            return
        client.create_collection(
            collection_name=collection,
            vectors_config={space: models.VectorParams(size=dimension, distance=models.Distance.COSINE)},
        )

    @staticmethod
    def _point_id(record: VectorRecord) -> str:
        identity = f"{record.space}\0{record.source_type}\0{record.source_id}\0{record.model_name}"
        return str(uuid.uuid5(_POINT_NAMESPACE, identity))

    @staticmethod
    def _hit(point: Any) -> VectorHit:
        payload = dict(point.payload or {})
        return VectorHit(
            source_id=str(payload.get("source_id") or ""),
            scope_id=str(payload.get("scope_id") or ""),
            source_type=str(payload.get("source_type") or ""),
            model_name=str(payload.get("model_name") or ""),
            score=float(point.score),
            metadata=dict(payload.get("metadata") or {}),
        )
```

- [ ] **Step 5: 依赖声明与安装**

`qdrant-client` 是**本任务自己的**依赖：`qdrant_index.py` 直接 import 它。
它不属于 Task 28——Task 28 只负责 `torch` / `torchvision` / `cn-clip` 三项。
声明必须在跑绿灯之前落地，否则从零照计划执行到这里会缺包。

`apps/api/pyproject.toml` 的 `dependencies` 追加一项：

```toml
    # 嵌入式 Qdrant 做派生索引；权威始终是 SQLite 的 memory_vectors。
    "qdrant-client>=1.12,<2",
```

Run: `.venv/bin/pip install -e "apps/api[dev]"`

- [ ] **Step 6: config 加 `qdrant_dir`**

`apps/api/src/sentrix/config.py`（**替换为**）:

```python
"""Typed configuration.

Only ``SENTRIX_DATA_DIR`` and a handful of connection settings are configurable.
Everything else is derived, because 1.0's failure mode was ~126 env flags whose
values disagreed with each other and with the code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENTRIX_", extra="forbid")

    data_dir: Path = Field(default=Path("data"))

    api_port: int = 9598
    api_host: str = "0.0.0.0"

    # 2.0 reuses 1.0's already-running model services; it never manages them.
    vlm_base_url: str = "http://127.0.0.1:8100/v1"
    text_embedder_url: str = "http://127.0.0.1:8101"

    def model_post_init(self, _context: Any, /) -> None:  # noqa: D105
        self.data_dir = self.data_dir.expanduser().resolve()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def database_path(self) -> Path:
        return self.data_dir / "sentrix.db"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preview_dir(self) -> Path:
        return self.data_dir / "previews"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def qdrant_dir(self) -> Path:
        """派生向量索引的目录。

        **必须由 data_dir 派生**，不允许单独配路径。1.0 的 ``data/qdrant`` 被 8091
        进程以 flock 独占（嵌入式 Qdrant 的目录只能一个进程用），2.0 若指过去，
        第二个实例起不来——而且是那种「起了、只是检索悄悄降级成全表扫」的坏法。
        派生自 data_dir 意味着换数据目录就整体换，不存在「只把索引指到 1.0」的口子。
        """
        return self.data_dir / "qdrant"

    def ensure_directories(self) -> None:
        for path in (self.data_dir, self.media_dir, self.preview_dir):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    return Settings()
```

- [ ] **Step 7: 容器装配**

把 `container.py` 改成（相对 Task 26 只多了 `vector_index` 一项）：

`apps/api/src/sentrix/bootstrap/container.py`（**替换为**）:

```python
"""The single wiring point.

Nothing else in the codebase constructs an adapter.  Domain and application code
receive ports; only this module knows which concrete class implements them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sentrix.config import Settings
from sentrix.domain.media.ports import PreviewEncoder
from sentrix.infrastructure.media.image_io import PillowPreviewEncoder
from sentrix.infrastructure.media.metadata import PillowMetadataReader
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.spaces import SqliteSpaceRepository
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository
from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "persistence" / "migrations"


@dataclass(frozen=True)
class Container:
    settings: Settings
    database: Database
    spaces: SqliteSpaceRepository
    asset_repository: SqliteAssetRepository
    observation_repository: SqliteObservationRepository
    media_storage: FileSystemMediaStorage
    metadata_reader: PillowMetadataReader
    preview_encoder: PreviewEncoder
    vector_repository: SqliteVectorRepository
    vector_index: QdrantVectorIndex

    @classmethod
    def build(cls, settings: Settings) -> Container:
        database = Database(settings.database_path, MIGRATIONS_DIR)
        return cls(
            settings=settings,
            database=database,
            spaces=SqliteSpaceRepository(database),
            asset_repository=SqliteAssetRepository(database),
            observation_repository=SqliteObservationRepository(database),
            media_storage=FileSystemMediaStorage(settings.media_dir),
            metadata_reader=PillowMetadataReader(),
            preview_encoder=PillowPreviewEncoder(),
            vector_repository=SqliteVectorRepository(database),
            vector_index=QdrantVectorIndex(settings.qdrant_dir),
        )

    def database_healthy(self) -> bool:
        try:
            with self.database.connect() as connection:
                connection.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
```


- [ ] **Step 8: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_qdrant_index.py -p no:warnings`

Expected:

```text
...........                                                              [100%]
11 passed
```

其中 `test_locked_directory_degrades_loudly` 会真的 fork 一个子进程占住目录再断言降级——那是本切片最不能省的一条。

- [ ] **Step 9: 更新 module-map.md**

加入两行：

```markdown
| `sentrix/infrastructure/vector/__init__.py` | 包标记 | infrastructure | — | — |
| `sentrix/infrastructure/vector/qdrant_index.py` | `QdrantVectorIndex`：嵌入式 Qdrant 派生索引；目录被占用时抛 `VectorIndexUnavailable` 而不是静默降级 | infrastructure | `sentrix/domain/retrieval/ports.py` | `sentrix/bootstrap/container.py` |
```

并**改旧行**（反向依赖，漏改就红）：

```markdown
| `sentrix/bootstrap/container.py` | 唯一依赖装配点；只有这里构造适配器 | bootstrap | `sentrix/config.py`、`sentrix/domain/media/ports.py`、`sentrix/infrastructure/media/image_io.py`、`sentrix/infrastructure/media/metadata.py`、`sentrix/infrastructure/media/storage.py`、`sentrix/infrastructure/persistence/database.py`、`sentrix/infrastructure/persistence/repositories/assets.py`、`sentrix/infrastructure/persistence/repositories/spaces.py`、`sentrix/infrastructure/persistence/repositories/vectors.py`、`sentrix/infrastructure/vector/qdrant_index.py` | `sentrix/api/deps.py`、`sentrix/api/routes/media.py`、`sentrix/main.py` |
| `sentrix/domain/retrieval/ports.py` | 向量端口：`VectorRepository`（权威）/ `VectorIndex`（派生镜像）/ `VisualEmbedder`（跨模态），以及 `VectorRecord` / `VectorHit` / `VectorIndexStatus` | domain | — | `sentrix/domain/retrieval/__init__.py`、`sentrix/infrastructure/persistence/repositories/vectors.py`、`sentrix/infrastructure/vector/qdrant_index.py` |
```

并改旧行：`sentrix/domain/retrieval/ports.py` 的「被谁依赖」加上 `sentrix/infrastructure/vector/qdrant_index.py`；`sentrix/bootstrap/container.py` 的「依赖谁」加上 `sentrix/infrastructure/vector/qdrant_index.py`。

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 10: 登记能力**

```json
{"capability":"Qdrant 派生向量索引","origin_kind":"adapted","source_commit":"2d2cee7c","source_path":"backend/qdrant_memory.py","source_symbol":"QdrantMemoryIndex.upsert","target_path":"apps/api/src/sentrix/infrastructure/vector/qdrant_index.py","verification":"apps/api/tests/integration/test_qdrant_index.py","status":"verified"}
```

- [ ] **Step 11: 全量门禁**

Run: `make ci`

Expected:

```text
Required test coverage of 85.0% reached. Total coverage: 97.20%
106 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

---

## Task 28: Chinese-CLIP 视觉嵌入适配器 + 冻结嵌入方案

**依赖：** Task 26（`VisualEmbedder` 端口）；与 Task 27 无依赖关系，可互换。

**Files:**
- Create: `apps/api/src/sentrix/domain/retrieval/scheme.py`
- Create: `apps/api/src/sentrix/infrastructure/models/__init__.py`
- Create: `apps/api/src/sentrix/infrastructure/models/chinese_clip.py`
- Create: `apps/api/tests/integration/test_chinese_clip.py`
- Create: `apps/api/tests/unit/test_scheme.py`
- Modify: `apps/api/pyproject.toml`
- Modify: `pyproject.toml`（mypy overrides 加 `cn_clip`）
- Modify: `apps/api/src/sentrix/bootstrap/container.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

**S2 的第二条硬约束：不要重新引入可切换性。** 1.0 的 `embeddings/scheme.py` 把方案钉死，理由是 IMAGE/TEXT 嵌入器被 env 切过（clip / chinese_clip / bge），memory_vectors、ANN 索引、FTS 各套并存且互不匹配，检索时好时坏，而且**没有任何一处会报错**。2.0 连"可配置"都不给：`scheme.py` 是常量，`Settings` 里没有对应字段，`test_scheme.py` 用一个禁用名单把这个约束钉住。

`cn_clip` 没有 `py.typed`，mypy 需要在配置里声明——集中一处，而不是在每个 import 上散落 `# type: ignore`。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/integration/test_chinese_clip.py`:

```python
"""真模型（chinese-clip-ViT-L-14，进程内 GPU/CPU）下的视觉嵌入测试。

这些断言分两类，都不依赖外部照片仓库：

- **结构类**：维度、确定性、图文共空间——钉住实现不退化；
- **语义类**：用 PIL 现场画出纯色图，ground truth 由**构造**确定（我用 RGB
  (200,30,30) 画的红图就是红的），断言 chinese-clip 的文本塔能把它排在其它颜色
  前面。这条不是循环论证：期望值来自画图这一侧，被测的是模型。

模型质量本身的验收按设计 §9.1 由使用者负责；这里只保证「链路没坏」。
"""

from __future__ import annotations

import io
import math
from pathlib import Path

import pytest
from PIL import Image

from sentrix.domain.retrieval.scheme import IMAGE_MODEL, VISUAL_DIMENSION
from sentrix.infrastructure.models.chinese_clip import ChineseClipVisualEmbedder

COLOURS = {
    "红色": (200, 30, 30),
    "蓝色": (30, 30, 200),
    "绿色": (30, 180, 60),
}


@pytest.fixture(scope="module")
def embedder() -> ChineseClipVisualEmbedder:
    instance = ChineseClipVisualEmbedder()
    if not instance.available:
        pytest.skip(f"chinese-clip 权重不可用: {instance.load_error}")
    return instance


def colour_image(path: Path, colour: tuple[int, int, int]) -> Path:
    buffer = io.BytesIO()
    Image.new("RGB", (224, 224), colour).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())
    return path


def cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def test_embedder_reports_the_frozen_scheme(embedder: ChineseClipVisualEmbedder):
    assert embedder.model_id == IMAGE_MODEL
    assert embedder.dimension == VISUAL_DIMENSION


def test_image_vector_has_the_declared_dimension(embedder: ChineseClipVisualEmbedder, tmp_path: Path):
    vector = embedder.embed_image(colour_image(tmp_path / "red.jpg", COLOURS["红色"]))

    assert len(vector) == VISUAL_DIMENSION
    assert all(math.isfinite(value) for value in vector)


def test_query_vector_has_the_declared_dimension(embedder: ChineseClipVisualEmbedder):
    vector = embedder.embed_query("桥")

    assert len(vector) == VISUAL_DIMENSION
    assert all(math.isfinite(value) for value in vector)


def test_same_image_embeds_identically(embedder: ChineseClipVisualEmbedder, tmp_path: Path):
    """同一张图两次推理必须逐位相同。

    ``model.eval()`` 缺失时 dropout 会让结果随机——这条是它唯一的防线。
    """
    path = colour_image(tmp_path / "red.jpg", COLOURS["红色"])

    assert embedder.embed_image(path) == embedder.embed_image(path)


def test_different_texts_give_different_vectors(embedder: ChineseClipVisualEmbedder):
    assert embedder.embed_query("猫") != embedder.embed_query("汽车")


def test_blank_text_yields_no_vector(embedder: ChineseClipVisualEmbedder):
    assert embedder.embed_query("   ") == []


def test_missing_file_yields_no_vector(embedder: ChineseClipVisualEmbedder, tmp_path: Path):
    assert embedder.embed_image(tmp_path / "nope.jpg") == []


@pytest.mark.parametrize("word", sorted(COLOURS))
def test_colour_word_ranks_its_own_colour_first(
    embedder: ChineseClipVisualEmbedder, tmp_path: Path, word: str
):
    vectors = {
        name: embedder.embed_image(colour_image(tmp_path / f"{name}.jpg", colour))
        for name, colour in COLOURS.items()
    }

    query = embedder.embed_query(word)
    scores = {name: cosine(query, vector) for name, vector in vectors.items()}
    ranked = sorted(scores, key=lambda name: scores[name], reverse=True)

    assert ranked[0] == word, f"{word} 的排序是 {ranked}，分数 {scores}"
```
`apps/api/tests/unit/test_scheme.py`:

```python
"""嵌入方案被钉死——2.0 不得重新引入「可切换性」。

1.0 的历史：IMAGE/TEXT 嵌入器被 env/.env 切换过（clip / chinese_clip / bge），
memory_vectors、ANN 索引、FTS 各套并存且互不匹配，检索时好时坏。这里用「配置里
根本没有这个开关」来保证同样的事不会再发生——不是靠约定，是靠它无处可配。
"""

from __future__ import annotations

from sentrix.config import Settings
from sentrix.domain.retrieval import scheme


def test_scheme_constants_are_the_ones_in_the_behaviour_baseline():
    assert scheme.IMAGE_EMBEDDER == "chinese_clip"
    assert scheme.IMAGE_MODEL == "chinese-clip-ViT-L-14"
    assert scheme.VISUAL_DIMENSION == 768
    assert scheme.VISUAL_SPACE == "visual"


def test_settings_expose_no_embedder_switch(tmp_path, monkeypatch):
    """配置里没有「选哪套嵌入/哪个向量后端」这一项。

    反向验证：把上面任何一个名字加进 ``Settings``，这条测试就会红——红得对，
    因为那正是 1.0 多套向量并存的入口。

    ``text_embedder_url``（bge-m3 sidecar 的地址）不在禁用名单里：它是 S4 的服务
    地址，不是模型选择；本切片用的是图像一侧，不碰它。
    """
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    forbidden = {
        "vector_backend",
        "image_embedder",
        "visual_embedder",
        "text_embedder",
        "embedding_backend",
        "ann_backend",
        "clip_model",
        "clip_checkpoint",
    }

    assert forbidden & set(Settings.model_fields) == set()
    # 这条只是证明上面那个集合不是「对着空集断言」——字段确实读得到。
    assert "data_dir" in Settings.model_fields
```

模型质量本身的验收按设计 §9.1 由使用者负责；这里的语义断言是可移植的构造型判据：用 PIL 现场画出纯色图，ground truth 来自**画图那一侧**（`(200,30,30)` 就是红的），被测的是模型的文本-图像对齐。实测三个颜色词在这三张图上都排对了自己的颜色（0.279/0.256/0.259 之类），所以断言可以写成"排第一"而不是"分数高于某个魔数"。

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_chinese_clip.py apps/api/tests/unit/test_scheme.py -q -p no:warnings`

Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.domain.retrieval.scheme'
E   ImportError: cannot import name 'scheme' from 'sentrix.domain.retrieval'
ERROR apps/api/tests/integration/test_chinese_clip.py
ERROR apps/api/tests/unit/test_scheme.py
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 依赖与类型检查配置**

`apps/api/pyproject.toml` 的 `dependencies` 追加三项：

```toml
    # 视觉记忆是**进程内**推理（设计 §2.2：模型适配属于 apps/api，进程生命周期
    # 不归 2.0 管的是 8100/8101 那些服务）。chinese-clip 的权重是
    # ~/.cache/clip/clip_cn_vit-l-14.pt，约 1.6GB，随 cn-clip 之外单独下载。
    "torch>=2.5,<3",
    "torchvision>=0.20,<1",
    "cn-clip>=1.6,<2",
```

仓库根 `pyproject.toml` 的 mypy overrides 改成：

```toml
[[tool.mypy.overrides]]
module = ["pillow_heif", "yaml", "cn_clip", "cn_clip.*"]
ignore_missing_imports = true
```

（`torch` 与 `qdrant_client` 自带 `py.typed`，不需要 override——已实测确认。）

- [ ] **Step 4: 写 scheme 与适配器**

`apps/api/src/sentrix/domain/retrieval/scheme.py`:

```python
"""单一嵌入方案（钉死，不读 env）。

对应 1.0 ``backend/embeddings/scheme.py``。历史教训写在那里：IMAGE/TEXT 嵌入器
曾被 env/.env 反复切换（clip / chinese_clip / bge），结果 memory_vectors、ANN 索引、
FTS 各套并存且互不匹配，检索时好时坏，而且**没有任何一处会报错**。

所以 2.0 不提供任何切换开关：写入、查询、重建索引三处都只认这里的常量。要换模型
就改这个文件并重建索引，那是一次有版本、可审查的迁移，不是一次环境变量调整。

本切片只用到 IMAGE 一侧；TEXT 一侧（BAAI/bge-m3，1024 维，HTTP sidecar）留给 S4。
"""

from __future__ import annotations

IMAGE_EMBEDDER = "chinese_clip"
IMAGE_MODEL = "chinese-clip-ViT-L-14"
VISUAL_DIMENSION = 768
# memory_vectors.space 与 Qdrant 的命名向量名都用这个值。
VISUAL_SPACE = "visual"
```
`apps/api/src/sentrix/infrastructure/models/__init__.py`:

```python
"""模型适配器：进程内推理，实现在 domain 的 port。"""
```
`apps/api/src/sentrix/infrastructure/models/chinese_clip.py`:

```python
"""Chinese-CLIP 视觉嵌入（进程内 GPU 推理）。

对应 1.0 ``backend/embeddings/chinese_clip_visual.py``。选型理由记在那里：标准
ViT-B-32 的文本-图像对齐对中文基本是随机的（AUC ~0.51），Chinese-CLIP 的文本
塔是中文 BERT，所以「毛绒睡衣」能和标成「连帽衫」的图对上。模型身份见
``domain/retrieval/scheme.py``——本文件不读任何 env 决定用哪个模型。

1.0 的两个失败模式在这里被改成显式行为：

1. ``embed_image`` 曾经静默 ``return []``，调用方拿到空向量照样 upsert，于是
   numpy ABI 冲突期间 223 张图没有任何视觉向量，直到手工清点才发现。现在失败
   仍然返回空（端口约定），但**必然**留下一条 ``log.exception``。
2. 没有 ``model.eval()`` 时 dropout 会让同一张图两次推理得到不同向量——测试
   ``test_same_image_embeds_identically`` 钉住这一点。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from sentrix.domain.retrieval import scheme

log = logging.getLogger("sentrix.chinese_clip")

DEFAULT_CHECKPOINT = Path.home() / ".cache" / "clip" / "clip_cn_vit-l-14.pt"
MODEL_NAME = "ViT-L-14"


class ChineseClipVisualEmbedder:
    """实现 ``domain.retrieval.ports.VisualEmbedder``。"""

    def __init__(self, checkpoint: Path | None = None, device: str | None = None) -> None:
        self.checkpoint = Path(checkpoint) if checkpoint else DEFAULT_CHECKPOINT
        self._device = device
        self._model: object | None = None
        self._preprocess: object | None = None
        self._error = ""
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    @property
    def model_id(self) -> str:
        return scheme.IMAGE_MODEL

    @property
    def dimension(self) -> int:
        return scheme.VISUAL_DIMENSION

    @property
    def device(self) -> str:
        """没有 CUDA 就用 CPU：2.0 必须能在没有 GPU 的机器上跑起来（只是慢）。"""
        if self._device is None:
            import torch

            self._device = "cuda" if torch.cuda.is_available() else "cpu"
        return self._device

    def _load(self) -> object | None:
        if self._model is not None:
            return self._model
        with self._load_lock:
            if self._model is not None:
                return self._model
            if not self.checkpoint.is_file():
                self._error = f"checkpoint missing: {self.checkpoint}"
                log.error("chinese-clip checkpoint 不存在: %s", self.checkpoint)
                return None
            try:
                from cn_clip.clip import load_from_name

                # cn_clip 把 model_name 解析到 download_root 下，指到权重所在目录
                # 就不会去 HuggingFace 拉一份新的。
                model, preprocess = load_from_name(
                    MODEL_NAME, device=self.device, download_root=str(self.checkpoint.parent)
                )
                model.eval()
                # 存进 self 再返回：cn_clip 没有 py.typed，load_from_name 的返回值是
                # Any，直接 return 会被 mypy --strict 的 no-any-return 拦下。
                self._model = model
                self._preprocess = preprocess
                self._error = ""
                return self._model
            except Exception as error:  # noqa: BLE001 - 加载失败必须留痕，不能静默
                self._error = f"{type(error).__name__}: {error}"
                log.exception("chinese-clip 加载失败: %s", self.checkpoint)
                return None

    @property
    def available(self) -> bool:
        return self._load() is not None

    @property
    def load_error(self) -> str:
        return self._error

    def embed_query(self, text: str) -> list[float]:
        phrase = str(text or "").strip()
        if not phrase:
            return []
        model = self._load()
        if model is None:
            return []
        try:
            import torch
            from cn_clip.clip import tokenize

            with self._inference_lock, torch.no_grad():
                tokens = tokenize([phrase]).to(self.device)
                vector = model.encode_text(tokens)  # type: ignore[attr-defined]
            return [float(value) for value in vector[0].cpu().tolist()]
        except Exception:
            log.exception("chinese-clip embed_query 失败: text=%r", phrase[:80])
            return []

    def embed_image(self, path: Path) -> list[float]:
        if not Path(path).is_file():
            log.error("chinese-clip embed_image 收到不存在的文件: %s", path)
            return []
        model = self._load()
        if model is None or self._preprocess is None:
            return []
        try:
            import torch
            from PIL import Image

            from sentrix.infrastructure.media.image_io import ensure_heif_support

            ensure_heif_support()
            with Image.open(path) as handle:
                image = handle.convert("RGB")
                batch = self._preprocess(image).unsqueeze(0).to(self.device)  # type: ignore[operator]
            with self._inference_lock, torch.no_grad():
                vector = model.encode_image(batch)  # type: ignore[attr-defined]
            return [float(value) for value in vector[0].cpu().tolist()]
        except Exception:
            # 这条日志是 1.0 缺的那一条：没有它，「这张图没有向量」在整条链路上不可见。
            log.exception("chinese-clip embed_image 失败: path=%s", path)
            return []
```

- [ ] **Step 5: 容器装配**

`container.py` 加 `from sentrix.infrastructure.models.chinese_clip import ChineseClipVisualEmbedder`、字段 `visual_embedder: ChineseClipVisualEmbedder`、以及 `build()` 里的 `visual_embedder=ChineseClipVisualEmbedder()`。构造是廉价的——模型在第一次嵌入时才加载。

`apps/api/src/sentrix/bootstrap/container.py`（**替换为**）:

```python
"""The single wiring point.

Nothing else in the codebase constructs an adapter.  Domain and application code
receive ports; only this module knows which concrete class implements them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from sentrix.config import Settings
from sentrix.domain.media.ports import PreviewEncoder
from sentrix.infrastructure.media.image_io import PillowPreviewEncoder
from sentrix.infrastructure.media.metadata import PillowMetadataReader
from sentrix.infrastructure.media.storage import FileSystemMediaStorage
from sentrix.infrastructure.models.chinese_clip import ChineseClipVisualEmbedder
from sentrix.infrastructure.persistence.database import Database
from sentrix.infrastructure.persistence.repositories.assets import (
    SqliteAssetRepository,
    SqliteObservationRepository,
)
from sentrix.infrastructure.persistence.repositories.spaces import SqliteSpaceRepository
from sentrix.infrastructure.persistence.repositories.vectors import SqliteVectorRepository
from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "infrastructure" / "persistence" / "migrations"


@dataclass(frozen=True)
class Container:
    settings: Settings
    database: Database
    spaces: SqliteSpaceRepository
    asset_repository: SqliteAssetRepository
    observation_repository: SqliteObservationRepository
    media_storage: FileSystemMediaStorage
    metadata_reader: PillowMetadataReader
    preview_encoder: PreviewEncoder
    vector_repository: SqliteVectorRepository
    vector_index: QdrantVectorIndex
    visual_embedder: ChineseClipVisualEmbedder

    @classmethod
    def build(cls, settings: Settings) -> Container:
        database = Database(settings.database_path, MIGRATIONS_DIR)
        return cls(
            settings=settings,
            database=database,
            spaces=SqliteSpaceRepository(database),
            asset_repository=SqliteAssetRepository(database),
            observation_repository=SqliteObservationRepository(database),
            media_storage=FileSystemMediaStorage(settings.media_dir),
            metadata_reader=PillowMetadataReader(),
            preview_encoder=PillowPreviewEncoder(),
            vector_repository=SqliteVectorRepository(database),
            vector_index=QdrantVectorIndex(settings.qdrant_dir),
            visual_embedder=ChineseClipVisualEmbedder(),
        )

    def database_healthy(self) -> bool:
        try:
            with self.database.connect() as connection:
                connection.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        except sqlite3.Error:
            return False
        return True
```


- [ ] **Step 6: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/integration/test_chinese_clip.py apps/api/tests/unit/test_scheme.py -p no:warnings`

Expected:

```text
............                                                             [100%]
12 passed
```

- [ ] **Step 7: 更新 module-map.md**

加入三行：

```markdown
| `sentrix/domain/retrieval/scheme.py` | 钉死的嵌入方案常量（模型名/维度/space）；不读 env，2.0 不提供切换开关 | domain | — | `sentrix/infrastructure/models/chinese_clip.py` |
| `sentrix/infrastructure/models/__init__.py` | 包标记 | infrastructure | — | — |
| `sentrix/infrastructure/models/chinese_clip.py` | `ChineseClipVisualEmbedder`：进程内 Chinese-CLIP 视觉嵌入，GPU 优先、可退 CPU | infrastructure | `sentrix/domain/retrieval/__init__.py`、`sentrix/domain/retrieval/scheme.py`、`sentrix/infrastructure/media/image_io.py` | `sentrix/bootstrap/container.py` |
```

并改旧行：`sentrix/domain/retrieval/__init__.py` 的「被谁依赖」加 `sentrix/infrastructure/models/chinese_clip.py`；`sentrix/infrastructure/media/image_io.py` 的「被谁依赖」加 `sentrix/infrastructure/models/chinese_clip.py`；`sentrix/bootstrap/container.py` 的「依赖谁」加 `sentrix/infrastructure/models/chinese_clip.py`。

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 8: 登记能力**

```json
{"capability":"chinese-clip 视觉嵌入","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/embeddings/chinese_clip_visual.py","source_symbol":"ChineseClipVisualEmbedder.embed_image","target_path":"apps/api/src/sentrix/infrastructure/models/chinese_clip.py","verification":"apps/api/tests/integration/test_chinese_clip.py","status":"verified"}
{"capability":"嵌入方案冻结（不读 env）","origin_kind":"adapted","source_commit":"2d2cee7c","source_path":"backend/embeddings/scheme.py","source_symbol":"IMAGE_MODEL","target_path":"apps/api/src/sentrix/domain/retrieval/scheme.py","verification":"apps/api/tests/unit/test_scheme.py","status":"verified"}
```

- [ ] **Step 9: 全量门禁**

Run: `make ci`

Expected:

```text
Required test coverage of 85.0% reached. Total coverage: 97.25%
118 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

> 预告一个会在 Task 29 出现的 mypy 报错：如果 `ports.py` 里的 `VisualEmbedder.model_id` 写成了裸属性，接到真容器时会得到 `error: Argument "embedder" has incompatible type ... Protocol member VisualEmbedder.dimension expected settable variable, got read-only attribute`。Task 26 已经把它写成只读 property，这里不该再遇到。

---

## Task 29: 导入即向量化

**依赖：** Task 26（仓储）、27（索引）、28（嵌入器）三者都已就位。

**Files:**
- Create: `apps/api/src/sentrix/application/retrieval/__init__.py`
- Create: `apps/api/src/sentrix/application/retrieval/index_visual.py`
- Create: `apps/api/tests/unit/test_index_visual.py`
- Modify: `apps/api/src/sentrix/domain/retrieval/ports.py`（加 `VisualIndexOutcome` 与 `AssetVisualIndexer`）
- Modify: `apps/api/src/sentrix/domain/retrieval/__init__.py`
- Modify: `apps/api/src/sentrix/application/media/import_assets.py`
- Modify: `apps/api/src/sentrix/api/routes/media.py`
- Modify: `apps/api/src/sentrix/api/schemas/media.py`
- Modify: `apps/api/tests/conftest.py`
- Modify: `apps/api/tests/contract/test_media_routes.py`
- Modify: `packages/contracts/openapi/sentrix-v1.yaml`（重新导出）
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

时机与 1.0 一致（`pipeline.py` 的 `_commit_fast_image`）：**导入即向量化**，用户在 Web 上导入完就能搜到。

三条来自 1.0 的硬性要求写进了实现：

1. **权威先写、镜像后写**。SQLite 里的 `memory_vectors` 是事实源，Qdrant 只是索引；镜像写失败不该让导入失败，但必须留下可见痕迹；
2. **空向量不写**。1.0 拿到空向量照样 upsert，于是 numpy ABI 冲突期间 223 张图没有任何视觉向量而无人知晓；
3. **只吞 `VectorIndexUnavailable`**。适配器里的代码 bug 必须炸出来，不能被降级逻辑掩盖成"索引暂时不可用"。

去重命中时**仍然调一次 indexer**：索引本身幂等（已有向量直接返回），这条路径的真实作用是补上第一次没写成的向量——重新导入是使用者唯一能想到的补救动作，它必须真的有效。

契约测试要把真嵌入器换成确定性假件（`conftest.py` 的 `FakeVisualEmbedder`）：契约测试验的是 HTTP 映射，不该依赖一台带显卡和 1.6GB 权重的机器；真链路的验收在 Task 33。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/unit/test_index_visual.py`:

```python
"""IndexAssetVisual 的单元测试：全部用假适配器，不碰模型也不碰磁盘。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sentrix.application.retrieval.index_visual import SOURCE_TYPE, IndexAssetVisual
from sentrix.domain.media.models import Asset, AssetStatus, MediaType
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import (
    VectorIndexUnavailable,
    VectorRecord,
    VisualIndexOutcome,
)

NOW = datetime(2026, 9, 22, tzinfo=UTC)


class FakeEmbedder:
    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = [1.0, 0.0, 0.0, 0.0] if vector is None else vector
        self.calls: list[Path] = []

    def embed_image(self, path: Path) -> list[float]:
        self.calls.append(Path(path))
        return list(self.vector)

    def embed_query(self, text: str) -> list[float]:
        return list(self.vector)


class FakeVectors:
    def __init__(self) -> None:
        self.records: list[VectorRecord] = []

    def upsert(self, record: VectorRecord) -> None:
        self.records.append(record)

    def has_vector(self, *, space, source_type, source_id, model_name) -> bool:
        return any(
            item.space == space
            and item.source_type == source_type
            and item.source_id == source_id
            and item.model_name == model_name
            for item in self.records
        )

    def search_exact(self, *, space, vector, limit, scope_id=None, model_name=None):
        return []

    def count(self, *, space: str) -> int:
        return len(self.records)


class FakeIndex:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available
        self.records: list[VectorRecord] = []

    def upsert(self, records):
        if not self.available:
            raise VectorIndexUnavailable("RuntimeError: Storage folder is already accessed")
        self.records.extend(records)
        return len(records)

    def search(self, **kwargs):
        return []

    def count(self, *, space, model_name=None) -> int:
        return len(self.records)

    def status(self):
        raise AssertionError("本用例不走 status()")


class BoomIndex(FakeIndex):
    def upsert(self, records):
        raise ZeroDivisionError("适配器里的真 bug")


def asset(path: str = "/media/asset_1.jpg", media_type: MediaType = MediaType.IMAGE) -> Asset:
    return Asset(
        id="asset_1",
        scope_id="album1",
        file_name="IMG_0001.jpg",
        media_type=media_type,
        path=path,
        mime_type="image/jpeg",
        size_bytes=10,
        status=AssetStatus.PROCESSED,
        content_sha256="abc",
        captured_at=NOW,
        captured_location=None,
        created_at=NOW,
        updated_at=NOW,
    )


def build(*, embedder=None, index=None):
    vectors = FakeVectors()
    use_case = IndexAssetVisual(embedder=embedder or FakeEmbedder(), vectors=vectors, index=index)
    return use_case, vectors


def test_image_is_embedded_and_stored_in_both_stores():
    embedder = FakeEmbedder()
    index = FakeIndex()
    use_case, vectors = build(embedder=embedder, index=index)

    outcome = use_case.index(asset())

    assert outcome.indexed is True
    assert outcome.mirrored is True
    assert embedder.calls == [Path("/media/asset_1.jpg")]
    record = vectors.records[0]
    assert record.source_type == SOURCE_TYPE
    assert record.source_id == "asset_1"
    assert record.space == scheme.VISUAL_SPACE
    assert record.model_name == scheme.IMAGE_MODEL
    assert record.scope_id == "album1"
    assert record.vector == (1.0, 0.0, 0.0, 0.0)
    assert index.records == [record]


def test_non_image_assets_are_not_embedded():
    embedder = FakeEmbedder()
    use_case, vectors = build(embedder=embedder)

    outcome = use_case.index(asset(media_type=MediaType.AUDIO))

    assert outcome == VisualIndexOutcome(indexed=False, reason="not_an_image")
    assert embedder.calls == []
    assert vectors.records == []


def test_empty_embedding_is_not_written_and_is_reported():
    """空向量绝不落库——1.0 的 223 张「没有向量的图」就是这么来的。"""
    use_case, vectors = build(embedder=FakeEmbedder(vector=[]))

    outcome = use_case.index(asset())

    assert outcome.indexed is False
    assert outcome.reason == "embedding_unavailable"
    assert vectors.records == []


def test_dimension_mismatch_is_reported_not_stored():
    use_case, vectors = build(embedder=FakeEmbedder(vector=[1.0, 0.0]))

    outcome = use_case.index(asset())

    assert outcome.indexed is False
    assert outcome.reason == "dimension_mismatch:2!=4"
    assert vectors.records == []


def test_second_index_of_the_same_asset_does_not_re_embed():
    embedder = FakeEmbedder()
    use_case, _ = build(embedder=embedder)
    first = use_case.index(asset())
    second = use_case.index(asset())

    assert first.indexed is True
    assert second == VisualIndexOutcome(indexed=True, mirrored=True, reason="already_indexed")
    assert len(embedder.calls) == 1, "重复导入不该再跑一次推理"


def test_unavailable_index_does_not_fail_the_authoritative_write():
    """镜像挂了可以降级，但不能把权威写入一起拖下水。"""
    use_case, vectors = build(index=FakeIndex(available=False))

    outcome = use_case.index(asset())

    assert outcome.indexed is True
    assert outcome.mirrored is False
    assert "already accessed" in outcome.reason
    assert len(vectors.records) == 1


def test_missing_index_is_reported_as_not_mirrored():
    use_case, vectors = build(index=None)

    outcome = use_case.index(asset())

    assert outcome.indexed is True
    assert outcome.mirrored is False
    assert outcome.reason == "no_index_configured"
    assert len(vectors.records) == 1


def test_unexpected_index_errors_are_not_downgraded():
    """只吞 VectorIndexUnavailable。适配器里的真 bug 必须炸出来。"""
    use_case, _ = build(index=BoomIndex())

    with pytest.raises(ZeroDivisionError):
        use_case.index(asset())
```

`apps/api/tests/contract/test_media_routes.py` 追加两条（其它原样保留）：

```python
# 文件顶部补一行：from sentrix.domain.retrieval.scheme import IMAGE_MODEL


def test_import_reports_and_writes_the_visual_vector(client: TestClient):
    """导入成功必须真的留下视觉向量，并且这件事在响应里看得见。

    1.0 的同类失败（223 张图没有任何视觉向量）在接口上完全没有痕迹，只能事后
    手工清点；``indexed`` 就是补上的那个痕迹。
    """
    body = client.post(
        "/api/v1/media/import",
        files={"file": ("a.jpg", jpeg_bytes(), "image/jpeg")},
        data={"scope_id": "album1"},
    ).json()

    assert body["indexed"] is True

    container = client.app.state.container
    assert (
        container.vector_repository.has_vector(
            space="visual",
            source_type="asset",
            source_id=body["asset_id"],
            model_name=IMAGE_MODEL,
        )
        is True
    )


def test_reimport_does_not_duplicate_the_visual_vector(client: TestClient):
    payload = jpeg_bytes()
    for _ in range(2):
        client.post(
            "/api/v1/media/import",
            files={"file": ("a.jpg", payload, "image/jpeg")},
            data={"scope_id": "album1"},
        )

    assert client.app.state.container.vector_repository.count(space="visual") == 1
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_index_visual.py -q -p no:warnings`
Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.application.retrieval'
ERROR apps/api/tests/unit/test_index_visual.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

契约层的红灯要单独看（把 `application/retrieval/` 留着、只回退导入用例与响应模型）：

Run: `.venv/bin/pytest apps/api/tests/contract/test_media_routes.py -q -p no:warnings`
Expected:

```text
FAILED apps/api/tests/contract/test_media_routes.py::test_import_reports_and_writes_the_visual_vector
FAILED apps/api/tests/contract/test_media_routes.py::test_reimport_does_not_duplicate_the_visual_vector
```

- [ ] **Step 3: 端口加 `AssetVisualIndexer`**

`domain/retrieval/ports.py` 追加 `VisualIndexOutcome` 与 `AssetVisualIndexer`（放在 `VectorIndex` 协议之后），并在顶部加 `from sentrix.domain.media.models import Asset`。`domain/retrieval/__init__.py` 的导入与 `__all__` 各加两项。改完之后这两个文件是：

`apps/api/src/sentrix/domain/retrieval/ports.py`（**替换为**）:

```python
"""检索域端口：权威向量存储、派生向量索引、视觉嵌入器。

三者的分工是 1.0 已经验证过的契约，2.0 原样保留：

- ``VectorRepository`` 是**权威**（SQLite 里的 memory_vectors），写入必须先到它；
- ``VectorIndex`` 是**派生镜像**（Qdrant），可重建、可缺席；
- 索引缺席或锁被占用时，检索必须回退到权威存储，并把「正在降级」这件事报出去。

1.0 的教训是降级被写进了日志但没人看：Qdrant 目录锁被 8091 独占时，第二个进程
只打印一行 ``vector search degrades to SQLite full scans``，接口照常返回空结果。
所以这里的 ``VectorIndexStatus`` 是端口的一部分，降级原因必须能一路传到 HTTP 响应。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sentrix.domain.media.models import Asset


@dataclass(frozen=True, slots=True)
class VectorRecord:
    """一条待写入的向量。``(space, source_type, source_id, model_name)`` 唯一。"""

    scope_id: str
    space: str
    source_type: str
    source_id: str
    model_name: str
    vector: tuple[float, ...]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def dimension(self) -> int:
        return len(self.vector)


@dataclass(frozen=True, slots=True)
class VectorHit:
    source_id: str
    scope_id: str
    source_type: str
    model_name: str
    score: float
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VectorIndexStatus:
    """索引健康度。``degraded`` 为真时必须是可行动的信息，不是一个布尔摆设。"""

    backend: str
    available: bool
    degraded: bool = False
    reason: str = ""


class VectorRepository(Protocol):
    """权威向量存储。SQLite 是唯一实现，也是索引重建的数据来源。"""

    def upsert(self, record: VectorRecord) -> None: ...

    def has_vector(self, *, space: str, source_type: str, source_id: str, model_name: str) -> bool: ...

    def search_exact(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def count(self, *, space: str) -> int: ...


class VectorIndexUnavailable(RuntimeError):
    """派生索引不可用（目录被占用、权限、磁盘满）。

    这是**预期内**的失败，所以必须是一个调用方能按类型捕获的异常，而不是
    「返回 0 / 返回空列表」——后者在调用点看不出区别，1.0 的静默降级就是这么来的。
    """


class VectorIndex(Protocol):
    """派生向量索引。缺席不是错误，但必须被看见。

    ``upsert`` / ``search`` / ``count`` 在索引不可用时抛 ``VectorIndexUnavailable``，
    由 application 层决定是回退到权威存储还是把错误暴露出去——适配器不替它决定。
    """

    def upsert(self, records: Sequence[VectorRecord]) -> int: ...

    def search(
        self,
        *,
        space: str,
        vector: Sequence[float],
        limit: int,
        scope_id: str | None = None,
        model_name: str | None = None,
    ) -> Sequence[VectorHit]: ...

    def count(self, *, space: str, model_name: str | None = None) -> int: ...

    def status(self) -> VectorIndexStatus: ...


@dataclass(frozen=True, slots=True)
class VisualIndexOutcome:
    """一次视觉索引的结果。三个字段让「没索引上」和「为什么」都能一路传到接口。"""

    indexed: bool
    mirrored: bool = False
    reason: str = ""


class AssetVisualIndexer(Protocol):
    """把一张 Asset 变成视觉记忆。导入用例依赖它，不依赖具体的嵌入器/索引。"""

    def index(self, asset: Asset) -> VisualIndexOutcome: ...


class VisualEmbedder(Protocol):
    """跨模态视觉嵌入：同一空间里的文本查询向量与图像向量。

    只管两个方向，不带设备、批次、模型元数据——``model_id`` 与 ``dimension``
    是索引兼容性校验的全部依据。

    这两个成员声明成只读 property 而不是裸属性：适配器的实现是 property，
    mypy 的 ``arg-type`` 不允许一个只读 property 去满足「可写变量」的协议约定。
    """

    @property
    def model_id(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed_image(self, path: Path) -> Sequence[float]: ...

    def embed_query(self, text: str) -> Sequence[float]: ...
```
`apps/api/src/sentrix/domain/retrieval/__init__.py`（**替换为**）:

```python
"""检索域：查询契约、候选融合、向量端口与相似度。纯规则，零 IO。"""

from sentrix.domain.retrieval.fusion import (
    ANCHOR_BOOST,
    DEFAULT_CHANNEL_WEIGHTS,
    RRF_K,
    evidence_class_for,
    fuse,
    weighted_rrf_score,
)
from sentrix.domain.retrieval.models import CandidateHit, FusedCandidate
from sentrix.domain.retrieval.ports import (
    AssetVisualIndexer,
    VectorHit,
    VectorIndex,
    VectorIndexStatus,
    VectorIndexUnavailable,
    VectorRecord,
    VectorRepository,
    VisualEmbedder,
    VisualIndexOutcome,
)
from sentrix.domain.retrieval.similarity import cosine_similarity, dimension_matches

__all__ = [
    "ANCHOR_BOOST",
    "DEFAULT_CHANNEL_WEIGHTS",
    "RRF_K",
    "AssetVisualIndexer",
    "CandidateHit",
    "FusedCandidate",
    "VectorHit",
    "VectorIndex",
    "VectorIndexStatus",
    "VectorIndexUnavailable",
    "VectorRecord",
    "VectorRepository",
    "VisualEmbedder",
    "VisualIndexOutcome",
    "cosine_similarity",
    "dimension_matches",
    "evidence_class_for",
    "fuse",
    "weighted_rrf_score",
]
```


- [ ] **Step 4: 写用例**

`apps/api/src/sentrix/application/retrieval/__init__.py`:

```python
"""检索用例：把导入的资产变成视觉记忆，并按内容词召回。"""
```
`apps/api/src/sentrix/application/retrieval/index_visual.py`:

```python
"""把一张 Asset 变成视觉记忆：嵌入 → 写权威存储 → 镜像到派生索引。

1.0 的时机是「导入即向量化」（``backend/pipeline.py`` 的 ``_commit_fast_image``），
2.0 保持同样时机：用户在 Web 上导入完照片，就应该能立刻搜到它。

两条来自 1.0 的硬性要求：

- **权威先写、镜像后写**。SQLite 里的 memory_vectors 是事实源，Qdrant 只是索引；
  镜像写失败不该让导入失败，但必须留下可见痕迹（返回 ``mirrored=False`` + 原因）。
- **空向量不写**。1.0 拿到空向量照样 upsert，于是「这张图没有任何视觉向量」在
  整条链路上不可见——numpy ABI 冲突期间 223 张图就是这么丢的。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sentrix.domain.media.models import Asset, MediaType
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import (
    AssetVisualIndexer,
    VectorIndex,
    VectorIndexUnavailable,
    VectorRecord,
    VectorRepository,
    VisualEmbedder,
    VisualIndexOutcome,
)

log = logging.getLogger("sentrix.retrieval.index_visual")

SOURCE_TYPE = "asset"


class IndexAssetVisual(AssetVisualIndexer):
    def __init__(
        self,
        *,
        embedder: VisualEmbedder,
        vectors: VectorRepository,
        index: VectorIndex | None = None,
    ) -> None:
        self._embedder = embedder
        self._vectors = vectors
        self._index = index

    def index(self, asset: Asset) -> VisualIndexOutcome:
        if asset.media_type is not MediaType.IMAGE:
            return VisualIndexOutcome(indexed=False, reason="not_an_image")

        if self._vectors.has_vector(
            space=scheme.VISUAL_SPACE,
            source_type=SOURCE_TYPE,
            source_id=asset.id,
            model_name=self._embedder.model_id,
        ):
            # 幂等：重复导入同一张图不该再跑一次推理。
            return VisualIndexOutcome(indexed=True, mirrored=True, reason="already_indexed")

        vector = self._embedder.embed_image(Path(asset.path))
        if not vector:
            # 到这里说明适配器已经 log.exception 过了；这里只负责不让它变成一次
            # 「导入成功但没有向量」的静默成功。
            return VisualIndexOutcome(indexed=False, reason="embedding_unavailable")
        if len(vector) != self._embedder.dimension:
            return VisualIndexOutcome(
                indexed=False,
                reason=f"dimension_mismatch:{len(vector)}!={self._embedder.dimension}",
            )

        record = VectorRecord(
            scope_id=asset.scope_id,
            space=scheme.VISUAL_SPACE,
            source_type=SOURCE_TYPE,
            source_id=asset.id,
            model_name=self._embedder.model_id,
            vector=tuple(float(value) for value in vector),
            metadata={"file_name": asset.file_name},
        )
        self._vectors.upsert(record)
        mirrored, reason = self._mirror(record)
        return VisualIndexOutcome(indexed=True, mirrored=mirrored, reason=reason)

    def _mirror(self, record: VectorRecord) -> tuple[bool, str]:
        """把同一条向量写进派生索引。只吞 ``VectorIndexUnavailable``——那是预期内的失败。

        其它异常一律向上抛：索引适配器里的代码错误不该被降级逻辑掩盖成
        「索引暂时不可用」。
        """
        if self._index is None:
            return False, "no_index_configured"
        try:
            self._index.upsert([record])
        except VectorIndexUnavailable as error:
            log.warning("派生向量索引不可用，权威存储已写入: %s", error)
            return False, str(error)
        return True, ""
```

- [ ] **Step 5: 让导入用例调它**

`application/media/import_assets.py` 的关键改动（完整文件见下）：

- `__init__` 多一个 `visual_indexer: AssetVisualIndexer | None = None`；
- `ImportResult` 多一个 `indexed: bool = False`；
- `execute()` 的两条返回路径都带上 `indexed=self._index_visual(...)`；
- 新增私有方法：

```python
    def _index_visual(self, asset: Asset) -> bool:
        if self._visual_indexer is None:
            return False
        return self._visual_indexer.index(asset).indexed
```

`apps/api/src/sentrix/application/media/import_assets.py`（**替换为**）:

```python
"""导入用例：把一次上传变成 Asset + Observation（+ 视觉向量）。

业务规则（都来自 1.0 的既有行为）：
- 内容 SHA-256 去重，**按 scope 隔离**，不跨相册去重
- 去重命中时不重复落盘
- 拍摄时间与 GPS 来自 EXIF，抽不到就留空，**不阻断导入**

S2 追加一条：**导入即向量化**（1.0 ``pipeline.py`` 的 ``_commit_fast_image`` 同样
时机）。``visual_indexer`` 是可选协作者——不传就退化成纯导入，这样单元测试与不关心
视觉记忆的调用方都不必构造嵌入器。

去重命中时**仍然调一次 indexer**：索引本身是幂等的（已有向量直接返回），所以这条
路径的真实作用是「补上第一次没能写成的向量」。1.0 有过 223 张图没有任何视觉向量
却毫无征兆的记录，重新导入是使用者唯一能想到的补救动作，它必须真的有效。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sentrix.domain.media.models import Asset, AssetStatus, MediaType, Observation
from sentrix.domain.media.ports import (
    AssetRepository,
    MediaStorage,
    MetadataReader,
    ObservationRepository,
)
from sentrix.domain.retrieval.ports import AssetVisualIndexer


@dataclass(frozen=True, slots=True)
class ImportRequest:
    scope_id: str
    file_name: str
    content_type: str | None
    content: bytes


@dataclass(frozen=True, slots=True)
class ImportResult:
    asset_id: str
    observation_id: str | None
    media_type: MediaType
    status: AssetStatus
    deduplicated: bool
    indexed: bool = False


def _new_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ImportAssets:
    def __init__(
        self,
        *,
        assets: AssetRepository,
        observations: ObservationRepository,
        storage: MediaStorage,
        metadata: MetadataReader,
        visual_indexer: AssetVisualIndexer | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._assets = assets
        self._observations = observations
        self._storage = storage
        self._metadata = metadata
        self._visual_indexer = visual_indexer
        self._clock = clock

    def execute(self, request: ImportRequest) -> ImportResult:
        content_sha256 = hashlib.sha256(request.content).hexdigest()
        media_type = MediaType.from_upload(request.content_type, request.file_name)

        existing = self._assets.find_by_hash(request.scope_id, content_sha256)
        if existing is not None:
            return ImportResult(
                asset_id=existing.id,
                observation_id=None,
                media_type=existing.media_type,
                status=existing.status,
                deduplicated=True,
                indexed=self._index_visual(existing),
            )

        asset_id = _new_id("asset")
        stored_path = self._storage.store(asset_id, request.file_name, request.content)

        captured_at, gps, device = (None, None, None)
        if media_type is MediaType.IMAGE:
            captured_at, gps, device = self._metadata.read(stored_path)

        now = self._clock()
        asset = Asset(
            id=asset_id,
            scope_id=request.scope_id,
            file_name=request.file_name,
            media_type=media_type,
            path=str(stored_path),
            mime_type=request.content_type,
            size_bytes=len(request.content),
            status=AssetStatus.PROCESSED,
            content_sha256=content_sha256,
            captured_at=captured_at or now,
            captured_location=gps,
            created_at=now,
            updated_at=now,
        )
        self._assets.add(asset)

        observation = Observation(
            id=_new_id("obs"),
            scope_id=request.scope_id,
            asset_id=asset_id,
            source_type="import",
            captured_at=asset.captured_at,
            confidence=0.0,
            created_at=now,
            updated_at=now,
        )
        self._observations.add(observation)

        return ImportResult(
            asset_id=asset_id,
            observation_id=observation.id,
            media_type=media_type,
            status=asset.status,
            deduplicated=False,
            indexed=self._index_visual(asset),
        )

    def _index_visual(self, asset: Asset) -> bool:
        if self._visual_indexer is None:
            return False
        return self._visual_indexer.index(asset).indexed
```

- [ ] **Step 6: 路由与响应模型**

`apps/api/src/sentrix/api/schemas/media.py`（**替换为**（ImportResponse 加 `indexed`））:

```python
from __future__ import annotations

from pydantic import BaseModel


class ImportResponse(BaseModel):
    asset_id: str
    observation_id: str | None
    media_type: str
    status: str
    deduplicated: bool
    # 有没有真的写成视觉向量。1.0 的「导入成功但没有向量」在接口上完全不可见，
    # 只能在事后手工清点库里少了多少条才发现。
    indexed: bool


class AssetSummary(BaseModel):
    id: str
    file_name: str
    media_type: str
    mime_type: str | None
    size_bytes: int
    status: str
    captured_at: str | None
    gps: dict[str, float] | None


class AssetListResponse(BaseModel):
    items: list[AssetSummary]
```
`apps/api/src/sentrix/api/routes/media.py`（**替换为**（构造 IndexAssetVisual 传给 ImportAssets））:

```python
"""媒体导入与交付。只做 HTTP 映射，业务在 application 层。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from sentrix.api.deps import get_container
from sentrix.api.schemas.media import AssetListResponse, AssetSummary, ImportResponse
from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.application.retrieval.index_visual import IndexAssetVisual
from sentrix.bootstrap.container import Container
from sentrix.domain.media.models import Asset

router = APIRouter(tags=["media"])

PREVIEW_MAX_DIMENSION = 1600
PREVIEW_QUALITY = 85


def _to_summary(asset: Asset) -> AssetSummary:
    return AssetSummary(
        id=asset.id,
        file_name=asset.file_name,
        media_type=asset.media_type.value,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        status=asset.status.value,
        captured_at=asset.captured_at.isoformat() if asset.captured_at else None,
        gps=(
            {"latitude": asset.captured_location.latitude, "longitude": asset.captured_location.longitude}
            if asset.captured_location
            else None
        ),
    )


@router.post("/media/import", status_code=201)
async def import_media(
    file: UploadFile = File(...),
    scope_id: str = Form("home-default"),
    container: Container = Depends(get_container),
) -> ImportResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    use_case = ImportAssets(
        assets=container.asset_repository,
        observations=container.observation_repository,
        storage=container.media_storage,
        metadata=container.metadata_reader,
        visual_indexer=IndexAssetVisual(
            embedder=container.visual_embedder,
            vectors=container.vector_repository,
            index=container.vector_index,
        ),
    )
    result = use_case.execute(
        ImportRequest(
            scope_id=scope_id,
            file_name=file.filename or "unnamed",
            content_type=file.content_type,
            content=content,
        )
    )
    return ImportResponse(
        asset_id=result.asset_id,
        observation_id=result.observation_id,
        media_type=result.media_type.value,
        status=result.status.value,
        deduplicated=result.deduplicated,
        indexed=result.indexed,
    )


@router.get("/assets")
def list_assets(
    scope_id: str = "home-default",
    limit: int = 200,
    container: Container = Depends(get_container),
) -> AssetListResponse:
    assets = container.asset_repository.list_by_scope(scope_id, min(limit, 1000))
    return AssetListResponse(items=[_to_summary(asset) for asset in assets])


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, container: Container = Depends(get_container)) -> AssetSummary:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return _to_summary(asset)


@router.get("/assets/{asset_id}/file")
def get_asset_file(asset_id: str, container: Container = Depends(get_container)) -> FileResponse:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    path = Path(asset.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="media file missing on disk")
    return FileResponse(path, media_type=asset.mime_type or "application/octet-stream")


@router.get("/assets/{asset_id}/preview")
def get_asset_preview(asset_id: str, container: Container = Depends(get_container)) -> Response:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    payload = container.preview_encoder.encode_jpeg(
        Path(asset.path), max_dimension=PREVIEW_MAX_DIMENSION, quality=PREVIEW_QUALITY
    )
    return Response(content=payload, media_type="image/jpeg")
```

- [ ] **Step 7: conftest 换假嵌入器**

`apps/api/tests/conftest.py`（**替换为**（新增 FakeVisualEmbedder，`client` fixture 用它替换容器里的嵌入器））:

```python
import dataclasses
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sentrix.config import Settings
from sentrix.domain.retrieval import scheme
from sentrix.infrastructure.persistence.database import Database
from sentrix.main import create_app


class FakeVisualEmbedder:
    """契约测试用的确定性嵌入器。

    真模型是进程内 GPU 推理、权重 1.6GB：放进 make ci 会让契约测试依赖一台带显卡
    和权重的机器，而契约测试要验的是 HTTP 映射，不是模型。真链路的验收在
    tests/e2e（Task 33），那里用真模型 + 真 Qdrant。
    """

    model_id = scheme.IMAGE_MODEL
    dimension = 8

    @staticmethod
    def _vector(seed: str) -> list[float]:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        return [digest[index] / 255.0 for index in range(8)]

    def embed_image(self, path) -> list[float]:
        return self._vector(Path(path).read_bytes().hex())

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


@pytest.fixture
def settings(tmp_path, monkeypatch) -> Settings:
    monkeypatch.setenv("SENTRIX_DATA_DIR", str(tmp_path))
    return Settings()


@pytest.fixture
def client(settings) -> TestClient:
    """装配好的应用，但把视觉嵌入器换成确定性假件。

    Container 是 frozen dataclass，用 dataclasses.replace 换掉一个适配器，
    其余装配路径（真 SQLite、真迁移、真 Qdrant）保持原样。
    """
    app = create_app(settings)
    app.state.container = dataclasses.replace(app.state.container, visual_embedder=FakeVisualEmbedder())
    return TestClient(app)


@pytest.fixture
def migrations_ready_db(tmp_path) -> Database:
    """真 SQLite + 真迁移文件，跑在临时目录里。"""
    migrations = Path(__file__).resolve().parents[1] / "src/sentrix/infrastructure/persistence/migrations"
    db = Database(tmp_path / "sentrix.db", migrations)
    db.migrate()
    return db
```

- [ ] **Step 8: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_index_visual.py apps/api/tests/contract/test_media_routes.py -p no:warnings`
Expected:

```text
................                                                         [100%]
16 passed
```

- [ ] **Step 9: 重新导出 OpenAPI 契约**

Run: `.venv/bin/python scripts/export_openapi.py`
Expected: `已写入 /home/asus/Github/sentrix-home-web2.0/packages/contracts/openapi/sentrix-v1.yaml`

`ImportResponse` 会多出 `indexed`（并且它是 required）。这是**破坏性变更**：既有的 TS 客户端若按旧 schema 解析必须跟着更新——本切片的前端是自己写的，没有生成客户端，所以没有别的消费方。

- [ ] **Step 10: 更新 module-map.md**

加入两行：

```markdown
| `sentrix/application/retrieval/__init__.py` | 包标记 | application | — | — |
| `sentrix/application/retrieval/index_visual.py` | 导入即向量化：嵌入 → 写权威存储 → 镜像派生索引；空向量不写、重复导入不重嵌 | application | `sentrix/domain/media/models.py`、`sentrix/domain/retrieval/__init__.py`、`sentrix/domain/retrieval/ports.py`、`sentrix/domain/retrieval/scheme.py` | `sentrix/api/routes/media.py` |
```

并改旧行（这些是新文件的反向依赖，逐条都要改）：

```markdown
| `sentrix/domain/retrieval/__init__.py` | … | domain | … | `sentrix/application/retrieval/index_visual.py`、`sentrix/infrastructure/models/chinese_clip.py` |
| `sentrix/domain/retrieval/scheme.py` | … | domain | — | `sentrix/application/retrieval/index_visual.py`、`sentrix/infrastructure/models/chinese_clip.py` |
| `sentrix/domain/media/models.py` | … | domain | — | …、`sentrix/application/retrieval/index_visual.py`、`sentrix/domain/retrieval/ports.py`、… |
| `sentrix/domain/retrieval/ports.py` | … | domain | `sentrix/domain/media/models.py` | `sentrix/application/media/import_assets.py`、`sentrix/application/retrieval/index_visual.py`、… |
| `sentrix/api/routes/media.py` | … | api | …、`sentrix/application/retrieval/index_visual.py`、… | `sentrix/main.py` |
| `sentrix/application/media/import_assets.py` | … | application | `sentrix/domain/media/models.py`、`sentrix/domain/media/ports.py`、`sentrix/domain/retrieval/ports.py` | `sentrix/api/routes/media.py` |
```

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 11: 登记能力**

```json
{"capability":"导入即向量化","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/pipeline.py","source_symbol":"IngestionPipeline._commit_fast_image","target_path":"apps/api/src/sentrix/application/retrieval/index_visual.py","verification":"apps/api/tests/unit/test_index_visual.py","status":"verified"}
```

- [ ] **Step 12: 全量门禁**

Run: `make ci`

Expected:

```text
Required test coverage of 85.0% reached. Total coverage: 97.69%
128 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

---

## Task 30: 视觉召回用例（融合骨架落地）

**依赖：** Task 25（融合）、26（权威存储）、28（嵌入器）。

**Files:**
- Create: `apps/api/src/sentrix/application/retrieval/retrieve_visual.py`
- Create: `apps/api/tests/unit/test_retrieve_visual.py`
- Modify: `apps/api/src/sentrix/domain/retrieval/ports.py`（`VectorRepository.count` 加 `model_name`）
- Modify: `apps/api/src/sentrix/infrastructure/persistence/repositories/vectors.py`
- Modify: `apps/api/tests/integration/test_vector_repository.py`
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

分层职责与 1.0 `backend/retrieval/visual_ann.py` 相同，但有一处**刻意的行为差异**：1.0 先问 Qdrant，为空就继续走静态 HNSW，两条路径都不比对"索引里到底有多少条"。2.0 的规则是**先比对计数，再决定信不信索引**：

| 情况 | 结果 |
|---|---|
| 索引不可用（目录被占用等） | 回退权威存储，`degraded=True`，原因是异常原文 |
| 索引可用但为空、权威非空 | 回退，原因 `index_empty:index=0,authoritative=N` |
| 索引条数少于权威 | 回退，原因 `index_stale:index=N,authoritative=M` |
| 计数对得上 | 用索引，`backend="qdrant"` |

无论走哪条路结果都必须正确，区别只在快慢以及 `degraded` 有没有置起来。

`VectorRepository.count` 因此需要支持 `model_name` 过滤——应用层要拿**同一个口径**去和 Qdrant 的 collection 计数比对，两边只要有一边多带了过滤条件，比对就恒不成立。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/unit/test_retrieve_visual.py`:

```python
"""视觉召回用例的单元测试：假嵌入器 + 假索引 + 假权威存储。

重点是**降级矩阵**：索引被锁、索引为空、索引落后、检索中途掉线，四种情况都必须
①仍然给出正确结果 ②把 degraded 置起来并带上原因。1.0 在这四种情况里全部静默。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from sentrix.application.retrieval.retrieve_visual import (
    CHANNEL,
    RetrieveVisual,
    VisualSearchRequest,
)
from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.ports import (
    VectorHit,
    VectorIndexStatus,
    VectorIndexUnavailable,
)

MODEL = scheme.IMAGE_MODEL


class FakeEmbedder:
    model_id = MODEL
    dimension = 4

    def __init__(self, vector: list[float] | None = None) -> None:
        self.vector = [1.0, 0.0, 0.0, 0.0] if vector is None else vector
        self.queries: list[str] = []

    def embed_image(self, path: Path) -> list[float]:
        return list(self.vector)

    def embed_query(self, text: str) -> list[float]:
        self.queries.append(text)
        return list(self.vector)


class FakeVectors:
    def __init__(self, hits: list[VectorHit] | None = None, total: int | None = None) -> None:
        self.hits = hits or []
        self.total = len(self.hits) if total is None else total
        self.searches: list[dict] = []

    def upsert(self, record):
        raise AssertionError("召回用例不该写向量")

    def has_vector(self, **kwargs) -> bool:
        return False

    def count(self, *, space: str, model_name: str | None = None) -> int:
        return self.total

    def search_exact(self, *, space, vector, limit, scope_id=None, model_name=None):
        self.searches.append({"space": space, "limit": limit, "scope_id": scope_id, "model_name": model_name})
        return self.hits[:limit]


class FakeIndex:
    def __init__(
        self,
        *,
        hits: list[VectorHit] | None = None,
        available: bool = True,
        count: int | None = None,
        reason: str = "RuntimeError: Storage folder is already accessed",
        fail_on_search: bool = False,
    ) -> None:
        self.hits = hits or []
        self.available = available
        self.count_value = len(self.hits) if count is None else count
        self.reason = reason
        self.fail_on_search = fail_on_search
        self.searches: list[dict] = []

    def upsert(self, records):
        raise AssertionError("召回用例不该写索引")

    def status(self) -> VectorIndexStatus:
        return VectorIndexStatus(
            backend="qdrant",
            available=self.available,
            degraded=not self.available,
            reason="" if self.available else self.reason,
        )

    def count(self, *, space, model_name=None) -> int:
        if not self.available:
            raise VectorIndexUnavailable(self.reason)
        return self.count_value

    def search(self, *, space, vector, limit, scope_id=None, model_name=None):
        if self.fail_on_search:
            raise VectorIndexUnavailable(self.reason)
        self.searches.append({"space": space, "limit": limit, "scope_id": scope_id, "model_name": model_name})
        return self.hits[:limit]


def hit(source_id: str, score: float) -> VectorHit:
    return VectorHit(
        source_id=source_id,
        scope_id="album1",
        source_type="asset",
        model_name=MODEL,
        score=score,
    )


def build(*, embedder=None, vectors=None, index=None) -> RetrieveVisual:
    return RetrieveVisual(
        embedder=embedder or FakeEmbedder(),
        vectors=vectors or FakeVectors(),
        index=index,
    )


def request(**kwargs) -> VisualSearchRequest:
    params = {"text": "桥", "scope_id": "album1", "limit": 5}
    params.update(kwargs)
    return VisualSearchRequest(**params)  # type: ignore[arg-type]


def test_healthy_index_serves_the_query():
    index = FakeIndex(hits=[hit("asset_a", 0.31), hit("asset_b", 0.28)], count=2)
    vectors = FakeVectors(total=2)

    result = build(vectors=vectors, index=index).execute(request())

    assert result.backend == "qdrant"
    assert result.degraded is False
    assert result.degraded_reason == ""
    assert [item.asset_id for item in result.hits] == ["asset_a", "asset_b"]
    assert result.authoritative_vectors == 2
    assert vectors.searches == [], "索引健康时不该回退到精确检索"


def test_fused_score_uses_the_visual_channel_weight():
    """融合分数必须是加权 RRF，不是原始余弦。

    visual 权重 2.5、RRF_K 60：第一名 = 2.5/61。若哪天有人把权重改回平权，
    这里会红。
    """
    index = FakeIndex(hits=[hit("asset_a", 0.31)], count=1)

    result = build(vectors=FakeVectors(total=1), index=index).execute(request())

    assert result.hits[0].score == pytest.approx(2.5 / 61)
    assert result.hits[0].raw_scores == {CHANNEL: 0.31}
    assert result.hits[0].channels == {CHANNEL: 1}


def test_hits_are_ranked_by_cosine_order_from_the_index():
    index = FakeIndex(hits=[hit("asset_1st", 0.9), hit("asset_2nd", 0.5)], count=2)

    result = build(vectors=FakeVectors(total=2), index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_1st", "asset_2nd"]
    assert result.hits[0].score > result.hits[1].score


def test_limit_is_applied_and_oversampled_upstream():
    index = FakeIndex(hits=[hit(f"asset_{i}", 1.0 - i / 10) for i in range(10)], count=10)

    result = build(vectors=FakeVectors(total=10), index=index).execute(request(limit=3))

    assert len(result.hits) == 3
    assert index.searches[0]["limit"] == 12, "索引侧应当超取 limit*4 再融合"


def test_locked_index_falls_back_to_exact_search_and_says_so():
    """S2 的硬约束：Qdrant 目录被占用时必须回退，而且降级要可见。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)
    index = FakeIndex(available=False)

    result = build(vectors=vectors, index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_a"]
    assert result.backend == "sqlite_exact"
    assert result.degraded is True
    assert "already accessed" in result.degraded_reason
    assert vectors.searches[0]["model_name"] == MODEL
    assert vectors.searches[0]["scope_id"] == "album1"


def test_empty_index_while_the_authoritative_store_has_vectors_is_degraded():
    """索引被删过 / 从没建过：不能因为索引里没有就返回空。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=7)
    index = FakeIndex(hits=[], count=0)

    result = build(vectors=vectors, index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_a"]
    assert result.degraded is True
    assert result.degraded_reason == "index_empty:index=0,authoritative=7"


def test_index_behind_the_authoritative_store_is_degraded():
    """1.0 的 ``data/ann/visual.hnsw`` 只有 1,170 条而真实 10,590 条，就是这么被
    发现的——**没有人**比对过这两个数。这里把它变成一条规则。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=10)
    index = FakeIndex(hits=[hit("asset_a", 0.31)], count=4)

    result = build(vectors=vectors, index=index).execute(request())

    assert result.backend == "sqlite_exact"
    assert result.degraded_reason == "index_stale:index=4,authoritative=10"
    assert index.searches == [], "已经判定落后就不该再去查它"


def test_index_dropping_between_status_and_search_still_returns_results():
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)
    index = FakeIndex(hits=[hit("asset_a", 0.31)], count=1, fail_on_search=True)

    result = build(vectors=vectors, index=index).execute(request())

    assert [item.asset_id for item in result.hits] == ["asset_a"]
    assert result.backend == "sqlite_exact"
    assert result.degraded is True
    assert "already accessed" in result.degraded_reason


def test_no_index_configured_uses_the_authoritative_store():
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)

    result = build(vectors=vectors, index=None).execute(request())

    assert result.backend == "sqlite_exact"
    assert result.degraded is True
    assert result.degraded_reason == "no_index_configured"


def test_blank_query_short_circuits_without_embedding():
    embedder = FakeEmbedder()
    index = FakeIndex(count=0)

    result = build(embedder=embedder, vectors=FakeVectors(total=0), index=index).execute(request(text="   "))

    assert result.hits == ()
    assert result.backend == "none"
    assert result.degraded is False
    assert embedder.queries == []


def test_unavailable_embedder_is_reported_not_silently_empty():
    index = FakeIndex(count=0)

    result = build(embedder=FakeEmbedder(vector=[]), vectors=FakeVectors(total=3), index=index).execute(
        request()
    )

    assert result.hits == ()
    assert result.degraded is True
    assert result.degraded_reason == "embedder_unavailable"
    assert result.authoritative_vectors == 0


def test_query_dimension_mismatch_is_reported():
    result = build(embedder=FakeEmbedder(vector=[1.0, 0.0]), vectors=FakeVectors(total=1)).execute(request())

    assert result.degraded_reason == "query_dimension_mismatch:2!=4"


def test_no_matches_is_not_a_degradation():
    """索引健康、就是没搜到——这不是降级，不能报成故障。"""
    index = FakeIndex(hits=[], count=3)

    result = build(vectors=FakeVectors(total=3), index=index).execute(request())

    assert result.hits == ()
    assert result.backend == "qdrant"
    assert result.degraded is False


def test_exact_fallback_also_filters_by_model():
    """回退路径必须带模型名，否则会把别的模型的向量算进来。"""
    vectors = FakeVectors(hits=[hit("asset_a", 0.31)], total=1)

    build(vectors=vectors, index=None).execute(request())

    assert vectors.searches[0]["model_name"] == MODEL
    assert vectors.searches[0]["space"] == scheme.VISUAL_SPACE


def test_hits_are_capped_at_limit_even_when_the_fallback_oversamples():
    vectors = FakeVectors(hits=[hit(f"asset_{i}", 1.0 - i / 10) for i in range(10)], total=10)

    result = build(vectors=vectors, index=None).execute(request(limit=2))

    assert len(result.hits) == 2


def test_request_is_immutable():
    base = request()

    assert replace(base, limit=1).limit == 1
```

并在 `apps/api/tests/integration/test_vector_repository.py` 末尾追加：

```python
def test_count_filters_by_model_name(repository: SqliteVectorRepository):
    """应用层要拿这个口径去和 Qdrant 的 collection 计数比对，必须能按模型过滤。"""
    repository.upsert(record("asset_new", (1.0, 0.0, 0.0), model_name=MODEL))
    repository.upsert(record("asset_old", (1.0, 0.0, 0.0), model_name="ViT-B-32"))

    assert repository.count(space="visual") == 2
    assert repository.count(space="visual", model_name=MODEL) == 1
    assert repository.count(space="visual", model_name="不存在") == 0
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_retrieve_visual.py -q -p no:warnings`
Expected:

```text
E   ModuleNotFoundError: No module named 'sentrix.application.retrieval.retrieve_visual'
ERROR apps/api/tests/unit/test_retrieve_visual.py
!!!!!!!!!!!!!!!!!!!! Interrupted: 1 error during collection !!!!!!!!!!!!!!!!!!!!
```

- [ ] **Step 3: 写用例**

`apps/api/src/sentrix/application/retrieval/retrieve_visual.py`:

```python
"""视觉召回：内容词 → 视觉通道候选 → 融合排名。

这是 S2 的用户可感知部分：在 Web 上打一个内容词，拿回正确的照片。

分层职责与 1.0 ``backend/retrieval/visual_ann.py`` 相同，但有一处**刻意的行为
差异**：1.0 先问 Qdrant，为空就继续走静态 HNSW，两条路径都不比对「索引里到底有多少
条」。生产上 ``data/ann/visual.hnsw`` 只有 1,170 条而真实 10,590 条，只因为 Qdrant
优先才没暴露——索引落后于权威这件事本身从不可见。

2.0 的规则是**先比对计数，再决定信不信索引**：

- 索引不可用（目录被占用等）→ 回退权威存储，``degraded=True`` + 原因；
- 索引可用但条数少于权威 → 同上，原因是 ``index_stale`` / ``index_empty``；
- 计数对得上 → 用索引，``backend="qdrant"``。

无论走哪条路，结果都必须正确；区别只在快慢，以及 ``degraded`` 字段有没有置起来。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from sentrix.domain.retrieval import scheme
from sentrix.domain.retrieval.fusion import fuse
from sentrix.domain.retrieval.models import CandidateHit
from sentrix.domain.retrieval.ports import (
    VectorHit,
    VectorIndex,
    VectorIndexStatus,
    VectorIndexUnavailable,
    VectorRepository,
    VisualEmbedder,
)

log = logging.getLogger("sentrix.retrieval.visual")

CHANNEL = "visual_ann"
# 索引是近似的、还带 scope 过滤：多取一些再让融合层裁，比在索引层精确裁更安全。
OVERSAMPLE_FACTOR = 4


@dataclass(frozen=True, slots=True)
class VisualSearchRequest:
    text: str
    scope_id: str = "home-default"
    limit: int = 20


@dataclass(frozen=True, slots=True)
class VisualSearchHit:
    asset_id: str
    score: float
    channels: Mapping[str, int] = field(default_factory=dict)
    raw_scores: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VisualSearchResult:
    hits: Sequence[VisualSearchHit] = field(default_factory=tuple)
    backend: str = "none"
    degraded: bool = False
    degraded_reason: str = ""
    # 权威存储里这个 space 有多少条向量。空结果时用来区分「没导入过」与「搜不到」。
    authoritative_vectors: int = 0


class RetrieveVisual:
    def __init__(
        self,
        *,
        embedder: VisualEmbedder,
        vectors: VectorRepository,
        index: VectorIndex | None = None,
    ) -> None:
        self._embedder = embedder
        self._vectors = vectors
        self._index = index

    def execute(self, request: VisualSearchRequest) -> VisualSearchResult:
        text = request.text.strip()
        if not text:
            return VisualSearchResult(hits=(), backend="none")

        query = self._embedder.embed_query(text)
        if not query:
            return VisualSearchResult(
                hits=(),
                backend="none",
                degraded=True,
                degraded_reason="embedder_unavailable",
            )
        if len(query) != self._embedder.dimension:
            return VisualSearchResult(
                hits=(),
                backend="none",
                degraded=True,
                degraded_reason=(f"query_dimension_mismatch:{len(query)}!={self._embedder.dimension}"),
            )

        limit = max(1, request.limit)
        candidates, backend, degraded, reason = self._recall(request, query, limit)
        authoritative = self._vectors.count(space=scheme.VISUAL_SPACE, model_name=self._embedder.model_id)

        hits = [
            CandidateHit(
                asset_id=hit.source_id,
                channel=CHANNEL,
                rank=rank,
                raw_score=hit.score,
            )
            for rank, hit in enumerate(candidates, start=1)
        ]
        fused = fuse({CHANNEL: hits}) if hits else []

        return VisualSearchResult(
            hits=tuple(
                VisualSearchHit(
                    asset_id=candidate.asset_id,
                    score=candidate.score,
                    channels=dict(candidate.channels),
                    raw_scores=dict(candidate.raw_scores),
                )
                for candidate in fused[:limit]
            ),
            backend=backend,
            degraded=degraded,
            degraded_reason=reason,
            authoritative_vectors=authoritative,
        )

    # ---- 召回 ------------------------------------------------------------

    def _recall(
        self, request: VisualSearchRequest, query: Sequence[float], limit: int
    ) -> tuple[Sequence[VectorHit], str, bool, str]:
        """返回 ``(候选, 实际后端, 是否降级, 原因)``。"""
        fetch = max(limit * OVERSAMPLE_FACTOR, limit)
        status = self._index_status()
        reason = "" if status.available else status.reason

        if status.available and self._index is not None:
            reason = self._staleness_reason()
            if not reason:
                try:
                    hits = self._index.search(
                        space=scheme.VISUAL_SPACE,
                        vector=query,
                        limit=fetch,
                        scope_id=request.scope_id,
                        model_name=self._embedder.model_id,
                    )
                except VectorIndexUnavailable as error:
                    # status()/count() 与 search() 之间索引掉了（例如另一个进程
                    # 刚好在这时拿走目录锁）。这一格必须回退，不能把异常抛给用户。
                    log.warning("派生索引在检索中途不可用，回退权威存储: %s", error)
                    reason = str(error)
                else:
                    return hits, "qdrant", False, ""

        hits = self._vectors.search_exact(
            space=scheme.VISUAL_SPACE,
            vector=query,
            limit=fetch,
            scope_id=request.scope_id,
            model_name=self._embedder.model_id,
        )
        return hits, "sqlite_exact", True, reason or "index_unavailable"

    def _index_status(self) -> VectorIndexStatus:
        if self._index is None:
            return VectorIndexStatus(
                backend="none", available=False, degraded=True, reason="no_index_configured"
            )
        return self._index.status()

    def _staleness_reason(self) -> str:
        """索引条数与权威条数对不上时给出原因；对得上返回空串。"""
        assert self._index is not None
        try:
            indexed = self._index.count(space=scheme.VISUAL_SPACE, model_name=self._embedder.model_id)
        except VectorIndexUnavailable as error:
            return str(error)
        authoritative = self._vectors.count(space=scheme.VISUAL_SPACE, model_name=self._embedder.model_id)
        if indexed == 0 and authoritative > 0:
            return f"index_empty:index=0,authoritative={authoritative}"
        if indexed < authoritative:
            return f"index_stale:index={indexed},authoritative={authoritative}"
        return ""
```

- [ ] **Step 4: 给 count 加模型过滤**

`domain/retrieval/ports.py` 的协议改成 `def count(self, *, space: str, model_name: str | None = None) -> int: ...`；`vectors.py` 的实现改成：

```python
    def count(self, *, space: str, model_name: str | None = None) -> int:
        """按 (space, 可选 model) 计数。

        模型维度必须可选且**默认不过滤**：应用层要用同一个口径去和 Qdrant 的
        collection 计数比对，两边只要有一边多带了过滤条件，比对就恒不成立。
        """
        sql = "SELECT COUNT(*) FROM memory_vectors WHERE space = ?"
        params: list[Any] = [space]
        if model_name:
            sql += " AND model_name = ?"
            params.append(model_name)
        with self._database.connect() as connection:
            row = connection.execute(sql, tuple(params)).fetchone()
        return int(row[0]) if row else 0
```

- [ ] **Step 5: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/unit/test_retrieve_visual.py apps/api/tests/integration/test_vector_repository.py -p no:warnings`
Expected:

```text
............................                                             [100%]
28 passed
```

- [ ] **Step 6: 更新 module-map.md**

在 `sentrix/application/retrieval/index_visual.py` 那行之后加：

```markdown
| `sentrix/application/retrieval/retrieve_visual.py` | 视觉召回用例：内容词 → 视觉通道候选 → 加权 RRF；索引被占用/为空/落后时回退权威存储并置 `degraded` | application | `sentrix/domain/retrieval/__init__.py`、`sentrix/domain/retrieval/fusion.py`、`sentrix/domain/retrieval/models.py`、`sentrix/domain/retrieval/ports.py`、`sentrix/domain/retrieval/scheme.py` | — |
```

（此刻它还没有消费者，Task 31 会把 `sentrix/api/routes/search.py` 填进「被谁依赖」。）并改这些旧行的反向依赖：`domain/retrieval/models.py`、`fusion.py`、`scheme.py`、`__init__.py` 的「被谁依赖」各加 `sentrix/application/retrieval/retrieve_visual.py`。

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 7: 登记能力**

```json
{"capability":"视觉 ANN 召回","origin_kind":"migrated","source_commit":"2d2cee7c","source_path":"backend/retrieval/visual_ann.py","source_symbol":"VisualAnnRetriever._retrieve_qdrant","target_path":"apps/api/src/sentrix/application/retrieval/retrieve_visual.py","verification":"apps/api/tests/unit/test_retrieve_visual.py","status":"verified"}
```

- [ ] **Step 8: 全量门禁**

Run: `make ci`

Expected:

```text
Required test coverage of 85.0% reached. Total coverage: 97.65%
145 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

---

## Task 31: `GET /api/v1/search`

**依赖：** Task 30（召回用例）。

**Files:**
- Create: `apps/api/src/sentrix/api/schemas/search.py`
- Create: `apps/api/src/sentrix/api/routes/search.py`
- Create: `apps/api/tests/contract/test_search_routes.py`
- Modify: `apps/api/src/sentrix/api/routes/media.py`（`_to_summary` → `to_summary`）
- Modify: `apps/api/src/sentrix/main.py`
- Modify: `packages/contracts/openapi/sentrix-v1.yaml`（重新导出）
- Modify: `docs/architecture/module-map.md`
- Modify: `docs/refactor/capability-map.jsonl`

**一条有意为之的边界**：S2 **没有**相关性阈值。一个毫不相干的词照样返回排好序的候选列表。理由：1.0 的校准层（`NeutralProbe`、per-space 阈值）属于设计里的 S6，在它到位之前，凭一个拍脑袋的阈值假装判断了相关性只会误伤真命中。契约测试把这一点显式钉住（`test_unrelated_word_gives_a_stable_ranking_without_a_relevance_claim`），免得到时候有人把它当成 bug 顺手"修"掉。

- [ ] **Step 1: 先写失败测试**

`apps/api/tests/contract/test_search_routes.py`:

```python
"""`GET /api/v1/search` 的契约测试。

嵌入器换成脚本化的假件（向量由测试直接指定），这样「哪张图该排第一」是构造出来的，
而不是碰运气撞上模型输出。真模型 + 真照片的验收在 tests/e2e（Task 33）。
"""

from __future__ import annotations

import dataclasses
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sentrix.config import Settings
from sentrix.domain.retrieval import scheme
from sentrix.main import create_app

RED = [1.0, 0.0, 0.0, 0.0]
GREEN = [0.0, 1.0, 0.0, 0.0]


class ScriptedEmbedder:
    """文本 -> 指定向量；图片 -> 由内容映射到指定向量。"""

    model_id = scheme.IMAGE_MODEL
    dimension = 4

    def __init__(self, images: dict[bytes, list[float]], texts: dict[str, list[float]]) -> None:
        self._images = images
        self._texts = texts

    def embed_image(self, path: Path) -> list[float]:
        return list(self._images.get(Path(path).read_bytes(), [0.0, 0.0, 0.0, 1.0]))

    def embed_query(self, text: str) -> list[float]:
        return list(self._texts.get(text.strip(), [0.0, 0.0, 0.0, 1.0]))


def jpeg_bytes(color: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture
def scripted_client(settings: Settings) -> TestClient:
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    app = create_app(settings)
    app.state.container = dataclasses.replace(
        app.state.container,
        visual_embedder=ScriptedEmbedder(images={red: RED, green: GREEN}, texts={"红色": RED, "绿色": GREEN}),
    )
    return TestClient(app)


def import_photo(client: TestClient, payload: bytes, scope_id: str = "album1") -> str:
    return client.post(
        "/api/v1/media/import",
        files={"file": ("a.jpg", payload, "image/jpeg")},
        data={"scope_id": scope_id},
    ).json()["asset_id"]


def test_search_ranks_the_matching_photo_first(scripted_client: TestClient):
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    red_id = import_photo(scripted_client, red)
    green_id = import_photo(scripted_client, green)

    response = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"})

    assert response.status_code == 200
    body = response.json()
    # 召回不做阈值截断：低相似度的候选仍然在列表里，只是排在后面。要的是名次，
    # 不是「有没有命中」——S2 没有校准层，凭一个阈值判断相关性只会误伤。
    assert [item["asset"]["id"] for item in body["items"]] == [red_id, green_id]
    assert body["backend"] == "qdrant"
    assert body["degraded"] is False
    assert body["authoritative_vectors"] == 2


def test_the_other_word_ranks_the_other_photo_first(scripted_client: TestClient):
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    import_photo(scripted_client, red)
    green_id = import_photo(scripted_client, green)

    items = scripted_client.get("/api/v1/search", params={"q": "绿色", "scope_id": "album1"}).json()["items"]

    assert items[0]["asset"]["id"] == green_id


def test_search_hit_carries_everything_the_web_needs(scripted_client: TestClient):
    """命中必须带回可打开的原图信息——前端不该再发一次请求才知道图在哪。"""
    import_photo(scripted_client, jpeg_bytes("red"))

    item = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"}).json()["items"][
        0
    ]

    assert item["asset"]["file_name"] == "a.jpg"
    assert item["asset"]["mime_type"] == "image/jpeg"
    assert item["asset"]["status"] == "processed"
    assert item["channels"] == {"visual_ann": 1}
    assert item["raw_scores"]["visual_ann"] == pytest.approx(1.0)
    # 融合分数是加权 RRF（visual 2.5 / k=60），不是原始余弦。
    assert item["score"] == pytest.approx(2.5 / 61)


def test_search_is_scoped(scripted_client: TestClient):
    red = jpeg_bytes("red")
    album1_id = import_photo(scripted_client, red, scope_id="album1")
    import_photo(scripted_client, red, scope_id="album2")

    items = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"}).json()["items"]

    assert [item["asset"]["id"] for item in items] == [album1_id]


def test_search_on_an_empty_library_is_empty_and_not_degraded(scripted_client: TestClient):
    body = scripted_client.get("/api/v1/search", params={"q": "红色"}).json()

    assert body["items"] == []
    assert body["degraded"] is False
    assert body["authoritative_vectors"] == 0


def test_unrelated_word_gives_a_stable_ranking_without_a_relevance_claim(
    scripted_client: TestClient,
):
    """S2 **没有**相关性阈值：一个毫不相干的词照样返回排好序的候选。

    这条是已知边界，不是缺陷：1.0 的校准层（NeutralProbe / probe per-space 阈值）
    属于 S6。在它到位之前，宁可给出「按相似度排好的列表」，也不要凭一个拍脑袋的
    阈值假装判断了相关性。这里只钉住两件事：不报降级、两次结果一致。
    """
    import_photo(scripted_client, jpeg_bytes("red"))

    first = scripted_client.get("/api/v1/search", params={"q": "完全不相关的词"}).json()
    second = scripted_client.get("/api/v1/search", params={"q": "完全不相关的词"}).json()

    assert first["backend"] == "qdrant"
    assert first["degraded"] is False
    assert [item["asset"]["id"] for item in first["items"]] == [
        item["asset"]["id"] for item in second["items"]
    ]


def test_scores_are_non_increasing(scripted_client: TestClient):
    red, green = jpeg_bytes("red"), jpeg_bytes("green")
    import_photo(scripted_client, red)
    import_photo(scripted_client, green)

    items = scripted_client.get("/api/v1/search", params={"q": "红色", "scope_id": "album1"}).json()["items"]

    scores = [item["score"] for item in items]
    assert scores == sorted(scores, reverse=True)


def test_blank_query_is_rejected(scripted_client: TestClient):
    assert scripted_client.get("/api/v1/search", params={"q": ""}).status_code == 422
    assert scripted_client.get("/api/v1/search").status_code == 422


def test_limit_is_bounded(scripted_client: TestClient):
    assert scripted_client.get("/api/v1/search", params={"q": "红色", "limit": 0}).status_code == 422
    assert scripted_client.get("/api/v1/search", params={"q": "红色", "limit": 1000}).status_code == 422
```

- [ ] **Step 2: 运行测试，确认红灯**

Run: `.venv/bin/pytest apps/api/tests/contract/test_search_routes.py -q -p no:warnings`
Expected:

```text
E       assert 404 == 200
E        +  where 404 = <Response [404 Not Found]>.status_code
E       KeyError: 'items'
FAILED apps/api/tests/contract/test_search_routes.py::test_scores_are_non_increasing
FAILED apps/api/tests/contract/test_search_routes.py::test_blank_query_is_rejected
FAILED apps/api/tests/contract/test_search_routes.py::test_limit_is_bounded
```

- [ ] **Step 3: 写 schema 与路由**

`apps/api/src/sentrix/api/schemas/search.py`:

```python
from __future__ import annotations

from pydantic import BaseModel

from sentrix.api.schemas.media import AssetSummary


class SearchHit(BaseModel):
    """一条命中。``asset`` 是可打开的原图本身——产品硬约束：查询必须返回原图。"""

    asset: AssetSummary
    score: float
    # 通道 -> 该通道里的名次。S2 只有 visual_ann 一条，结构先按六路留好。
    channels: dict[str, int]
    raw_scores: dict[str, float]


class SearchResponse(BaseModel):
    query: str
    # 实际服务的后端：qdrant 或 sqlite_exact。
    backend: str
    degraded: bool
    degraded_reason: str
    # 权威存储里的向量条数。用来区分「还没导入过」和「搜不到」。
    authoritative_vectors: int
    items: list[SearchHit]
```
`apps/api/src/sentrix/api/routes/search.py`:

```python
"""内容词检索。只做 HTTP 映射，召回与融合在 application 层。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query

from sentrix.api.deps import get_container
from sentrix.api.routes.media import to_summary
from sentrix.api.schemas.search import SearchHit, SearchResponse
from sentrix.application.retrieval.retrieve_visual import RetrieveVisual, VisualSearchRequest
from sentrix.bootstrap.container import Container

log = logging.getLogger("sentrix.api.search")

router = APIRouter(tags=["search"])


@router.get("/search")
def search_assets(
    q: str = Query(..., min_length=1, max_length=200, description="内容词，例如 桥 / 海边 / 红色"),
    scope_id: str = "home-default",
    limit: int = Query(20, ge=1, le=100),
    container: Container = Depends(get_container),
) -> SearchResponse:
    use_case = RetrieveVisual(
        embedder=container.visual_embedder,
        vectors=container.vector_repository,
        index=container.vector_index,
    )
    result = use_case.execute(VisualSearchRequest(text=q, scope_id=scope_id, limit=limit))

    items: list[SearchHit] = []
    for hit in result.hits:
        asset = container.asset_repository.get(hit.asset_id)
        if asset is None:
            # 向量还在、资产没了（scope 被删等）。产品硬约束第 5 条要求查询返回
            # **可打开的**原图，所以只能丢弃——但丢弃这件事必须留痕。
            log.warning("检索命中了一个已不存在的资产，已丢弃: %s", hit.asset_id)
            continue
        items.append(
            SearchHit(
                asset=to_summary(asset),
                score=hit.score,
                channels=dict(hit.channels),
                raw_scores=dict(hit.raw_scores),
            )
        )

    return SearchResponse(
        query=q,
        backend=result.backend,
        degraded=result.degraded,
        degraded_reason=result.degraded_reason,
        authoritative_vectors=result.authoritative_vectors,
        items=items,
    )
```

- [ ] **Step 4: 注册路由，并把 `media.py` 的 `_to_summary` 改名**

`search.py` 要复用媒体端点的资产摘要映射，所以把 `api/routes/media.py` 里的 `_to_summary` 改名为 `to_summary`（去掉前导下划线），再把 `main.py` 改成：

`apps/api/src/sentrix/main.py`（**替换为**（只多了 search 路由的 import 与注册））:

```python
"""FastAPI assembly. Route modules contain transport mapping only."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from sentrix import __version__
from sentrix.api.errors import DomainError, domain_error_handler
from sentrix.api.routes import health, media, search
from sentrix.bootstrap.container import Container
from sentrix.config import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.ensure_directories()

    container = Container.build(settings)
    container.database.migrate()

    app = FastAPI(title="Sentrix Home API", version=__version__, docs_url=None, redoc_url=None)
    app.state.container = container
    app.add_exception_handler(DomainError, domain_error_handler)

    v1 = APIRouter(prefix="/api/v1")
    v1.include_router(health.router)
    v1.include_router(media.router)
    v1.include_router(search.router)
    app.include_router(v1)
    return app


# 这里刻意没有模块级 ``app = create_app()``：它会让任何 ``import sentrix.main``
# （pytest 收集、脚本、类型检查）都带上副作用——在进程 CWD 下建出 data/、跑一次迁移。
# 进程入口走工厂：``uvicorn --factory sentrix.main:create_app``（见 Makefile 的 dev）。
# 理由与 Task 5 的定稿一致；本块曾残留该行（1076a7f 只改了 Task 5，漏改这里）。
```
`apps/api/src/sentrix/api/routes/media.py`（**替换为**（`_to_summary` → `to_summary`））:

```python
"""媒体导入与交付。只做 HTTP 映射，业务在 application 层。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response

from sentrix.api.deps import get_container
from sentrix.api.schemas.media import AssetListResponse, AssetSummary, ImportResponse
from sentrix.application.media.import_assets import ImportAssets, ImportRequest
from sentrix.application.retrieval.index_visual import IndexAssetVisual
from sentrix.bootstrap.container import Container
from sentrix.domain.media.models import Asset

router = APIRouter(tags=["media"])

PREVIEW_MAX_DIMENSION = 1600
PREVIEW_QUALITY = 85


def to_summary(asset: Asset) -> AssetSummary:
    return AssetSummary(
        id=asset.id,
        file_name=asset.file_name,
        media_type=asset.media_type.value,
        mime_type=asset.mime_type,
        size_bytes=asset.size_bytes,
        status=asset.status.value,
        captured_at=asset.captured_at.isoformat() if asset.captured_at else None,
        gps=(
            {"latitude": asset.captured_location.latitude, "longitude": asset.captured_location.longitude}
            if asset.captured_location
            else None
        ),
    )


@router.post("/media/import", status_code=201)
async def import_media(
    file: UploadFile = File(...),
    scope_id: str = Form("home-default"),
    container: Container = Depends(get_container),
) -> ImportResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file")

    use_case = ImportAssets(
        assets=container.asset_repository,
        observations=container.observation_repository,
        storage=container.media_storage,
        metadata=container.metadata_reader,
        visual_indexer=IndexAssetVisual(
            embedder=container.visual_embedder,
            vectors=container.vector_repository,
            index=container.vector_index,
        ),
    )
    result = use_case.execute(
        ImportRequest(
            scope_id=scope_id,
            file_name=file.filename or "unnamed",
            content_type=file.content_type,
            content=content,
        )
    )
    return ImportResponse(
        asset_id=result.asset_id,
        observation_id=result.observation_id,
        media_type=result.media_type.value,
        status=result.status.value,
        deduplicated=result.deduplicated,
        indexed=result.indexed,
    )


@router.get("/assets")
def list_assets(
    scope_id: str = "home-default",
    limit: int = 200,
    container: Container = Depends(get_container),
) -> AssetListResponse:
    assets = container.asset_repository.list_by_scope(scope_id, min(limit, 1000))
    return AssetListResponse(items=[to_summary(asset) for asset in assets])


@router.get("/assets/{asset_id}")
def get_asset(asset_id: str, container: Container = Depends(get_container)) -> AssetSummary:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return to_summary(asset)


@router.get("/assets/{asset_id}/file")
def get_asset_file(asset_id: str, container: Container = Depends(get_container)) -> FileResponse:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    path = Path(asset.path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="media file missing on disk")
    return FileResponse(path, media_type=asset.mime_type or "application/octet-stream")


@router.get("/assets/{asset_id}/preview")
def get_asset_preview(asset_id: str, container: Container = Depends(get_container)) -> Response:
    asset = container.asset_repository.get(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    payload = container.preview_encoder.encode_jpeg(
        Path(asset.path), max_dimension=PREVIEW_MAX_DIMENSION, quality=PREVIEW_QUALITY
    )
    return Response(content=payload, media_type="image/jpeg")
```

- [ ] **Step 5: 运行测试，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/contract/test_search_routes.py -p no:warnings`
Expected:

```text
.........                                                                [100%]
9 passed
```

- [ ] **Step 6: 重新导出 OpenAPI 契约**

Run: `.venv/bin/python scripts/export_openapi.py`
Expected: `已写入 /home/asus/Github/sentrix-home-web2.0/packages/contracts/openapi/sentrix-v1.yaml`

新增 `/api/v1/search`（GET，参数 `q` 必填 1–200 字符、`scope_id` 默认 `home-default`、`limit` 1–100 默认 20）与 `SearchHit` / `SearchResponse` 两个 schema。

- [ ] **Step 7: 更新 module-map.md**

加入两行：

```markdown
| `sentrix/api/schemas/search.py` | v1 检索端点的响应模型（SearchHit / SearchResponse） | api | `sentrix/api/schemas/media.py` | `sentrix/api/routes/search.py` |
| `sentrix/api/routes/search.py` | `GET /api/v1/search`：按内容词检索，只做 HTTP 映射 | api | `sentrix/api/deps.py`、`sentrix/api/routes/media.py`、`sentrix/api/schemas/search.py`、`sentrix/application/retrieval/retrieve_visual.py`、`sentrix/bootstrap/container.py` | `sentrix/main.py` |
```

并改旧行：`sentrix/main.py` 的「依赖谁」加 `sentrix/api/routes/search.py`；`sentrix/api/deps.py`、`sentrix/api/schemas/media.py`、`sentrix/application/retrieval/retrieve_visual.py`、`sentrix/bootstrap/container.py` 的「被谁依赖」各加对应项。

Run: `.venv/bin/python scripts/check_module_map.py`
Expected: `module-map: 0 个问题`

- [ ] **Step 8: 登记能力**

```json
{"capability":"内容词检索接口","origin_kind":"NEW-2.0","source_commit":"2d2cee7c","source_path":"backend/retrieval/visual_ann.py","source_symbol":"VisualAnnRetriever.retrieve","target_path":"apps/api/src/sentrix/api/routes/search.py","verification":"apps/api/tests/contract/test_search_routes.py","status":"verified"}
```

- [ ] **Step 9: 全量门禁**

Run: `make ci`

Expected:

```text
Required test coverage of 85.0% reached. Total coverage: 97.65%
154 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

---

## Task 32: Web 搜索框

**依赖：** Task 31（HTTP 契约已定型）。

**Files:**
- Create: `apps/web/src/features/search/SearchBox.tsx`
- Create: `apps/web/src/features/search/SearchResults.tsx`
- Modify: `apps/web/src/shared/api/client.ts`
- Modify: `apps/web/src/App.tsx`
- Modify: `apps/web/src/styles.css`

前端不做任何相关性计算：搜索词直接交给后端，结果按后端给的顺序渲染。**降级状态要显式展示**——后端在降级，用户有权知道这次结果是怎么来的。

`make ci` 从 2026-09-22 起已覆盖前端（链尾的 `web-check`：`tsc -b` + `oxlint` + `vite build`），
所以这一步跑 `make ci` 即可；下面的三件套仍单独列出，方便只改前端时快速定位是哪一道挂的。
注意类型检查是 `tsc -b` 而不是 `tsc --noEmit`——后者在本仓库命中 0 个文件、恒通过。

- [ ] **Step 1: 先写前端**

`apps/web/src/features/search/SearchBox.tsx`:

```tsx
import { useState, type FormEvent } from "react";

import { searchAssets, type SearchResponse } from "../../shared/api/client";

/** 内容词检索入口。搜索是后端的事，这里只负责把词送过去、把结果交回去。 */
export function SearchBox({
  scopeId,
  onResults,
}: {
  scopeId: string;
  onResults: (response: SearchResponse) => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const query = text.trim();
    if (query === "") {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      onResults(await searchAssets(query, scopeId));
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="search" onSubmit={submit}>
      <input
        value={text}
        onChange={(event) => setText(event.target.value)}
        placeholder="搜内容词，例如 桥 / 海边 / 红色"
        aria-label="按内容词搜索照片"
      />
      <button type="submit" disabled={busy || text.trim() === ""}>
        {busy ? "搜索中…" : "搜索"}
      </button>
      {error ? <p className="error">{error}</p> : null}
    </form>
  );
}
```
`apps/web/src/features/search/SearchResults.tsx`:

```tsx
import { assetDisplayUrl, type SearchResponse } from "../../shared/api/client";

/** 检索结果。降级状态要显式展示——后端在降级，用户有权知道结果是怎么来的。 */
export function SearchResults({
  response,
  onClose,
}: {
  response: SearchResponse;
  onClose: () => void;
}) {
  return (
    <section className="results">
      <header className="results-header">
        <h2>
          「{response.query}」的结果（{response.items.length}）
        </h2>
        <button type="button" onClick={onClose}>
          返回全部照片
        </button>
      </header>
      {response.degraded ? (
        <p className="degraded">
          检索正在降级（{response.backend}）：{response.degraded_reason}
        </p>
      ) : null}
      {response.items.length === 0 ? (
        <p className="empty">
          {response.authoritative_vectors === 0 ? "还没有已索引的照片。" : "没有匹配的照片。"}
        </p>
      ) : (
        <ul className="grid">
          {response.items.map((item) => (
            <li key={item.asset.id}>
              {/* 结果一律给可打开的原图（HEIC 走服务端转码预览） */}
              <img src={assetDisplayUrl(item.asset)} alt={item.asset.file_name} loading="lazy" />
              <span>{item.asset.file_name}</span>
              <small>{item.score.toFixed(4)}</small>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
```

- [ ] **Step 2: 运行类型检查，确认红灯**

Run: `cd apps/web && npx tsc -b`
Expected:

```text
src/features/search/SearchBox.tsx(3,10): error TS2305: Module '"../../shared/api/client"' has no exported member 'searchAssets'.
src/features/search/SearchBox.tsx(3,29): error TS2305: Module '"../../shared/api/client"' has no exported member 'SearchResponse'.
src/features/search/SearchResults.tsx(1,32): error TS2305: Module '"../../shared/api/client"' has no exported member 'SearchResponse'.
src/features/search/SearchResults.tsx(32,32): error TS7006: Parameter 'item' implicitly has an 'any' type.
```

- [ ] **Step 3: 接上 API 客户端与 App**

`apps/web/src/shared/api/client.ts`（**替换为**（末尾追加 SearchHit / SearchResponse / searchAssets））:

```ts
const BASE = "/api/v1";

export interface AssetSummary {
  id: string;
  file_name: string;
  media_type: string;
  mime_type: string | null;
  size_bytes: number;
  status: string;
  captured_at: string | null;
  gps: { latitude: number; longitude: number } | null;
}

interface AssetListResponse {
  items: AssetSummary[];
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, init);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Sentrix API ${response.status}: ${body}`);
  }
  return (await response.json()) as T;
}

export async function listAssets(scopeId: string): Promise<AssetSummary[]> {
  const body = await request<AssetListResponse>(`/assets?scope_id=${encodeURIComponent(scopeId)}`);
  return body.items;
}

export function assetFileUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/file`;
}

export function assetPreviewUrl(assetId: string): string {
  return `${BASE}/assets/${assetId}/preview`;
}

/** 浏览器能直接渲染的格式走原图，其余（HEIC 等）走服务端转码预览。 */
export function assetDisplayUrl(asset: AssetSummary): string {
  if (asset.mime_type === "image/heic" || asset.mime_type === "image/heif") {
    return assetPreviewUrl(asset.id);
  }
  return assetFileUrl(asset.id);
}

export interface SearchHit {
  asset: AssetSummary;
  score: number;
  channels: Record<string, number>;
  raw_scores: Record<string, number>;
}

export interface SearchResponse {
  query: string;
  backend: string;
  degraded: boolean;
  degraded_reason: string;
  authoritative_vectors: number;
  items: SearchHit[];
}

/** 按内容词检索。后端负责嵌入、召回与融合，前端不做任何相关性计算。 */
export async function searchAssets(
  query: string,
  scopeId: string,
  limit = 20,
): Promise<SearchResponse> {
  const params = new URLSearchParams({ q: query, scope_id: scopeId, limit: String(limit) });
  return request<SearchResponse>(`/search?${params.toString()}`);
}
```
`apps/web/src/App.tsx`（**替换为**）:

```tsx
import { useEffect, useState } from "react";

import { AssetGrid } from "./features/library/AssetGrid";
import { SearchBox } from "./features/search/SearchBox";
import { SearchResults } from "./features/search/SearchResults";
import { listAssets, type AssetSummary, type SearchResponse } from "./shared/api/client";

const SCOPE_ID = "album1";

export default function App() {
  const [assets, setAssets] = useState<AssetSummary[]>([]);
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAssets(SCOPE_ID)
      .then(setAssets)
      .catch((cause: Error) => setError(cause.message));
  }, []);

  return (
    <main className="app">
      <h1>Sentrix Home 2.0</h1>
      <SearchBox scopeId={SCOPE_ID} onResults={setResults} />
      {error ? <p className="error">{error}</p> : null}
      {results ? (
        <SearchResults response={results} onClose={() => setResults(null)} />
      ) : (
        <AssetGrid assets={assets} />
      )}
    </main>
  );
}
```
`apps/web/src/styles.css`（**替换为**（末尾追加搜索框与降级横幅样式））:

```css
:root { color-scheme: light dark; font-family: system-ui, sans-serif; }
body { margin: 0; }
.app { padding: 1.5rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 0.75rem; list-style: none; padding: 0; }
.grid button { width: 100%; background: none; border: 1px solid #8884; border-radius: 8px; padding: 0.5rem; cursor: pointer; }
.grid img { width: 100%; height: 140px; object-fit: cover; border-radius: 4px; display: block; }
.grid span { display: block; font-size: 0.75rem; margin-top: 0.35rem; overflow-wrap: anywhere; }
.detail { position: fixed; inset: 0; background: #000c; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 1rem; padding: 2rem; }
.detail img { max-width: min(90vw, 1200px); max-height: 62vh; object-fit: contain; }
.detail dl { display: grid; grid-template-columns: auto auto; gap: 0.25rem 1rem; color: white; margin: 0; }
.detail dt { opacity: 0.7; }
.error { color: #c33; }
.empty { opacity: 0.7; }
.search { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
.search input { flex: 1; max-width: 28rem; padding: 0.5rem 0.75rem; border: 1px solid #8884; border-radius: 8px; font-size: 1rem; }
.search button { padding: 0.5rem 1rem; border: 1px solid #8884; border-radius: 8px; cursor: pointer; }
.results-header { display: flex; align-items: baseline; gap: 1rem; }
.results-header h2 { font-size: 1.1rem; margin: 0; }
.degraded { background: #ffb02022; border: 1px solid #ffb02066; border-radius: 8px; padding: 0.5rem 0.75rem; font-size: 0.85rem; }
.grid small { display: block; opacity: 0.6; font-size: 0.7rem; }
```

- [ ] **Step 4: 运行检查，确认绿灯**

Run: `cd apps/web && npx tsc -b && npx oxlint && npx vite build`
Expected:

```text
Found 0 warnings and 0 errors.
Finished on 8 files with 116 rules using 32 threads.
vite v8.3.0 building client environment for production...
✓ 21 modules transformed.
dist/assets/index-DGdUseF4.js   223.98 kB │ gzip: 70.45 kB
✓ built
```

- [ ] **Step 5: 全量门禁**

Run: `make ci`
Expected: `154 passed` + `ci: 全部通过`（前端改动不影响 Python 侧的计数）

---

## Task 33: 端到端验收

**依赖：** Task 25–32 全部。

**Files:**
- Create: `apps/api/tests/e2e/test_visual_search.py`
- Modify: `docs/refactor/capability-map.jsonl`

这一层用**真 chinese-clip（进程内 GPU）+ 真 Qdrant + 真 SQLite**，走完整 HTTP 链路。两条不循环的判据：

1. **构造出的 ground truth**——纯色图的颜色由测试自己画出来，期望值来自画图那一侧；
2. **两条独立实现互相校验**——Qdrant ANN 的路由结果必须与权威存储上的暴力精确检索逐名次一致。两边实现完全不同，只有"向量写对了、scope 过滤对了、排序没被融合层弄反"同时成立才会一致。

模型质量本身（"桥"找不找得到桥）按设计 §9.1 由使用者肉眼验收，不在这里假装验证。

- [ ] **Step 1: 先写测试**

`apps/api/tests/e2e/test_visual_search.py`:

```python
"""端到端验收：导入照片 → 用内容词搜到它。

这一层用**真 chinese-clip（进程内推理）+ 真 Qdrant + 真 SQLite**，走完整 HTTP 链路。
两条不循环的判据：

1. **构造出的 ground truth**：纯色图的颜色由测试自己画出来，期望值来自画图那一侧，
   被测的是模型的文本-图像对齐；
2. **两条独立检索路径互相校验**：Qdrant ANN 的路由结果必须与权威存储上的暴力精确
   检索逐名次一致——两边是两套实现，只有数据没错它们才会一致。

模型质量本身（"桥" 找不找得到桥）按设计 §9.1 由使用者肉眼验收，不在这里假装验证。
"""

from __future__ import annotations

import dataclasses
import io
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from sentrix.config import Settings
from sentrix.domain.retrieval import scheme
from sentrix.infrastructure.models.chinese_clip import DEFAULT_CHECKPOINT
from sentrix.infrastructure.vector.qdrant_index import QdrantVectorIndex
from sentrix.main import create_app

PHOTO_FIXTURES = Path("/home/asus/Github/Sentrix-Home-Web/data/album3-v2-source/photos")
SCOPE = "album1"

COLOURS = {"红色": (200, 30, 30), "蓝色": (30, 30, 200), "绿色": (30, 180, 60)}

_LOCK_HOLDER = """
import sys, time
from qdrant_client import QdrantClient
client = QdrantClient(path=sys.argv[1])
client.get_collections()
print("ready", flush=True)
time.sleep(60)
"""


@pytest.fixture(scope="module", autouse=True)
def requires_the_real_model() -> None:
    if not DEFAULT_CHECKPOINT.is_file():
        pytest.skip(f"chinese-clip 权重不可用: {DEFAULT_CHECKPOINT}")


@pytest.fixture
def real_client(settings: Settings) -> TestClient:
    """真容器：真模型、真 Qdrant、独立临时数据目录。"""
    return TestClient(create_app(settings))


def colour_photo(path: Path, colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (224, 224), colour).save(buffer, format="JPEG")
    path.write_bytes(buffer.getvalue())
    return buffer.getvalue()


def import_photo(client: TestClient, name: str, payload: bytes) -> str:
    response = client.post(
        "/api/v1/media/import",
        files={"file": (name, payload, "image/jpeg")},
        data={"scope_id": SCOPE},
    )
    assert response.status_code == 201, response.text
    assert response.json()["indexed"] is True, "导入必须写成视觉向量"
    return response.json()["asset_id"]


def search(client: TestClient, word: str, **params) -> dict:
    query = {"q": word, "scope_id": SCOPE}
    query.update(params)
    response = client.get("/api/v1/search", params=query)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("word", sorted(COLOURS))
def test_imported_photo_is_found_by_its_colour(real_client: TestClient, tmp_path: Path, word: str):
    """导入三张纯色图，用颜色词必须搜到对应那一张，且排第一。"""
    ids = {}
    for name, colour in COLOURS.items():
        payload = colour_photo(tmp_path / f"{colour[0]}.jpg", colour)
        ids[name] = import_photo(real_client, f"{colour[0]}.jpg", payload)

    body = search(real_client, word)

    assert body["degraded"] is False
    assert body["backend"] == "qdrant"
    assert body["authoritative_vectors"] == len(COLOURS)
    assert body["items"][0]["asset"]["id"] == ids[word], body


def test_qdrant_ranking_matches_the_exact_scan_on_the_authoritative_store(
    real_client: TestClient, tmp_path: Path
):
    """两条独立实现必须给出同一个名次。

    Qdrant 走 ANN + payload 过滤，权威存储走 SQLite 全表余弦——实现完全不同，
    只有「向量写对了、scope 过滤对了、排序没被融合层弄反」同时成立才会一致。
    """
    if not PHOTO_FIXTURES.is_dir():
        pytest.skip(f"真实照片夹具不可用: {PHOTO_FIXTURES}")
    photos = sorted(PHOTO_FIXTURES.glob("*.jpg"))[:8]
    if len(photos) < 8:
        pytest.skip(f"真实照片不足 8 张: {PHOTO_FIXTURES}")

    ids = {}
    for photo in photos:
        ids[photo.name] = import_photo(real_client, photo.name, photo.read_bytes())

    word = "海边"
    body = search(real_client, word, limit=8)
    api_order = [item["asset"]["id"] for item in body["items"]]

    container = real_client.app.state.container
    query_vector = container.visual_embedder.embed_query(word)
    exact = container.vector_repository.search_exact(
        space=scheme.VISUAL_SPACE,
        vector=query_vector,
        limit=8 * 4,
        scope_id=SCOPE,
        model_name=scheme.IMAGE_MODEL,
    )
    exact_order = [hit.source_id for hit in exact if hit.source_id in ids.values()][:8]

    assert api_order == exact_order, f"ANN 与精确回扫不一致：{api_order} != {exact_order}"
    # 分数是加权 RRF（visual 2.5 / RRK_K 60），不是原始余弦。
    assert body["items"][0]["score"] == pytest.approx(2.5 / 61)
    assert [item["channels"]["visual_ann"] for item in body["items"]] == list(range(1, len(api_order) + 1))


def test_search_is_deterministic(real_client: TestClient, tmp_path: Path):
    payload = colour_photo(tmp_path / "red.jpg", COLOURS["红色"])
    import_photo(real_client, "red.jpg", payload)

    first = search(real_client, "红色")
    second = search(real_client, "红色")

    assert first["items"] == second["items"]


def test_scope_isolation_holds_through_the_whole_chain(real_client: TestClient, tmp_path: Path):
    payload = colour_photo(tmp_path / "red.jpg", COLOURS["红色"])
    album1_id = import_photo(real_client, "red.jpg", payload)
    real_client.post(
        "/api/v1/media/import",
        files={"file": ("red.jpg", payload, "image/jpeg")},
        data={"scope_id": "album2"},
    )

    body = search(real_client, "红色", scope_id="album1")

    assert [item["asset"]["id"] for item in body["items"]] == [album1_id]


def test_locked_index_still_answers_with_the_right_photo(real_client: TestClient, tmp_path: Path):
    """S2 的硬约束端到端版本：Qdrant 目录被别的进程占用时，检索必须仍然给出正确照片，
    并把降级如实报出来（1.0 在这里是静默返回空）。"""
    locked_dir = tmp_path / "locked-qdrant"
    holder = subprocess.Popen(
        [sys.executable, "-c", _LOCK_HOLDER, str(locked_dir)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "ready"

        container = real_client.app.state.container
        real_client.app.state.container = dataclasses.replace(
            container,
            vector_index=QdrantVectorIndex(locked_dir),
            visual_embedder=container.visual_embedder,
        )

        payload = colour_photo(tmp_path / "red.jpg", COLOURS["红色"])
        red_id = import_photo(real_client, "red.jpg", payload)
        colour_photo(tmp_path / "blue.jpg", COLOURS["蓝色"])
        import_photo(real_client, "blue.jpg", (tmp_path / "blue.jpg").read_bytes())

        body = search(real_client, "红色")

        assert body["degraded"] is True
        assert body["backend"] == "sqlite_exact"
        assert "already accessed" in body["degraded_reason"]
        assert body["items"][0]["asset"]["id"] == red_id
        assert body["authoritative_vectors"] == 2
    finally:
        holder.terminate()
        holder.wait(timeout=10)
```

- [ ] **Step 2: 运行，确认绿灯**

Run: `.venv/bin/pytest apps/api/tests/e2e/test_visual_search.py -p no:warnings`
Expected:

```text
.......                                                                  [100%]
7 passed
```

（这一步没有"红灯"：它是把前四个任务已经各自验过的能力串起来。真正的红灯在前面每个任务里。）

- [ ] **Step 3: 登记能力**

```json
{"capability":"视觉记忆端到端检索","origin_kind":"NEW-2.0","source_commit":"2d2cee7c","source_path":"backend/retrieval/visual_ann.py","source_symbol":"VisualAnnRetriever.retrieve","target_path":"apps/api/src/sentrix/api/routes/search.py","verification":"apps/api/tests/e2e/test_visual_search.py","status":"verified"}
```

- [ ] **Step 4: 全量门禁**

Run: `make ci`

Expected:

```text
Contracts: 5 kept, 0 broken.
capability-map: 0 个问题
module-map: 0 个问题
openapi 契约一致
Required test coverage of 85.0% reached. Total coverage: 97.65%
161 passed   （警告条数随 torch 版本变，见文末「环境事实」；不计入判定）
ci: 全部通过
```

---

## 人工验收（使用者执行）

自动验收只能保证链路没坏。**"输入内容词能不能搜出正确的照片"必须用眼睛看**。

起服务（注意端口：9598 在 153 上已被别的进程占用，实测要换一个空闲端口）：

```bash
cd /home/asus/Github/sentrix-home-web2.0
SENTRIX_DATA_DIR=$PWD/data .venv/bin/python -m uvicorn --factory sentrix.main:create_app \
  --host 127.0.0.1 --port 8391 --app-dir apps/api/src
```

导入照片、搜索：

```bash
for f in /path/to/photos/*.jpg; do
  curl -s -F "file=@$f" -F scope_id=album1 http://127.0.0.1:8391/api/v1/media/import
done
curl -s "http://127.0.0.1:8391/api/v1/search?q=%E6%A1%A5&scope_id=album1&limit=10"
```

再起 Web（`cd apps/web && npm run dev -- --port 4274`，vite 把 `/api` 代理到 9598——**换端口时记得同步改 `vite.config.ts`**），在搜索框里输入内容词，看结果图对不对。

### 实测记录（规划阶段在隔离副本上跑过一遍）

数据：从 1.0 生产库里按 VLM 描述挑出 20 张有明确内容的照片（ground truth 来自另一个模型，与被测系统无关），另加 16 张随机干扰图，共 36 张、去重后 35 个资产。查询用真模型、真 Qdrant。

```text
--- 查询 '桥'   backend=qdrant degraded=False 已索引=35
   1. 0.04098 cosine=0.3061 桥_1_IMG_6022.jpg      （描述：海边大桥背景下的白色文字牌）
   2. 0.04032 cosine=0.2869 桥_0_IMG_4548.jpg      （描述：落日余晖下的河流与拱桥景观）
   3. 0.03968 cosine=0.2772 桥_2_IMG_6023.jpg
   4. 0.03906 cosine=0.2659 海边_0_2017-11-05 164457.jpg
   5. 0.03846 cosine=0.2639 桥_3_2017-11-05 153809.jpg
   该词标注照片的名次: [1, 2, 3, 5]  （共 4 张）
--- 查询 '海边' backend=qdrant degraded=False 已索引=35
   1. 0.04098 cosine=0.3429 海边_1_IMG_6458.JPG
   2. 0.04032 cosine=0.3408 海边_2_upload_30387298d776_IMG_6457.JPG
   该词标注照片的名次: [1, 2, 6]  （共 4 张）
--- 查询 '猫'   backend=qdrant degraded=False 已索引=35
   该词标注照片的名次: [1, 2, 4, 5]  （共 4 张）
--- 查询 '狗'   backend=qdrant degraded=False 已索引=35
   该词标注照片的名次: [1, 2, 3, 18]（共 4 张）
--- 查询 '雪'   backend=qdrant degraded=False 已索引=35
   该词标注照片的名次: [3, 4, 5, 12]（共 4 张）
--- 查询 '红色' backend=qdrant degraded=False 已索引=35
   该词标注照片的名次: []（这批样本里没有以红色为主色调的照片）
```

结论：内容词检索确实能命中正确照片。`雪` 的前两名是海边照片（雪与海在 CLIP 的空间里本来就近），属于模型能力的已知边界，不是链路问题。**没有相关性阈值**这件事在这里也看得见：`红色` 明明没有对应照片，仍然返回了 20 条排好序的候选。

---

## 已知边界与不做的事

| 项 | 说明 | 核实（对实现） |
|---|---|---|
| 没有相关性阈值 | 见 Task 31。校准层（`NeutralProbe` / per-space 阈值）属于 S6 | ✅ 全仓无 `threshold` / `min_score` / `cutoff` / `NeutralProbe` |
| 删资产不清向量 | `memory_vectors` 与 Qdrant 里的行会留下。检索侧会丢弃找不到资产的命中并记日志（产品硬约束第 5 条：只返回**可打开的**原图） | ✅ 丢弃+留痕在 `api/routes/search.py`（`asset_repository.get` 为 `None` → `log.warning` + `continue`）；全仓无删除端点、无 `delete`/`remove` 方法，所以「留下」目前无从触发 |
| 向量只增不改 | 换模型意味着重建索引。2.0 不提供"用另一套模型再嵌一遍"的路径——那正是 1.0 出问题的地方 | ✅ `index_visual.py` 的 `has_vector` → `already_indexed` 短路（重复导入不重嵌）；`scheme.py` 不读 env，没有换模型的入口 |
| 导入是同步的 | 一张图 GPU 约 65ms（实测 1,589 张 103 秒）。批量导入大相册会阻塞请求，S4 再谈后台作业 | ✅ `media.py` 的 `async def import_media` 只 `await` 读文件，随后直接调用同步的 `use_case.execute`，无 `to_thread` / `run_in_threadpool`——确实占着事件循环 |
| 没有 HNSW | 设计里 `data/ann/` 是另一条索引路径，本切片只用 Qdrant + SQLite 精确回扫 | ✅ 全仓无 `data/ann` / `.hnsw` 的代码路径，只在解释 1.0 行为的 docstring 里出现 |

## 验证记录

以下是在隔离副本 `/home/asus/s2verify/repo` 上实际跑出来的，不是承诺。

> **这是某一次实测的快照，不是现行断言。** 权威来源是各任务的 commit：**红灯**用
> 「前一任务 commit + 本任务的全部 `tests/` 文件」的重建树跑（Task 27 还要先补上它
> Step 1 的 `ports.py`），**绿灯**用该任务自己的 commit 跑。执行时要对照的是任务正文里的
> `Expected:` 块；本表只用于回顾「当时红成什么样、绿成什么样」，两者对不上时以正文与
> commit 为准。

### 每个任务的红灯（只留测试、去掉实现）

| 任务 | 命令 | exit | 观察到的失败 |
|---|---|---:|---|
| 25 | `pytest apps/api/tests/unit/test_fusion.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.domain.retrieval'` |
| 26 | `pytest apps/api/tests/integration/test_vector_repository.py apps/api/tests/unit/test_similarity.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.domain.retrieval.ports'` / `…retrieval.similarity` |
| 27 | `pytest apps/api/tests/integration/test_qdrant_index.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.infrastructure.vector'` |
| 28 | `pytest apps/api/tests/integration/test_chinese_clip.py apps/api/tests/unit/test_scheme.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.domain.retrieval.scheme'` / `ImportError: cannot import name 'scheme'` |
| 29 | `pytest apps/api/tests/unit/test_index_visual.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.application.retrieval'` |
| 29（契约层） | `pytest apps/api/tests/contract/test_media_routes.py` | 1 | `FAILED …::test_import_reports_and_writes_the_visual_vector` / `…::test_reimport_does_not_duplicate_the_visual_vector` |
| 30 | `pytest apps/api/tests/unit/test_retrieve_visual.py` | 2 | `ModuleNotFoundError: No module named 'sentrix.application.retrieval.retrieve_visual'` |
| 31 | `pytest apps/api/tests/contract/test_search_routes.py` | 1 | `assert 404 == 200`、`KeyError: 'items'`、9 条 FAILED |
| 32 | `cd apps/web && npx tsc -b` | 2 | `TS2305` ×3（`searchAssets` / `SearchResponse` / `SearchResponse`）＋ `TS7006: Parameter 'item' implicitly has an 'any' type` |

### 每个任务的绿灯（终态树上重跑一遍）

| 任务 | 命令 | 结果 |
|---|---|---|
| 25 | `pytest apps/api/tests/unit/test_fusion.py` | `10 passed` |
| 26 | `pytest apps/api/tests/integration/test_vector_repository.py apps/api/tests/unit/test_similarity.py` | `20 passed` |
| 27 | `pytest apps/api/tests/integration/test_qdrant_index.py` | `11 passed` |
| 28 | `pytest apps/api/tests/integration/test_chinese_clip.py apps/api/tests/unit/test_scheme.py` | `12 passed` |
| 29 | `pytest apps/api/tests/unit/test_index_visual.py apps/api/tests/contract/test_media_routes.py` | `16 passed` |
| 30 | `pytest apps/api/tests/unit/test_retrieve_visual.py apps/api/tests/integration/test_vector_repository.py` | `28 passed` |
| 31 | `pytest apps/api/tests/contract/test_search_routes.py` | `9 passed` |
| 32 | `cd apps/web && npx tsc -b && npx oxlint` | `Found 0 warnings and 0 errors.` |
| 33 | `pytest apps/api/tests/e2e/test_visual_search.py` | `7 passed` |
| 全部 | `make ci` | `5 kept, 0 broken` · `0 个问题` · `openapi 契约一致` · `161 passed` · `coverage 97.65%` · 前端 `web-check` 三道静默通过 · `ci: 全部通过` |

### 环境事实（规划时实测）

```text
torch 2.14.0+cu130 / torchvision 0.29.0+cu130   cuda_avail True
  约束是 torch>=2.5,<3 / torchvision>=0.20,<1，上面是在该范围下解析到的当前版本，
  不是硬钉值；规划时的沙箱解析到的是 2.5.1+cu124。两个版本下 S2 的全部测试都通过。
qdrant-client 1.19.1，自带 py.typed
  注：门禁 Expected 里**不再断言警告条数**——它是 torch 版本的函数。torch 2.5.1 下
  cn_clip 的 torch.load 发一条 FutureWarning，且每加载一次模型发一条（规划时 Task 28
  是 3 条，Task 33 的 e2e 每个用例各建一次真容器，放大到多条）；torch ≥ 2.6 把
  weights_only 默认值翻成 True 后这条不再发。与版本无关的固定项只有 starlette 那 2 条。
  同理，Expected 里**不写耗时**（``in X.XXs`` / ``in XXms``）：它是机器负载、GPU 状态
  与首次加载的函数，不是被测行为。判定只看 passed 数、覆盖率与错误文本。
cn-clip 1.6.0，无 py.typed（需要 mypy override）
~/.cache/clip/clip_cn_vit-l-14.pt  1,625,135,766 字节
chinese-clip 加载 3.1s，768 维；GPU 上单张图约 65ms（1,589 张 103s）
  ⚠ 这一行的三个数是**规划时在隔离副本上、torch 2.5.1+cu124 下**测的；本机解析到 2.14.0+cu130
  后未重测。拿它们做性能预期前，先在目标机上重测一次。
Qdrant 本地模式第二个进程打开同一目录：
  RuntimeError: Storage folder <path> is already accessed by another instance of Qdrant client.
```

### 遗留

- **代码在隔离副本里，没进真实仓库。** 执行阶段要按本计划逐任务真写一遍。副本路径 `/home/asus/s2verify/repo` 可以留着对照，但不要直接 `cp` 过去——照着计划走一遍才是这份文档的价值所在。
- **Task 27/28 的 `pyproject.toml` 依赖声明，在已提交历史里落在 T28。** 本计划修正后应落在 Task 27 的 Step 5（`qdrant-client` 是 T27 自己的依赖）。已提交的 T27 没有动 `pyproject.toml`，是 T28 才把四个包一起加上的；最终文件内容与副本逐字节一致，只是 commit 边界对不上计划。对着历史看计划时别以为漏了。
- `9598` 端口在 153 上已被另一个进程占用（非 1.0 的 8091）。人工验收换空闲端口，`vite.config.ts` 的代理要同步改。

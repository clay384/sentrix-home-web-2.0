#!/usr/bin/env python3
"""把已有资产的视觉向量补上（W1）。**幂等，可反复跑。**

**为什么需要它。** 写入侧（`IndexAssetVisual`）是导入流水线的一环，只对**此后**导入的
图片生效。W1 把嵌入方案从 `chinese-clip(768d)` 换成 `tencent/WeMM-Embedding-2B(2048d)`
之后，存量资产在 `memory_vectors` 里的那行是**旧模型名下**的 768 维向量，而检索侧按
`model_name` 过滤——于是**一张图都搜不到**。这个脚本按当前方案重嵌一遍。

**它不是「重建索引」，是「补当前方案缺的那一行」。** 旧 model_name 的行原样留着不删
（`memory_vectors` 的唯一键含 `model_name`，同一条资产可以并有多套模型的向量）。
这与 1.0 `rebuild_scope_vectors.py:6-9` 同一条口径：切回旧方案仍可用。

**幂等靠用例自己**：`IndexAssetVisual.index` 先查 `has_vector` 再嵌，
所以重跑不会重复打侧车、也不会写重复行。**因此「本次写了 0 行」不是失败**——
第二次跑本来就是 0。判据要看**这个 scope 现在有多少张图真的带当前方案的向量**
（`覆盖率`），那才是「这条通道通没通」。第一版把「本次 0 行」当失败，于是幂等重跑
会报错，而它完全正常——文本回填那边踩过一次，这里照它的写法。

**人脸向量不在范围内。** `memory_vectors` 的 `visual` 空间里同时住着 `buffalo_l` 的
512 维人脸向量（`source_type=face_instance`），那是另一个模型、另一条链路，
换检索嵌入方案不影响它。

**跑之前先把 API 停掉。** 派生索引（Qdrant）是嵌入式本地实例，**同一份存储目录
同时只能有一个进程打开**。API 在跑时回填，权威存储照写，但每张图的镜像都会以
`Storage folder ... already accessed by another instance` 失败——日志里看得见，
可**重跑补不回来**：`IndexAssetVisual.index` 见到权威存储里已有行就直接早返回
（`reason="already_indexed"`），根本不会再走镜像那一步。要补救只能先删掉那些行
再重跑（或者干脆把服务停掉再跑）。

用法：
    # 先停掉 API（9598），否则镜像会静默丢掉
    .venv/bin/python scripts/backfill_visual_vectors.py [scope]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api" / "src"))

from sentrix.application.retrieval.index_visual import IndexAssetVisual  # noqa: E402
from sentrix.bootstrap.container import Container  # noqa: E402
from sentrix.config import load_settings  # noqa: E402
from sentrix.domain.retrieval import scheme  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="回填视觉向量（幂等，可反复跑）")
    parser.add_argument("scope", nargs="?", default="album1")
    scope = parser.parse_args().scope

    container = Container.build(load_settings())
    indexer = IndexAssetVisual(
        embedder=container.visual_embedder,
        vectors=container.vector_repository,
        index=container.vector_index,
    )

    assets = [item for item in container.asset_repository.list_by_scope(scope, 10_000)]
    images = [item for item in assets if item.media_type.value == "image"]
    print(f"scope={scope}  资产 {len(assets)} 个，其中图片 {len(images)} 张")
    print(f"嵌入模型 {container.visual_embedder.model_id} / {container.visual_embedder.dimension} 维\n")
    if not images:
        print("没有图片可回填。")
        return 0

    written = 0
    already = 0
    failed: list[str] = []

    for asset in images:
        outcome = indexer.index(asset)
        if outcome.indexed and outcome.reason == "already_indexed":
            already += 1
            continue
        if not outcome.indexed:
            failed.append(f"{asset.id}: {outcome.reason}")
            continue
        written += 1

    # 真正的判据：**现在**有多少张图带当前方案的向量（不是本次写了几张）
    covered = sum(
        1
        for asset in images
        if container.vector_repository.has_vector(
            space=scheme.VISUAL_SPACE,
            source_type="asset",
            source_id=asset.id,
            model_name=container.visual_embedder.model_id,
        )
    )

    print(f"本次写 {written} 张（重跑时正常为 0——用例自带幂等），已有 {already} 张")
    print(f"\n视觉向量覆盖率：{covered}/{len(images)}")

    if failed:
        print(f"\n✗ 失败 {len(failed)} 张：")
        for item in failed[:10]:
            print(f"    {item}")
        return 1
    if covered == 0:
        print("\n✗ 覆盖率是 0——不要当成「已经写过了」，先看嵌入侧是不是真的连着。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

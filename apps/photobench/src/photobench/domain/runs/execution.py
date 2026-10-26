"""run-execution 主链的**全部纯计算**（S9b-2）。

与 ``domain.runs.analysis`` 的分工：``analysis`` 是**读侧**（把已落盘的 run 变成列表页
与详情页要的东西），这里是**写侧**（把主产品的一次问答变成要落盘的 item）。
两者都落在 domain，因为两侧都不碰 IO，而 infrastructure 的结果写入要能用这里的形状。

**这一层与 1.0 的关系是一半照搬、一半重算，分界线是「2.0 是否还暴露那个信息」：**

照搬（2.0 有等价物的）：

- ``normalize_media_refs``  1.0 ``:955``——题面里的 GT 媒体引用，两代都是数据集里的字段。
- ``resolve_predicted_media``  1.0 ``:1129``——把 asset_id 解成带类型/文件名的记录。
- ``resolve_gt_media``  1.0 ``:1364``——把 GT 引用按已入库资产解出映射状态。
- ``modality_metrics``  1.0 ``:1206``——GT 与交付集的 precision/recall/f1。
- ``safe_slug`` / ``album_media_entries``  1.0 ``:1581`` / ``:1587``。

**不能照搬、只能重算的**（1.0 有、2.0 根本没有的信息）：

1.0 的 item 里有一整套模型侧轨迹——``model_call_metrics``（每次调用的 ttft/token）、
``execution_trace`` / ``tool_trace``（工具入参、结果、``debug_asset_ids``）、
``retrieval_trace``、``agent2_trace``、``answer_grounding``、``tool_perf``，
以及由它们算出的 ``image_ranking`` / ``timing_breakdown``。**2.0 一个都没有**：
``GET /assistant/turns/{id}`` 只给 ``{status, termination_reason, answer, attachments,
requirements, public_progress}``，``public_progress`` 的 payload 里只有**工具名与
tool_call_id**，没有参数、没有结果、没有 asset_id、没有耗时、没有 token
（S9b-2 逐行核过 ``domain/assistant/task_state.py`` 的 ``emit`` 与 ``run_turn.py`` 的
全部发射点，并实跑一轮确认）。2.0 也没有任何别的端点能把它们读出来——
``agent_turn_events`` 表的唯一读取者是这两个轮询/SSE 端点。

所以本层**不发明**这些字段：``predicted_media`` 与召回指标只对齐到 2.0 真的给了的
**交付集**（``result.attachments[].asset_id``），``retrieved_candidate_media`` /
``evidence_source_media`` / ``image_ranking`` **留空并说明原因**——把候选集与交付集
混成一个数字，正是 1.0 ``_extract_media_sets`` 那段注释反复警告的事；而在 2.0 里
连候选集都拿不到，编一个只会让指标看起来完整。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from photobench.domain.runs.analysis import (
    VIDEO_EXTENSIONS,
    infer_media_type,
    media_key,
    metric_triplet,
)

#: 2.0 的 ``POST /api/v1/imports`` 用 ``Path.iterdir()`` **非递归**扫目录
#: （``application/media/import_album.py:100-102``），所以导入的单位是「一个扁平目录」，
#: 不是「一份 manifest 里的文件清单」。这条是由 2.0 的端点形状决定的，不是选择。
IMPORT_DIRECTORY_IS_FLAT = True


class RunCancelledError(RuntimeError):
    """一次 run 被取消时，正在等待的那一段抛它（1.0 ``:905`` 同名类）。

    **它继承 ``RuntimeError`` 而不是 ``DomainError``**，这一点是有意的：取消是**运行
    状态**，不是请求错误。如果它是 ``DomainError``，``api/errors.py`` 会把它映射成
    400 回给发起取消的那个 HTTP 请求——而取消请求本身是成功的，应该是 202/200。
    1.0 也是 ``RuntimeError``。

    抛出点有三处（与 1.0 相同）：judge 重试退避、judge 限流等待、模型/回合等待。
    ``RunExecution.execute`` 接住它并把 run 收成 ``cancelled``——它必须与「某个相位
    失败」分开，否则一次主动取消会在 run.json 里显示成一个错误。
    """

    def __init__(self, message: str = "run cancelled") -> None:
        super().__init__(message)
        self.message = message


def safe_slug(value: str, fallback: str = "x") -> str:
    """1.0 ``:1581``。给 scope 名与 run 目录名用，所以只留 ``[a-z0-9._-]``。"""
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip()).strip("-._").lower()
    return slug or fallback


def album_media_entries(manifest: dict[str, Any]) -> list[str]:
    """1.0 ``:1587``：manifest 里 photo 在前、video 在后，去重保序。"""
    entries: list[str] = []
    seen: set[str] = set()
    for key in ("photos", "videos"):
        for item in manifest.get(key) or []:
            relative = str(item).strip()
            if relative and relative not in seen:
                seen.add(relative)
                entries.append(relative)
    return entries


def import_directories(manifest: dict[str, Any]) -> list[str]:
    """manifest 的媒体条目落在哪几个**扁平目录**里（相对路径，保序）。

    2.0 的整册导入一次只吃一个目录，而一个 album 的 manifest 里 photos 与 videos
    可能在不同的子目录（``photos/`` 与 ``videos/``）。所以主链按这个函数的返回值
    **逐目录导一次**，scope 相同、结果累加——1.0 没有这一步，它的 ``/api/import``
    是按文件收的，2026-10-04 的范围调整把这一层差异落在这里。

    没有条目的 album 返回空表（主链据此判「没有可导的东西」，与 1.0
    ``_phase_photo_import`` 对 ``missing`` 的处理是同一条路）。
    """
    directories: list[str] = []
    seen: set[str] = set()
    for relative in album_media_entries(manifest):
        parent = str(Path(relative).parent)
        if parent in {".", ""}:
            parent = ""
        if parent not in seen:
            seen.add(parent)
            directories.append(parent)
    return directories


def index_assets_by_name(assets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """1.0 ``_phase_qa_eval`` ``:3108``：按**文件名 basename** 归组。

    用 list 而不是单值：同一张照片在同一个 scope 里只该有一行，但跨 scope 查询
    （``scope_id`` 为空）会拿到重名——那时 ``resolve_gt_media`` 会把映射标成
    ``ambiguous`` 而不是随手挑一张。
    """
    index: dict[str, list[dict[str, Any]]] = {}
    for asset in assets:
        name = Path(str(asset.get("file_name") or "")).name
        index.setdefault(name, []).append(asset)
    return index


def normalize_media_refs(record: dict[str, Any], prefix: str = "retrieval") -> list[dict[str, str]]:
    """1.0 ``:955``：``{prefix}_media_refs`` 优先，否则从 ``_image_ids`` / ``_video_ids`` 升级。

    ``_video_ids`` 那一路显式给 ``"video"`` 类型——那是 1.0 的兼容路径，光看 id 猜不出
    是视频（视频 id 常常就是一个普通文件名）。这条不能省：2.0 的评测题库还是同一批
    ``.jsonl``，老题只有 ``retrieval_image_ids`` 与新题有 ``retrieval_media_refs``。
    """
    refs = record.get(f"{prefix}_media_refs")
    candidates: list[tuple[object, object]] = []
    if isinstance(refs, list):
        for ref in refs:
            if isinstance(ref, dict):
                candidates.append((ref.get("media_type"), ref.get("media_id") or ref.get("id")))
    else:
        for value in record.get(f"{prefix}_image_ids") or []:
            candidates.append((None, value))
        for value in record.get(f"{prefix}_video_ids") or []:
            candidates.append(("video", value))

    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for explicit_type, value in candidates:
        media_id = str(value or "").strip()
        if not media_id:
            continue
        media_type = infer_media_type(media_id, explicit_type)
        key = media_key(media_type, media_id)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({"media_type": media_type, "media_id": media_id})
    return normalized


def selected_asset_ids(turn_result: dict[str, Any]) -> list[str]:
    """2.0 的**交付集**：``result.attachments[].asset_id``。

    对应 1.0 ``_extract_media_sets``（``:984``）里的 ``selected_asset_ids`` 那一支。
    1.0 那一支有六七个来源（``image_delivery`` / ``answer_grounding.selected_delivery``
    / ``debug_preview_handles`` 的映射…），2.0 只有一个：``AttachmentSummary``
    （``api/schemas/assistant.py:47``）。**这不是简化，是 2.0 的权威定义**——
    交付给用户的就是这三个字段那一份，别的都没有对外。

    保序去重（1.0 用 ``dict.fromkeys``）：交付顺序是模型的选择顺序，后面
    ``delivery_matched_file_names`` 的排序才是另一回事。
    """
    attachments = turn_result.get("attachments")
    ids = [
        str(item.get("asset_id"))
        for item in (attachments or [])
        if isinstance(item, dict) and item.get("asset_id")
    ]
    return list(dict.fromkeys(ids))


def retrieved_candidate_asset_ids(turn_result: dict[str, Any]) -> list[str]:
    """2.0 的**候选集**：``result.retrieved_candidates``——检索捞回来的那批 ``asset_id``。

    **它与 ``selected_asset_ids`` 是两个不同的问题**，合起来才分得出锅：
    ``selected`` 是「模型从候选里挑了哪几张交出去」，这个是「检索给了它哪些」。
    只有交付集的时候，「检索没找到」与「找到了没交付」在指标上完全一样——而这才是
    ``retrieval_*`` 那一族此前失真的根因：分母是 GT、分子是交付集，394 道零交付的题
    recall 一道不差全是 0.0。

    契约在 ``api/schemas/assistant.py`` 的 ``CompleteEventPayload.retrieved_candidates``。
    老结果（这次放宽之前跑的）没有这个字段，读到**空列表**而不是回填交付集——
    回填就又把两件事合成一件了。
    """
    candidates = turn_result.get("retrieved_candidates")
    ids = [str(item) for item in (candidates or []) if item]
    return list(dict.fromkeys(ids))


def resolve_predicted_media(
    asset_ids: list[str], assets_by_name: dict[str, list[dict[str, Any]]]
) -> list[dict[str, Any]]:
    """1.0 ``:1129``：asset_id → 带 ``media_type`` / ``file_name`` / ``media_id`` 的记录。

    **视频关键帧回落到父视频那一段照搬**：模型交付的常常是抽出来的关键帧
    （``derived_kind == "video_keyframe"``），而 GT 标的是那段视频。不回落的后果是
    同一件事在指标里算成「没召回」。这条是 1.0 的口径（``:1135-1152``），2.0 的
    ``/videos/{id}/scenes`` 造出来的关键帧同样是 ``video_keyframe``。
    """
    assets_by_id = {
        str(asset["id"]): (file_name, asset)
        for file_name, assets in assets_by_name.items()
        for asset in assets
        if asset.get("id")
    }
    resolved: list[dict[str, Any]] = []
    for asset_id in asset_ids:
        match = assets_by_id.get(str(asset_id))
        if not match:
            continue
        file_name, asset = match
        raw_metadata = asset.get("metadata_json")
        metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
        derived_kind = str(asset.get("derived_kind") or metadata.get("derived_kind") or "")
        parent_asset_id = str(asset.get("parent_asset_id") or metadata.get("parent_asset_id") or "")
        if derived_kind in {"video_keyframe", "video_keyframe_webp"} and parent_asset_id in assets_by_id:
            source_file_name, source_asset = assets_by_id[parent_asset_id]
            source_type = infer_media_type(
                source_file_name, source_asset.get("media_type") or source_asset.get("asset_type")
            )
            if source_type == "video":
                media: dict[str, Any] = {
                    "asset_id": parent_asset_id,
                    "file_name": source_file_name,
                    "media_type": "video",
                    "media_id": Path(source_file_name).stem,
                    "source_timestamp_sec": asset.get("source_timestamp_sec")
                    or metadata.get("source_timestamp_sec"),
                    "source_keyframe_asset_id": str(asset_id),
                }
                if source_asset.get("media_url"):
                    media["media_url"] = source_asset["media_url"]
                resolved.append(media)
                continue
        media_type = infer_media_type(file_name, asset.get("media_type") or asset.get("asset_type"))
        media = {
            "asset_id": str(asset_id),
            "file_name": file_name,
            "media_type": media_type,
            "media_id": Path(file_name).stem if media_type == "video" else file_name,
        }
        if asset.get("media_url"):
            media["media_url"] = asset["media_url"]
        resolved.append(media)
    return resolved


def resolve_gt_media(
    gt_refs: list[dict[str, str]],
    assets_by_name: dict[str, list[dict[str, Any]]],
    retrieved_media: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """1.0 ``:1364``：GT 引用 → 已入库资产，带 ``mapping_status``。

    ``mapping_status`` 三态（``ok`` / ``missing`` / ``ambiguous``）是 1.0 就有的诊断：
    GT 在库里找不到时，召回率的分母仍然算它，但**原因要留下来**——否则「题目标错了」
    与「导入漏了这张」看起来一模一样。
    """
    asset_index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for file_name, assets in assets_by_name.items():
        for asset in assets:
            media_type = infer_media_type(file_name, asset.get("media_type") or asset.get("asset_type"))
            asset_index.setdefault(media_key(media_type, file_name), []).append(asset)
    retrieved_keys = {
        media_key(item.get("media_type"), item.get("media_id") or item.get("file_name"))
        for item in retrieved_media
    }
    result: list[dict[str, Any]] = []
    for ref in gt_refs:
        media_type = ref["media_type"]
        media_id = ref["media_id"]
        key = media_key(media_type, media_id)
        candidates = asset_index.get(key, [])
        file_name = Path(media_id).name
        if media_type == "video" and Path(file_name).suffix.lower() not in VIDEO_EXTENSIONS:
            file_name = f"{file_name}.mp4"
        result.append(
            {
                "media_type": media_type,
                "media_id": media_id,
                "image_id": media_id if media_type == "image" else None,
                "video_id": media_id if media_type == "video" else None,
                "file_name": file_name,
                "asset_id": candidates[0].get("id") if len(candidates) == 1 else None,
                "matched": key in retrieved_keys,
                "mapping_status": (
                    "ok" if len(candidates) == 1 else "missing" if not candidates else "ambiguous"
                ),
            }
        )
    return result


def modality_metrics(
    gt_refs: list[dict[str, str]], predicted_media: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """1.0 ``:1206``：总量、图片、视频三组 triplet。

    ``predicted_media`` 是**哪一批**由调用方决定，函数本身不关心。调用方现在传两次：
    一次交付集（``predicted_media``）、一次候选集（``retrieved_candidate_media``）——
    1.0 把那三个集分开算正是因为它的候选集与交付集是两份数据；2.0 此前只有交付一份，
    于是「检索没找到」与「找到了没交付」在指标上完全一样。
    """
    gt_keys = {media_key(ref.get("media_type"), ref.get("media_id")) for ref in gt_refs}
    predicted_keys = {
        media_key(item.get("media_type"), item.get("media_id") or item.get("file_name"))
        for item in predicted_media
    }
    result = {"media": metric_triplet(gt_keys, predicted_keys)}
    for media_type in ("image", "video"):
        result[media_type] = metric_triplet(
            {key for key in gt_keys if key[0] == media_type},
            {key for key in predicted_keys if key[0] == media_type},
        )
    return result


def summarize_progress(public_progress: list[Any]) -> dict[str, Any]:
    """把 2.0 的 ``public_progress`` 收成一份可读的轨迹摘要。

    **这是本切片对「轨迹」能做的全部**——原始事件在 item 的 ``trace`` 里逐条落盘，
    这里只是给读侧一个不用自己解析 JSON 的入口。四项都直接从事件本身取，不做推断：

    - ``steps``：事件条数（``step_index`` 的最大值，1.0 没有对应物）；
    - ``stages`` / ``kinds``：阶段与种类的计数（规划/工具循环/终态各几次）；
    - ``tools``：**工具名与出现次数**——2.0 的 payload 只给 ``tool`` 与
      ``tool_call_id``（``run_turn.py:651-657``），没有入参与结果。1.0 的
      ``tool_trace`` 有入参、结果与 ``debug_asset_ids``，**2.0 没有**，所以这里
      不编 ``tool_trace``：一个只有工具名的列表，叫 ``tools`` 才是诚实的名字。

    ``tool_result`` 的对账按 ``tool_call_id`` 配对：2.0 会为同一次调用发
    ``model_action``（发出调用）与 ``tool_result``/``tool_error``（收结果）两条，
    后者才是「真的执行了」。1.0 的 ``_extract_tool_perf`` 同样按结果计数。
    """
    stages: dict[str, int] = {}
    kinds: dict[str, int] = {}
    tools: dict[str, int] = {}
    errors: dict[str, int] = {}
    max_step = 0
    executed: dict[str, int] = {}
    for event in public_progress or []:
        if not isinstance(event, dict):
            continue
        kind = str(event.get("kind") or "")
        stage = str(event.get("stage") or "")
        if kind:
            kinds[kind] = kinds.get(kind, 0) + 1
        if stage:
            stages[stage] = stages.get(stage, 0) + 1
        step_index = event.get("step_index")
        if isinstance(step_index, int):
            max_step = max(max_step, step_index)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        tool = str(payload.get("tool") or "") if payload else ""
        if tool:
            tools[tool] = tools.get(tool, 0) + 1
            if kind in {"tool_result", "tool_error"}:
                executed[tool] = executed.get(tool, 0) + 1
        if kind in {"tool_error", "guard"} and payload:
            code = str(payload.get("code") or "unknown")
            errors[code] = errors.get(code, 0) + 1
    return {
        "steps": max_step,
        "stages": stages,
        "kinds": kinds,
        "tools": tools,
        "executed_tools": executed,
        "error_codes": errors,
    }


#: 2.0 的进度事件里，哪些 kind 是「一次工具调用」的两半。
_TOOL_EVENT_KINDS = frozenset({"model_action", "tool_result", "tool_error"})


def traces_from_progress(
    turns: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把 2.0 的 ``public_progress`` 事件流合成为 1.0 的 ``tool_trace`` 与 ``execution_trace``。

    **为什么是「合成」而不是「照搬」。** 1.0 的助手把 ``tool_trace`` / ``execution_trace``
    当成两个现成的字段返回；2.0 的助手只返回**一条事件流**（``public_progress``），
    它是前端进度条与本层轨迹的**同一个来源**。所以这里做的是一次投影：事件流 → 两条轨迹。
    这样只有一个真相，不存在「轨迹说 A、进度条说 B」。

    形状对齐 1.0 是**必须的**，因为读侧的 ``bind_tool_calls_to_model_rounds`` /
    ``annotate_model_calls``（``analysis.py``）早就写好了，它们按
    ``(conversation_turn, parent_step_id)`` 把工具挂回模型轮次——挂不上那整棵
    Agent Loop 调用树就是空的。**读侧没缺过代码，缺的一直是这两格**：

    - 一次工具调用在事件流里是**两条**（``model_action`` 发起、``tool_result`` 收尾），
      这里按 ``tool_call_id`` 合成一条；
    - ``step_id`` 取 ``tool_call_id``（工具自己的 id），``parent_step_id`` 取发起它那次
      模型调用的 ``step_N``——两者恰好是 1.0 那套绑定要找的键。
    """
    tool_trace: list[dict[str, Any]] = []
    execution_trace: list[dict[str, Any]] = []
    # 键 -> **那一行本身**。存下标会在第二次事件时把 int 当 dict 用。
    row_by_call: dict[tuple[Any, str], dict[str, Any]] = {}
    for turn in turns:
        turn_index = turn.get("index")
        for event in turn.get("trace") or []:
            if not isinstance(event, Mapping):
                continue
            payload = event.get("payload") or {}
            step_id = payload.get("step_id")
            execution_trace.append(
                {
                    "step_id": step_id,
                    "parent_step_id": payload.get("parent_step_id"),
                    "conversation_turn": turn_index,
                    "stage": event.get("stage"),
                    "type": event.get("kind"),
                    "status": event.get("status"),
                    "text": event.get("text"),
                    "timestamp": event.get("timestamp"),
                }
            )
            if event.get("kind") not in _TOOL_EVENT_KINDS or not payload.get("tool"):
                continue
            key = (turn_index, str(payload.get("tool_call_id") or step_id))
            row = row_by_call.get(key)
            if row is None:
                row = {
                    "step_id": step_id,
                    "parent_step_id": payload.get("parent_step_id"),
                    "conversation_turn": turn_index,
                    "tool": str(payload.get("tool")),
                    "status": None,
                    "arguments": {},
                    "duration_ms": None,
                    "result": {},
                    "text": "",
                }
                row_by_call[key] = row
                tool_trace.append(row)
            # 后到的那条（收尾）覆盖状态与结果；``arguments`` 只有发起那条有。
            if payload.get("arguments") is not None:
                row["arguments"] = dict(payload["arguments"])
            if event.get("kind") != "model_action":
                row["status"] = str(event.get("status") or "")
                row["text"] = str(event.get("text") or "")
                if payload.get("duration_ms") is not None:
                    row["duration_ms"] = payload["duration_ms"]
                if payload.get("result") is not None:
                    row["result"] = dict(payload["result"])
        # **每次模型调用也是一条执行步骤**，而且必须带 ``stage="model"``：读侧的
        # ``analysis.agent_stability`` 只认这一种（``stage/type == "model"`` +
        # ``call_type``），数出来的 ``json_parse_total`` 喂给「主 Agent 稳定性」那张卡。
        # 不带它，那张卡恒显示「未记录」——而模型调用的事实**就在手上**。
        for call in (turn.get("trace_bundle") or {}).get("model_calls") or []:
            tools = [str(name) for name in (call.get("tool_calls") or [])]
            execution_trace.append(
                {
                    "step_id": call.get("step_id"),
                    "conversation_turn": turn_index,
                    "stage": "model",
                    "type": "model_call",
                    "call_type": str(call.get("call_type") or "agent"),
                    "status": call.get("status", "complete"),
                    "tool_argument_parse_facts": call.get("tool_argument_parse_facts"),
                    "error_type": call.get("error_type"),
                    "attempt": call.get("attempt"),
                    # ``action`` 是「这一步做了什么」：调了哪个工具，或者就是收口。
                    # ``agent_stability`` 的 ``parsed_success`` 按它判「这一步解析出了动作」。
                    "action": tools[0] if tools else "final",
                    "prompt_tokens": call.get("prompt_tokens"),
                    "completion_tokens": call.get("completion_tokens"),
                    "duration_ms": call.get("duration_ms"),
                }
            )
    return tool_trace, execution_trace


def agent2_trace_from(trace: Mapping[str, Any] | None, *, termination_reason: str) -> dict[str, Any]:
    """把助手报的**回合级事实**摆成 1.0 的 ``agent2_trace`` 形状。

    1.0 的助手直接返回 ``agent2_trace``；2.0 只返回事实（``task_state`` / ``planner`` /
    ``evidence_ledger`` / ``budget``），**摆盘是评测侧的事**——同一个事实在三个消费者那里
    可以有不同摆法，而它只有一个来源。

    前端 ``Agent2SummaryPanel`` 整块依赖这个形状：没有它，那个面板**从来不会出现**
    （``getAgent2Trace`` 返回 null 就 ``return null``）；记忆层那张「证据账本」卡也
    一直显示「未记录」。
    """
    if not trace:
        return {}
    task_state = trace.get("task_state") or {}
    requirements = [item for item in (task_state.get("requirements") or []) if isinstance(item, Mapping)]
    status_counts: dict[str, int] = {}
    for requirement in requirements:
        status = str(requirement.get("status") or "")
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1
    planner = trace.get("planner") or {}
    decisions = (
        [
            {
                "status": planner.get("status"),
                "reason": planner.get("fallback_reason") or "",
                "detail": planner.get("detail") or "",
            }
        ]
        if planner
        else []
    )
    # ``partial_entries``：1.0 判的是「取到的条数少于要的条数」，2.0 没有那个计数，
    # 用 ``unmet_reason``——它回答的是同一个问题「这条需求是不是没凑齐」，而且更准
    # （W7 把「取了没取到」与「根本没去取」分开记了）。
    return {
        "planner_decisions": decisions,
        "task_declaration": {
            "goal": planner.get("goal") or "",
            "requirements": requirements,
        },
        "task_state": dict(task_state),
        "evidence_ledger": dict(trace.get("evidence_ledger") or {}),
        "requirement_status_counts": status_counts,
        "evidence_coverage": {
            "entries": len(requirements),
            "partial_entries": sum(1 for item in requirements if item.get("unmet_reason")),
        },
        "terminal_reason": termination_reason,
        "budget_outcome": dict(trace.get("budget") or {}),
        "guard": list(trace.get("guard") or []),
    }


def debug_steps_from(events: Sequence[Any]) -> list[dict[str, Any]]:
    """``DebugTracePanel`` 要的逐步轨迹（2.0 的进度事件投影）。

    这里**不复制**模型的完整输入输出：它们在回合的 ``trace_bundle.debug_calls`` 里，
    一条调用一份。本函数只保留「这条事件对应哪次模型调用」的锚点
    （``step_id`` / ``parent_step_id`` / ``call_id``），让前端按锚点去取，
    避免同一份 prompt 在 run.json 里出现两次。
    """
    steps: list[dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload") or {}
        step: dict[str, Any] = {
            "type": str(event.get("kind") or ""),
            "status": str(event.get("status") or ""),
            "text": str(event.get("text") or ""),
        }
        for key in ("step_id", "parent_step_id", "call_id"):
            if payload.get(key) is not None:
                step[key] = payload[key]
        if payload.get("tool"):
            step["tool"] = payload["tool"]
        if payload.get("arguments") is not None:
            step["arguments"] = dict(payload["arguments"])
        if payload.get("result") is not None:
            step["observation"] = dict(payload["result"])
        if payload.get("duration_ms") is not None:
            step["duration_ms"] = payload["duration_ms"]
        steps.append(step)
    return steps


def model_identity(payload: dict[str, Any], requested: str = "") -> dict[str, Any]:
    """Select an unambiguous endpoint identity without guessing among multiple models."""
    candidates: list[str] = []
    for entry in payload.get("data") or payload.get("models") or []:
        value = (
            entry.get("id") or entry.get("name") or entry.get("model") if isinstance(entry, dict) else entry
        )
        if isinstance(value, str) and value.strip() and value.strip() not in candidates:
            candidates.append(value.strip())
    selected = requested if requested in candidates else candidates[0] if len(candidates) == 1 else ""
    if requested and requested not in candidates:
        selected = ""
    name = selected.replace("\\", "/").rsplit("/", 1)[-1]
    for extension in (".gguf", ".safetensors", ".onnx", ".bin", ".pt", ".pth"):
        if name.lower().endswith(extension):
            name = name[: -len(extension)]
            break
    return {
        "raw_id": selected or None,
        "display_name": name or None,
        "source": "endpoint_models",
        "status": "resolved" if selected else "unavailable",
        "reason": None
        if selected
        else "requested_model_missing"
        if requested
        else "ambiguous_or_empty_models",
        "candidates": candidates,
    }

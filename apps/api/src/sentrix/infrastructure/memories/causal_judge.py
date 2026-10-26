"""OpenAI 兼容文本端点上的 rules_vlm 因果判定适配器。"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

import httpx

from sentrix.domain.memories.causality import CausalParseError, CausalVerdict
from sentrix.infrastructure.assistant.model_endpoint import ServedModel

log = logging.getLogger(__name__)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class CausalInferenceError(RuntimeError):
    """模型故障、无效 JSON 或缺行；上层须报告图构建降级。"""


class OpenAICausalJudge:
    """只做传输和结构校验；业务提示词和候选筛选不在适配器里。"""

    def __init__(self, base_url: str, served_model: ServedModel, *, timeout_s: float) -> None:
        self._base_url = base_url.rstrip("/")
        self._served_model = served_model
        self._timeout_s = timeout_s

    def judge(self, prompt: str, *, expected_count: int) -> Sequence[CausalVerdict]:
        if expected_count < 1:
            return ()
        model = self._served_model.current()
        try:
            response = httpx.post(
                f"{self._base_url}/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "stream": False,
                    "max_tokens": 768,
                },
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("content is not text")
            try:
                return self._parse(content, expected_count)
            except ValueError as error:
                log.warning("因果判定 JSON 无效 endpoint=%s model=%s: %s", self._base_url, model, error)
                raise CausalParseError(str(error)) from error
        except CausalParseError:
            raise
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as error:
            log.warning("因果判定模型失败 endpoint=%s model=%s: %s", self._base_url, model, error)
            raise CausalInferenceError(
                f"causal judge failed at {self._base_url}: {type(error).__name__}"
            ) from error

    @staticmethod
    def _parse(content: str, expected_count: int) -> tuple[CausalVerdict, ...]:
        raw = _THINK.sub("", content).strip()
        fence = _FENCE.search(raw)
        if fence:
            raw = fence.group(1).strip()
        document = json.loads(raw)
        rows = document.get("results") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            raise ValueError("causal response has no results array")
        verdicts: dict[int, CausalVerdict] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("causal result is not an object")
            pair_id = row.get("pair_id")
            if isinstance(pair_id, bool) or not isinstance(pair_id, int) or not 0 <= pair_id < expected_count:
                raise ValueError("invalid causal pair_id")
            if pair_id in verdicts or not isinstance(row.get("has_causal"), bool):
                raise ValueError("duplicate pair_id or invalid has_causal")
            edge_type = row.get("edge_type")
            if edge_type not in {"LEADS_TO", "ENABLES", "NONE"}:
                raise ValueError("invalid causal edge_type")
            raw_confidence = row.get("confidence")
            if not isinstance(raw_confidence, (int, float)) or isinstance(raw_confidence, bool):
                raise ValueError("invalid causal confidence")
            confidence = float(raw_confidence)
            if not 0.0 <= confidence <= 1.0:
                raise ValueError("invalid causal confidence")
            verdicts[pair_id] = CausalVerdict(
                pair_id=pair_id,
                has_causal=row["has_causal"],
                edge_type=edge_type,
                confidence=confidence,
                reason=str(row.get("reason") or "")[:80],
            )
        if len(verdicts) != expected_count:
            raise ValueError(f"causal response missing {expected_count - len(verdicts)} rows")
        return tuple(verdicts[index] for index in range(expected_count))

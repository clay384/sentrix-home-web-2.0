"""PhotoBench 的领域错误：一个稳定的错误码，外加它与 HTTP 状态的对应。

**它为什么住在 domain。** 与 ``apps/api/src/sentrix/domain/errors.py`` 是同一条口径：
api 可以 import domain 与 application，而 application **不许** import api
（``photobench-application-purity``）。「错误带码」要在两边同时成立，基类就只能落在
两者都看得见的那一层。

**它保的是什么行为。** 1.0 的 ``OrchestratorHandler`` 用两个 except 兜住所有失败：
``KeyError`` → 404，其余 ``Exception`` → 400，响应体固定 ``{"error": str(e)}``。
前端按这个形状读错误，而本轮**前端一行不动**，所以状态码与响应体都要原样保留。
2.0 只是把「哪个失败是 404」从路由的 except 顺序变成领域层的可断言事实。
"""

from __future__ import annotations


class DomainError(Exception):
    """带错误码与 HTTP 状态的领域错误基类。"""

    code = "domain_error"
    status_code = 400

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)
        self.message = message or self.code


class NotFoundError(DomainError):
    """1.0 用 ``KeyError`` 表达的东西：run / item 不存在。"""

    code = "not_found"
    status_code = 404


class InvalidRequestError(DomainError):
    """1.0 用 ``ValueError`` 表达的东西：参数不合法。"""

    code = "invalid_request"
    status_code = 400


class RequestOutcomeUnknownError(RuntimeError):
    """A non-idempotent request may have executed; automatic replay is forbidden."""

    def __init__(self, *, method: str, cause_type: str, attempts: int) -> None:
        self.facts = {
            "code": "request_outcome_unknown",
            "method": method,
            "cause_type": cause_type,
            "attempts": attempts,
            "automatic_replay": False,
        }
        super().__init__(f"{method} outcome unknown after {cause_type}; request was not replayed")


class SharedJudgeGateError(RuntimeError):
    """A shared Judge dispatch gate failed closed; never retry as a provider call."""

    def __init__(self, code: str) -> None:
        self.facts = {"code": code, "automatic_replay": False, "source": "shared_judge_gate"}
        super().__init__("shared_judge_gate:" + code)

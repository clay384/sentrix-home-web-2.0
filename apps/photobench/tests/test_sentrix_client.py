"""``HttpxSentrixClient`` 的用例：**2.0 的端点形状**。

这一份钉的全是「1.0 与 2.0 不一样的地方」，因为那就是本切片最容易写错、也最容易
被一个「跑得通」的用例放过的东西：

- 全局前缀 ``/api`` → ``/api/v1``；
- 种身份是**单人 multipart**，``aliases`` 是**一个分隔字符串**而不是数组；
- 导入收的是**服务端目录**；
- 资产表要**拆信封**（2.0 是 ``{"items": [...]}``，1.0 是 ``{"assets": [...]}``）；
- 问答是**两段式**，轮询路径是复数 ``/turns/{id}``。

用 ``fakes_read.FakeJsonHttpClient``：它按 URL 子串作答，所以每条用例都能顺手断言
「打的是哪个 URL」——URL 拼错时它会以「没预设这个 URL」的形式红，而不是静默走到
另一条分支上。
"""

from __future__ import annotations

import pytest
from fakes_read import FakeJsonHttpClient

from photobench.infrastructure.sentrix.client import HttpxSentrixClient

BASE = "http://sentrix:9598"


def _client(responses: dict) -> tuple[HttpxSentrixClient, FakeJsonHttpClient]:
    http = FakeJsonHttpClient(responses)
    return HttpxSentrixClient(http, lambda: BASE), http


def test_every_call_goes_through_the_v1_prefix():
    """1.0 是 ``/api``；2.0 是 ``/api/v1``。**前缀写在适配器里只有一处**，
    所以这条断言一旦红，说明有人在别处又拼了一个地址。"""
    client, http = _client({"": {"ok": True}})
    client.list_assets(scope_id="s1", limit=5)
    client.start_conversation(scope_id="s1")
    client.import_album(scope_id="s1", directory="/d", limit=1, timeout=9)
    client.get_turn(turn_id="t1")
    assert all(url.startswith(f"{BASE}/api/v1/") for url, *_ in http.calls)


def test_the_base_url_is_read_at_call_time_not_at_construction():
    """1.0 的 ``DEFAULT_SENTRIX_URL`` 是模块级全局量，``POST /api/config`` 就地改它，
    而主链是在**跑起来之后**读的。传当前值会让「改回配置」之后仍然打旧地址。"""
    http = FakeJsonHttpClient({"": {"items": []}})
    current = {"url": BASE}
    client = HttpxSentrixClient(http, lambda: current["url"])
    client.list_assets(scope_id="s1", limit=1)
    current["url"] = "http://other:9999"
    client.list_assets(scope_id="s1", limit=1)
    assert http.calls[0][0].startswith(BASE)
    assert http.calls[1][0].startswith("http://other:9999")


def test_list_assets_unwraps_the_items_envelope_and_drops_non_objects():
    """2.0 与 1.0 的信封键名不同（``items`` vs ``assets``），拆封是适配器的活。"""
    client, http = _client({"assets": {"items": [{"id": "a1"}, "坏行"]}})
    assert client.list_assets(scope_id="s1", limit=7) == [{"id": "a1"}]
    assert "scope_id=s1" in http.calls[0][0]
    assert "limit=7" in http.calls[0][0]


def test_list_assets_quotes_the_scope_id():
    """``quote`` **默认把 ``/`` 当安全字符**，于是 ``scope_id="a/b c"`` 会拼成
    ``?scope_id=a/b%20c``。查询串里斜杠合法、所以这条断言在只看行为时是不是红的
    取决于实现——真正的后果在路径上（``/assistant/turns/a/b`` 把路径切成两段）。
    这条用例钉的是**编码方式唯一**：凡是从调用方来的 id，一律转义。"""
    client, http = _client({"assets": {"items": []}})
    client.list_assets(scope_id="a/b c", limit=1)
    assert "scope_id=a%2Fb%20c" in http.calls[0][0]


def test_seed_person_is_a_single_person_multipart_with_a_joined_alias_string():
    """2.0 只有 ``POST /people/seed``（单人），``aliases`` 是**顿号或逗号分隔的一个
    字符串**（``identity.py:895`` 的 ``_split_terms``）。发成 JSON 数组会被 FastAPI
    当成缺字段、发成逗号分隔在别名自带逗号时会被切错。"""
    client, http = _client({"people/seed": {"entity_id": "e1"}})
    result = client.seed_person(
        scope_id="s1",
        name="明明",
        family_role="大宝",
        aliases=["小明", "明明"],
        file_name="1.jpg",
        content=b"J",
    )
    assert result == {"entity_id": "e1"}
    url, fields, files = http.multipart_calls[0]
    assert url == f"{BASE}/api/v1/people/seed"
    assert fields == {"name": "明明", "family_role": "大宝", "aliases": "小明、明明", "scope_id": "s1"}
    assert files == [("files", "1.jpg", b"J")]


def test_import_album_sends_a_server_side_directory():
    client, http = _client({"imports": {"imported": 3}})
    client.import_album(scope_id="s1", directory="/srv/albums/a", limit=14, timeout=99)
    url, payload, method, timeout = http.calls[0]
    assert url == f"{BASE}/api/v1/imports"
    assert method == "POST"
    assert payload == {"scope_id": "s1", "directory": "/srv/albums/a", "limit": 14}
    assert timeout == 99


def test_ask_is_two_calls_and_the_turn_poll_uses_the_plural_path():
    """1.0 一次 ``POST /assistant/turn`` 拿到答案；2.0 先建会话再发起回合，
    而轮询路径是**复数** ``/assistant/turns/{id}``（1.0 是单数）。

    这条断言同时钉住「发起回合的请求体里**没有** scope」——那个字段属于会话，
    发一个被路由忽略的字段只会让下一个人以为它有用。
    """
    client, http = _client(
        {
            "assistant/conversations": {"id": "conv_1"},
            "turns": {"turn_id": "turn_1", "status": "running"},
        }
    )
    client.start_conversation(scope_id="s1", title="q1")
    client.start_turn(conversation_id="conv_1", message="海边有谁？")
    client.get_turn(turn_id="turn_1")
    conversation_call, turn_call, poll_call = http.calls
    assert conversation_call[0] == f"{BASE}/api/v1/assistant/conversations"
    assert conversation_call[1] == {"scope_id": "s1", "title": "q1"}
    assert turn_call[0] == f"{BASE}/api/v1/assistant/conversations/conv_1/turns"
    assert turn_call[1] == {"message": "海边有谁？"}
    assert poll_call[0] == f"{BASE}/api/v1/assistant/turns/turn_1"
    assert poll_call[2] == "GET"


def test_relationship_batch_sends_the_explicit_name_mapping():
    """``entity_by_name`` 优先于 2.0 内部的名字索引——「我」这类代称在库里重名时，
    不给映射就会被解析到别人身上。"""
    client, http = _client({"relationships/batch": {"imported": 1}})
    client.import_relationships(
        scope_id="s1",
        relationships=[{"subject": "我", "predicate": "妻子", "object": "芳芳"}],
        entity_by_name={"我": "e1", "芳芳": "e2"},
    )
    url, payload, method, _timeout = http.calls[0]
    assert url == f"{BASE}/api/v1/relationships/batch"
    assert method == "POST"
    assert payload == {
        "scope_id": "s1",
        "relationships": [{"subject": "我", "predicate": "妻子", "object": "芳芳"}],
        "entity_by_name": {"我": "e1", "芳芳": "e2"},
    }


def test_a_failure_propagates_instead_of_being_swallowed():
    """失败必须抛。吞掉之后「主链跑完了」与「主链什么都没做」在 run.json 里一样。"""
    http = FakeJsonHttpClient({"items": {"items": []}})
    http.fail_with = RuntimeError("HTTP 500 boom")
    client = HttpxSentrixClient(http, lambda: BASE)
    with pytest.raises(RuntimeError, match="boom"):
        client.list_assets(scope_id="s1", limit=1)

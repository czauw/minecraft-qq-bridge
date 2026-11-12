"""路由：/list 文本、双向格式、去重、回环抑制与分片。"""

from __future__ import annotations

import asyncio

import pytest

from helpers import make_config, server_entry
from mcqq_bridge.events import MCEvent, QQMessage
from mcqq_bridge.manager import ServerQueryResult
from mcqq_bridge.qq_official import SendResult
from mcqq_bridge.router import BridgeRouter, format_list, format_mc_event, split_message


class FakeQQ:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None, bool]] = []

    async def send_text(self, text: str, *, reply_to: str | None = None, proactive: bool = False):
        self.sent.append((text, reply_to, proactive))
        return SendResult(accepted=True, delivered=True, message_id="sent-1")


class FakeManager:
    def __init__(self, results: list[ServerQueryResult] | None = None) -> None:
        self.results = results or []
        self.broadcasts: list[tuple[str, list[str] | None]] = []

    async def query(self, names=None):
        if names is None:
            return list(self.results)
        return [result for result in self.results if result.name in names]

    async def broadcast(self, text: str, names=None):
        self.broadcasts.append((text, names))
        return {result.name: True for result in self.results if result.enabled and result.status == "online"}

    def online_targets(self, names=None):
        return [
            result.name
            for result in self.results
            if result.enabled and result.connected and result.status == "online"
        ]


def build_router(results=None, **config_overrides) -> tuple[BridgeRouter, FakeQQ, FakeManager]:
    config = make_config(**config_overrides)
    manager = FakeManager(results)
    qq = FakeQQ()
    return BridgeRouter(config, manager, qq), qq, manager


def qq_message(text: str, message_id: str = "m1", nickname: str = "小明", group: str = "GROUP_OPENID_1"):
    return QQMessage(
        group_openid=group,
        message_id=message_id,
        member_openid="member-1",
        nickname=nickname,
        text=text,
    )


ONLINE_TWO = ServerQueryResult(
    name="主服", enabled=True, connected=True, status="online", players=["A", "B"], source="rcon"
)
ONLINE_ZERO = ServerQueryResult(
    name="生存服", enabled=True, connected=True, status="online", players=[], source="rcon"
)


def test_format_list_blocks():
    results = [
        ONLINE_TWO,
        ONLINE_ZERO,
        ServerQueryResult(name="创造服", enabled=False, connected=False, status="disabled"),
        ServerQueryResult(name="旧服", enabled=True, connected=False, status="offline"),
    ]
    # 禁用服务器不出现（既不查询也不展示）
    assert format_list(results) == (
        "主服 2人在线：\nA\nB\n\n生存服 0人在线\n\n旧服 离线"
    )


def test_format_list_all_disabled_is_empty():
    results = [ServerQueryResult(name="创造服", enabled=False, connected=False, status="disabled")]
    assert format_list(results) == ""


async def test_list_all_uses_single_reply_order_and_no_mc_broadcast():
    results = [ONLINE_TWO, ONLINE_ZERO]
    router, qq, manager = build_router(results)
    await router.on_qq_message(qq_message("/list"))
    assert manager.broadcasts == []
    assert len(qq.sent) == 1
    text, reply_to, proactive = qq.sent[0]
    assert text == "主服 2人在线：\nA\nB\n\n生存服 0人在线"
    assert reply_to == "m1"
    assert proactive is False


async def test_list_single_server_and_unknown_name():
    router, qq, manager = build_router([ONLINE_TWO])
    await router.on_qq_message(qq_message("/list 主服", message_id="m2"))
    assert qq.sent[-1][0] == "主服 2人在线：\nA\nB"

    await router.on_qq_message(qq_message("/list 不存在", message_id="m3"))
    assert qq.sent[-1][0] == "未找到服务器：不存在"
    assert manager.broadcasts == []


async def test_list_disabled_server_is_not_found():
    """禁用服务器对 /list 不可见：按名字查也不返回它的信息。"""
    servers = [server_entry("主服", 18080), server_entry("创造服", 18081, enabled=False)]
    router, qq, manager = build_router([ONLINE_TWO], servers=servers)
    await router.on_qq_message(qq_message("/list 创造服", message_id="m7"))
    assert qq.sent[-1][0] == "未找到服务器：创造服"
    assert manager.broadcasts == []


async def test_list_without_enabled_servers_reports_empty():
    servers = [server_entry("创造服", 18081, enabled=False)]
    router, qq, manager = build_router([], servers=servers)
    await router.on_qq_message(qq_message("/list", message_id="m8"))
    assert qq.sent[-1][0] == "没有启用的服务器"
    assert manager.broadcasts == []


async def test_listing_is_not_list_and_never_forwarded():
    """/listing 不是 /list，但同样以 / 开头 → 一律不转发，也不回复。"""
    router, qq, manager = build_router([ONLINE_TWO])
    await router.on_qq_message(qq_message("/listing hello"))
    assert manager.broadcasts == []
    assert qq.sent == []


async def test_any_slash_chat_is_never_forwarded():
    router, qq, manager = build_router([ONLINE_TWO])
    for index, text in enumerate(("/help", "/tp A B", "/", "/listx 主服")):
        await router.on_qq_message(qq_message(text, message_id=f"slash-{index}"))
    assert manager.broadcasts == []
    assert qq.sent == []


async def test_at_prefixed_list_replies_with_player_list():
    """全量模式 @机器人 时 content 形如 ``<@member_openid> /list``。"""
    router, qq, manager = build_router([ONLINE_TWO, ONLINE_ZERO])
    await router.on_qq_message(qq_message("<@8A2B3C> /list"))
    assert manager.broadcasts == []
    assert len(qq.sent) == 1
    assert qq.sent[0][0] == "主服 2人在线：\nA\nB\n\n生存服 0人在线"
    assert qq.sent[0][1] == "m1"


async def test_at_prefixed_list_with_argument_and_all_mention():
    router, qq, manager = build_router([ONLINE_TWO])
    await router.on_qq_message(qq_message("<@all> <@8A2B3C> /list 主服", message_id="m4"))
    assert qq.sent[-1][0] == "主服 2人在线：\nA\nB"
    assert manager.broadcasts == []


async def test_at_prefixed_slash_chat_is_suppressed_but_at_text_is_forwarded():
    router, qq, manager = build_router([ONLINE_TWO])
    await router.on_qq_message(qq_message("<@8A2B3C> /help", message_id="m5"))
    assert manager.broadcasts == []
    assert qq.sent == []

    await router.on_qq_message(qq_message("<@8A2B3C> 你好", message_id="m6"))
    # 转发时剥掉艾特标签，不把 <@openid> 带进 MC
    assert manager.broadcasts == [("小明：你好", None)]


async def test_other_group_ignored():
    router, qq, manager = build_router([ONLINE_TWO])
    await router.on_qq_message(qq_message("你好", group="OTHER_GROUP"))
    assert manager.broadcasts == []
    assert qq.sent == []


async def test_duplicate_qq_event_is_deduplicated():
    router, _, manager = build_router([ONLINE_TWO])
    message = qq_message("你好")
    await router.on_qq_message(message)
    await router.on_qq_message(message)
    assert manager.broadcasts == [("小明：你好", None)]


async def test_qq_to_mc_disabled():
    router, _, manager = build_router([ONLINE_TWO], relay={"qq_to_mc_enabled": False})
    await router.on_qq_message(qq_message("你好"))
    assert manager.broadcasts == []


def test_format_mc_events():
    relay = make_config().relay
    assert format_mc_event(
        MCEvent(server="主服", type="chat", player="Alice", text="hi", event_id="1"), relay
    ) == "[主服] [Alice] ： hi"
    assert format_mc_event(
        MCEvent(server="主服", type="join", player="Alice", event_id="2"), relay
    ) == "[主服] [Alice] ： 进入了服务器"
    assert format_mc_event(
        MCEvent(server="主服", type="leave", player="Alice", event_id="3"), relay
    ) == "[主服] [Alice] ： 离开了服务器"
    assert format_mc_event(
        MCEvent(server="主服", type="death", player="Alice", text="was slain by Zombie", event_id="4"),
        relay,
    ) == "[主服] [Alice] ： was slain by Zombie"


async def test_mc_event_sent_as_proactive_and_deduplicated():
    router, qq, _ = build_router([ONLINE_TWO])
    event = MCEvent(server="主服", type="chat", player="Alice", text="hi", event_id="e1")
    await router.on_mc_event(event)
    await router.on_mc_event(event)
    assert qq.sent == [("[主服] [Alice] ： hi", None, True)]


async def test_mc_event_disabled_type_skipped():
    router, qq, _ = build_router([ONLINE_TWO], relay={"mc_to_qq": {"chat": False}})
    await router.on_mc_event(MCEvent(server="主服", type="chat", player="A", text="hi", event_id="e"))
    assert qq.sent == []


async def test_loop_guard_suppresses_returning_broadcast():
    router, qq, manager = build_router([ONLINE_TWO])
    await router.on_qq_message(qq_message("你好"))
    assert manager.broadcasts == [("小明：你好", None)]
    # 服务端把广播回显成一条玩家聊天事件（同文、同服）
    await router.on_mc_event(
        MCEvent(server="主服", type="chat", player="小明", text="小明：你好", event_id="echo-1")
    )
    assert qq.sent == []
    # 其它服务器的正常聊天不受影响
    await router.on_mc_event(
        MCEvent(server="生存服", type="chat", player="小明", text="小明：你好", event_id="echo-2")
    )
    assert qq.sent and qq.sent[0][0] == "[生存服] [小明] ： 小明：你好"


def test_split_message_short_and_long():
    chunks, truncated = split_message("a\nb", max_chars=100, max_parts=5)
    assert chunks == ["a\nb"] and truncated is False

    text = "\n".join(f"player-{index}" for index in range(200))
    chunks, truncated = split_message(text, max_chars=100, max_parts=5)
    assert len(chunks) == 5
    assert truncated is True
    assert chunks[-1].endswith("（列表过长，已截断）")
    assert all(len(chunk) <= 100 + len("（列表过长，已截断）") + 1 for chunk in chunks)


def test_split_message_handles_single_long_line():
    chunks, truncated = split_message("x" * 250, max_chars=100, max_parts=5)
    assert len(chunks) == 3
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert truncated is False


# --- M3：广播前按目标登记回环防护 ----------------------------------------------

ONLINE_THIRD = ServerQueryResult(
    name="生存服", enabled=True, connected=True, status="online", players=["C"], source="rcon"
)


class DelayedAckManager(FakeManager):
    """broadcast 迟迟不返回 ACK，用于验证「ACK 之前」的回显已被抑制。"""

    def __init__(self, results, ack: asyncio.Event, outcome: dict[str, bool] | None = None) -> None:
        super().__init__(results)
        self.ack = ack
        self.outcome = outcome

    async def broadcast(self, text, names=None):
        self.broadcasts.append((text, names))
        await self.ack.wait()
        if self.outcome is not None:
            return dict(self.outcome)
        return {result.name: True for result in self.results if result.enabled}


def build_router_with(manager) -> tuple[BridgeRouter, FakeQQ]:
    config = make_config()
    qq = FakeQQ()
    return BridgeRouter(config, manager, qq), qq


async def test_echo_before_ack_is_suppressed():
    ack = asyncio.Event()
    manager = DelayedAckManager([ONLINE_TWO], ack)
    router, qq = build_router_with(manager)
    task = asyncio.create_task(router.on_qq_message(qq_message("你好")))
    for _ in range(100):
        if manager.broadcasts:
            break
        await asyncio.sleep(0.01)
    # ACK 还没返回，服务端已经把 broadcast 回显成玩家聊天事件
    await router.on_mc_event(
        MCEvent(server="主服", type="chat", player="小明", text="小明：你好", event_id="echo-early")
    )
    assert qq.sent == []
    ack.set()
    await asyncio.wait_for(task, timeout=2)
    assert manager.broadcasts == [("小明：你好", None)]


async def test_slow_server_does_not_delay_guard_for_other_servers():
    ack = asyncio.Event()
    manager = DelayedAckManager([ONLINE_TWO, ONLINE_THIRD], ack)
    router, qq = build_router_with(manager)
    task = asyncio.create_task(router.on_qq_message(qq_message("你好")))
    for _ in range(100):
        if manager.broadcasts:
            break
        await asyncio.sleep(0.01)
    # 多服广播里任一台慢 ACK，都不会让另一台的回显漏过去
    await router.on_mc_event(
        MCEvent(server="生存服", type="chat", player="小明", text="小明：你好", event_id="echo-slow")
    )
    assert qq.sent == []
    ack.set()
    await asyncio.wait_for(task, timeout=2)


async def test_failed_broadcast_releases_loop_guard():
    ack = asyncio.Event()
    ack.set()
    manager = DelayedAckManager([ONLINE_TWO], ack, outcome={"主服": False})
    router, qq = build_router_with(manager)
    await router.on_qq_message(qq_message("你好"))
    assert "mc:主服:小明：你好" not in router._guard_refs
    # 广播失败说明玩家没看到这句话：之后真正的同名聊天必须照常转发
    await router.on_mc_event(
        MCEvent(server="主服", type="chat", player="Alice", text="小明：你好", event_id="real-1")
    )
    assert qq.sent == [("[主服] [Alice] ： 小明：你好", None, True)]


async def test_broadcast_exception_releases_loop_guard():
    class RaisingManager(FakeManager):
        async def broadcast(self, text, names=None):
            raise RuntimeError("boom")

    router, qq = build_router_with(RaisingManager([ONLINE_TWO]))
    with pytest.raises(RuntimeError):
        await router.on_qq_message(qq_message("你好"))
    assert router._guard_refs == {}
    assert len(router._loop_guard) == 0


async def test_concurrent_identical_broadcasts_keep_guard_until_last_release():
    ack = asyncio.Event()
    manager = DelayedAckManager([ONLINE_TWO], ack, outcome={"主服": False})
    router, qq = build_router_with(manager)
    first = asyncio.create_task(router.on_qq_message(qq_message("你好", message_id="m-1")))
    second = asyncio.create_task(router.on_qq_message(qq_message("你好", message_id="m-2")))
    for _ in range(100):
        if len(manager.broadcasts) >= 2:
            break
        await asyncio.sleep(0.01)
    assert router._guard_refs.get("mc:主服:小明：你好") == 2
    ack.set()
    await asyncio.gather(first, second)
    assert "mc:主服:小明：你好" not in router._guard_refs


def test_split_message_truncation_never_exceeds_max_chars():
    text = "\n".join(f"player-{index}" for index in range(200))
    chunks, truncated = split_message(text, max_chars=50, max_parts=2)
    assert truncated is True
    assert len(chunks) == 2
    assert all(len(chunk) <= 50 for chunk in chunks), [len(chunk) for chunk in chunks]
    assert chunks[-1].endswith("（列表过长，已截断）")

    tiny, _ = split_message(text, max_chars=5, max_parts=1)
    assert all(len(chunk) <= 5 for chunk in tiny)


def test_split_message_exact_boundary():
    text = "\n".join(f"p{index}" for index in range(10))  # 20 字符含换行
    chunks, truncated = split_message(text, max_chars=20, max_parts=5)
    assert truncated is False
    assert all(len(chunk) <= 20 for chunk in chunks)


async def test_successful_broadcast_does_not_leak_refcounts():
    router, qq, manager = build_router([ONLINE_TWO])
    await router.on_qq_message(qq_message("你好"))
    # 成功的广播把「在途计数」释放掉，只留 TTL 指纹（避免计数表无限增长）
    assert router._guard_refs == {}
    assert router._loop_guard.contains("mc:主服:小明：你好")
    await router.on_mc_event(
        MCEvent(server="主服", type="chat", player="小明", text="小明：你好", event_id="echo")
    )
    assert qq.sent == []

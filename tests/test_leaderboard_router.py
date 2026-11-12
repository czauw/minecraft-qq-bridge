"""命令路由：/death、/playtime 语义 + 既有行为回归（验收 1-4、13、14）。"""

from __future__ import annotations

import pytest

from leaderboard_helpers import (
    UUID_MAIN,
    UUID_MAIN_ALT,
    UUID_OTHER,
    FakeClient,
    FakeClock,
    make_instance,
    start_service,
    write_stats,
    write_usercache,
)
from leaderboard_helpers import FakeManager as BaseFakeManager
from mcqq_bridge.config import AppConfig
from mcqq_bridge.events import MCEvent, QQMessage
from mcqq_bridge.leaderboard_models import Leaderboard, ServerBoard
from mcqq_bridge.manager import ServerQueryResult
from mcqq_bridge.qq_official import SendResult
from mcqq_bridge.router import BridgeRouter

T0 = 1_760_000_000.0


class FakeQQ:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None, bool]] = []

    async def send_text(self, text: str, *, reply_to: str | None = None, proactive: bool = False):
        self.sent.append((text, reply_to, proactive))
        return SendResult(accepted=True, delivered=True, message_id="out-1")


class FakeManager(BaseFakeManager):
    """在排行榜假管理器上补 /list 与广播能力。"""

    def __init__(self, clients, list_results):
        super().__init__(clients)
        self.list_results = list_results
        self.broadcasts: list[tuple[str, list[str] | None]] = []

    async def query(self, names=None):
        if names is None:
            return list(self.list_results)
        return [item for item in self.list_results if item.name in names]

    async def broadcast(self, text: str, names=None):
        self.broadcasts.append((text, names))
        return {item.name: True for item in self.list_results if item.enabled and item.status == "online"}

    def online_targets(self, names=None):
        return [
            item.name
            for item in self.list_results
            if item.enabled and item.connected and item.status == "online"
        ]


def qq_message(text: str, message_id: str = "m1", group: str = "GROUP_OPENID_1") -> QQMessage:
    return QQMessage(
        group_openid=group,
        message_id=message_id,
        member_openid="member-1",
        nickname="小明",
        text=text,
    )


async def build(tmp_path, *, passive_allowed: bool = True):
    root_a, stats_a = make_instance(tmp_path, "a")
    root_b, stats_b = make_instance(tmp_path, "b")
    write_usercache(
        root_a,
        [
            ("MUEZORE", UUID_MAIN, "2026-11-02 00:00:00 +0000"),
            ("MUEZORE", UUID_MAIN_ALT, "2026-11-01 00:00:00 +0000"),
            ("yui_Thefish", UUID_OTHER, "2026-11-01 00:00:00 +0000"),
        ],
    )
    write_usercache(root_b, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    # 齿轮盛宴：同服同名双 UUID（40+42=82 次），另有 0 值玩家
    write_stats(stats_a, UUID_MAIN, ticks=1_374_540, deaths=40, mtime=T0 - 100)
    write_stats(stats_a, UUID_MAIN_ALT, ticks=0, deaths=42, mtime=T0 - 100)
    write_stats(stats_a, UUID_OTHER, ticks=7, deaths=0, mtime=T0 - 100)
    write_stats(stats_b, UUID_MAIN, ticks=5231, deaths=115, mtime=T0 - 100)

    from helpers import make_config, server_entry

    config = make_config(
        servers=[
            server_entry("齿轮盛宴", 19000, stats_dir=str(stats_a)),
            server_entry("亡者世界", 19001, stats_dir=str(stats_b)),
            server_entry("资源服", 19002, enabled=False, stats_dir=str(stats_a)),
        ],
        stats={"db_path": str(tmp_path / "stats.db")},
        bot={"proactive_enabled": passive_allowed},
    )
    clients = [FakeClient("齿轮盛宴", []), FakeClient("亡者世界", [])]
    list_results = [
        ServerQueryResult(name="齿轮盛宴", enabled=True, connected=True, status="online", players=["A"]),
        ServerQueryResult(name="亡者世界", enabled=True, connected=True, status="online", players=[]),
        ServerQueryResult(name="资源服", enabled=False, connected=False, status="disabled"),
    ]
    manager = FakeManager(clients, list_results)
    qq = FakeQQ()
    service = await start_service(config, manager=manager, clock=FakeClock(T0))
    router = BridgeRouter(config, manager, qq, leaderboards=service)
    return router, qq, manager, service, config


async def test_death_all_enabled_players_including_zero(tmp_path):
    router, qq, manager, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/death", "M-DEATH"))
        assert len(qq.sent) == 1
        text = qq.sent[0][0]
        # 无参 = 所有启用服务器 × 每服全部玩家（含 0 值），不是 Top N
        assert text == (
            "齿轮盛宴 死亡榜：\n"
            "1. MUEZORE  82 次\n"
            "2. yui_Thefish  0 次\n"
            "\n"
            "亡者世界 死亡榜：\n"
            "1. MUEZORE  115 次"
        )
        assert "资源服" not in text  # 禁用服整段不出现
        assert manager.broadcasts == []  # 命令不下发 MC
    finally:
        await service.stop()


async def test_playtime_all_enabled_players_including_zero(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/playtime", "M-PT"))
        text = qq.sent[0][0]
        # 秒级：1374540//20 = 68727 = 19h05m27s；7//20 = 0 → 0s
        assert "1. MUEZORE  19h05m27s" in text
        assert "2. yui_Thefish  0s" in text
        assert "亡者世界 在线时长榜：" in text
        assert "1. MUEZORE  04m21s" in text  # 5231//20 = 261 秒
    finally:
        await service.stop()


async def test_player_argument_is_unknown_server(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/death MUEZORE", "M-P"))
        assert qq.sent == [("未找到服务器：MUEZORE", "M-P", False)]
    finally:
        await service.stop()


async def test_disabled_server_hidden_and_not_found(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/death 资源服", "M-D"))
        assert qq.sent == [("未找到服务器：资源服", "M-D", False)]
    finally:
        await service.stop()


async def test_single_server_selection_and_case_insensitivity(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/Death 亡者世界", "M-1"))
        assert qq.sent[-1][0] == "亡者世界 死亡榜：\n1. MUEZORE  115 次"
        await router.on_qq_message(qq_message("/PLAYTIME 亡者世界", "M-2"))
        assert qq.sent[-1][0] == "亡者世界 在线时长榜：\n1. MUEZORE  04m21s"
    finally:
        await service.stop()


async def test_extra_arguments_are_ignored(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/death 亡者世界 5", "M-3"))
        assert qq.sent[-1][0] == "亡者世界 死亡榜：\n1. MUEZORE  115 次"
    finally:
        await service.stop()


async def test_at_prefix_is_stripped_before_matching(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("<@member_openid> /death 亡者世界", "M-4"))
        assert qq.sent[-1][0].startswith("亡者世界 死亡榜：")
    finally:
        await service.stop()


async def test_near_miss_commands_are_not_leaderboards(tmp_path):
    router, qq, manager, service, _ = await build(tmp_path)
    try:
        for text in ("/deathx", "/playtimes", "/death 5 extra"):
            await router.on_qq_message(qq_message(text, f"M-{text}"))
        # 前两个以 / 开头：既不回复也不转发；第三个按「未找到服务器 5」处理
        assert [item[0] for item in qq.sent] == ["未找到服务器：5"]
        assert manager.broadcasts == []
    finally:
        await service.stop()


async def test_replies_are_passive_with_msg_id(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/death 亡者世界", "M-PASSIVE"))
        assert all(reply_to == "M-PASSIVE" and proactive is False for _, reply_to, proactive in qq.sent)
    finally:
        await service.stop()


async def test_passive_reply_works_when_proactive_disabled(tmp_path):
    router, qq, _, service, _ = await build(tmp_path, passive_allowed=False)
    try:
        await router.on_qq_message(qq_message("/death 亡者世界", "M-P2"))
        assert qq.sent and qq.sent[0][1] == "M-P2" and qq.sent[0][2] is False
    finally:
        await service.stop()


async def test_duplicate_message_id_only_replies_once(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/death 亡者世界", "M-DUP"))
        await router.on_qq_message(qq_message("/death 亡者世界", "M-DUP"))
        assert len(qq.sent) == 1
    finally:
        await service.stop()


async def test_non_target_group_is_ignored(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/death", "M-G", group="OTHER_GROUP"))
        assert qq.sent == []
    finally:
        await service.stop()


async def test_list_still_works_and_commands_do_not_regress_it(tmp_path):
    router, qq, manager, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("/list", "M-L"))
        assert qq.sent[-1][0] == "齿轮盛宴 1人在线：\nA\n\n亡者世界 0人在线"
        assert "资源服" not in qq.sent[-1][0]
        await router.on_qq_message(qq_message("/list 资源服", "M-L2"))
        assert qq.sent[-1][0] == "未找到服务器：资源服"
        assert manager.broadcasts == []
    finally:
        await service.stop()


async def test_qq_to_mc_forwarding_and_slash_suppression_still_work(tmp_path):
    router, qq, manager, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("大家好", "M-F"))
        assert manager.broadcasts == [("小明：大家好", None)]
        await router.on_qq_message(qq_message("/unknown", "M-S"))
        assert len(manager.broadcasts) == 1  # 以 / 开头一律不转发
        assert qq.sent == []
    finally:
        await service.stop()


async def test_mc_event_forwarding_and_loop_guard_still_work(tmp_path):
    router, qq, manager, service, _ = await build(tmp_path)
    try:
        await router.on_qq_message(qq_message("你好", "M-B"))
        event = MCEvent(server="齿轮盛宴", type="chat", player="A", text="小明：你好", event_id="ev-1")
        await router.on_mc_event(event)
        # 回环抑制：广播出去的文本从 MC 回来不应再发到 QQ
        assert qq.sent == []

        await router.on_mc_event(
            MCEvent(server="齿轮盛宴", type="chat", player="A", text="别的话", event_id="ev-2")
        )
        assert qq.sent[-1][0] == "[齿轮盛宴] [A] ： 别的话"
        assert qq.sent[-1][2] is True  # MC → QQ 走主动消息
    finally:
        await service.stop()


async def test_unavailable_leaderboard_reports_gracefully(tmp_path):
    router, qq, _, service, _ = await build(tmp_path)
    try:

        async def boom(kind, server=None):
            from mcqq_bridge.leaderboard_models import LeaderboardUnavailable

            raise LeaderboardUnavailable("排行榜数据暂不可用")

        service.get_board = boom  # type: ignore[assignment]
        await router.on_qq_message(qq_message("/death", "M-U"))
        assert qq.sent == [("排行榜数据暂不可用", "M-U", False)]
    finally:
        await service.stop()


async def test_router_without_leaderboard_service_reports_unavailable(tmp_path):
    from helpers import make_config

    config = make_config()
    manager = FakeManager([], [])
    qq = FakeQQ()
    router = BridgeRouter(config, manager, qq)
    await router.on_qq_message(qq_message("/death", "M-N"))
    assert qq.sent == [("排行榜数据暂不可用", "M-N", False)]
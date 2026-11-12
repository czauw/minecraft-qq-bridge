"""在线会话：join/leave、60 秒 RCON 对账、崩溃/断线收敛（验收 2、10）。"""

from __future__ import annotations

import asyncio

import pytest

from leaderboard_helpers import (
    UUID_MAIN,
    UUID_MAIN_ALT,
    UUID_OTHER,
    FakeClient,
    FakeClock,
    FakeManager,
    join_event,
    leave_event,
    make_instance,
    make_lb_config,
    start_service,
    write_stats,
    write_usercache,
)
from mcqq_bridge.events import MCEvent
from mcqq_bridge.leaderboards import MAX_ONLINE_EXTRAPOLATION_SECONDS
from mcqq_bridge.runtime import BridgeRuntime

T0 = 1_760_000_000.0


def prepare(tmp_path, *, players: list[str] | None = None, name: str = "主服"):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(
        root,
        [
            ("MUEZORE", UUID_MAIN, "2026-11-02 00:00:00 +0000"),
            ("MUEZORE", UUID_MAIN_ALT, "2026-11-01 00:00:00 +0000"),
            ("Other", UUID_OTHER, "2026-11-01 00:00:00 +0000"),
        ],
    )
    write_stats(stats_dir, UUID_MAIN, ticks=2000, deaths=1, mtime=T0 - 100)
    config = make_lb_config(tmp_path, [{"name": name, "stats_dir": str(stats_dir)}])
    client = FakeClient(name, players or [])
    return config, FakeManager([client]), client


async def test_duplicate_join_and_leave_are_idempotent(tmp_path):
    config, manager, _ = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        clock.advance(10)
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        clock.advance(10)
        # 重复 join 不能新开会话、也不能把 anchor 挪到第二次 join（否则丢 10 秒）
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 120

        service.offer_event(leave_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        service.offer_event(leave_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        await service.scan_once()
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 100
    finally:
        await service.stop()


async def test_disabled_relay_join_still_tracks_session(tmp_path):
    # 关闭「进服通知」转发也必须照样统计在线时长（统计钩子在 relay 开关之前）
    config, manager, _ = prepare(tmp_path)
    config = config.model_copy(
        update={"relay": config.relay.model_copy(update={"mc_to_qq": config.relay.mc_to_qq.model_copy(update={"join": False})})}
    )
    runtime = BridgeRuntime(config)
    runtime.leaderboards.manager = manager
    runtime.leaderboards._clock = FakeClock(T0)
    runtime.leaderboards._loops = False
    await runtime.leaderboards.start()
    try:
        await runtime._on_mc_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await runtime.leaderboards._drain_events()
        board = await runtime.leaderboards.get_board("playtime")
        assert board.servers[0].rows[0].online is True
    finally:
        await runtime.leaderboards.stop()


async def test_rcon_failure_does_not_close_sessions_or_other_servers(tmp_path):
    root_a, stats_a = make_instance(tmp_path, "a")
    root_b, stats_b = make_instance(tmp_path, "b")
    for root in (root_a, root_b):
        write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_a, UUID_MAIN, ticks=2000, deaths=1, mtime=T0 - 100)
    write_stats(stats_b, UUID_MAIN, ticks=2000, deaths=1, mtime=T0 - 100)
    config = make_lb_config(
        tmp_path,
        [
            {"name": "齿轮盛宴", "stats_dir": str(stats_a)},
            {"name": "亡者世界", "stats_dir": str(stats_b)},
        ],
    )
    clock = FakeClock(T0)
    client_a = FakeClient("齿轮盛宴", ["MUEZORE"])
    client_b = FakeClient("亡者世界", ["MUEZORE"])
    service = await start_service(config, manager=FakeManager([client_a, client_b]), clock=clock)
    try:
        await service.reconcile_once()
        assert len(service._sessions) == 2

        client_a.fail = True  # 单台 RCON 失败
        clock.advance(30)
        await service.reconcile_once()
        # A 的会话保留（加时按 60 秒证据窗口封顶），B 的会话正常刷新
        assert ("齿轮盛宴", UUID_MAIN) in service._sessions
        assert ("亡者世界", UUID_MAIN) in service._sessions
        assert service._sessions[("亡者世界", UUID_MAIN)].evidence_at == clock.now
    finally:
        await service.stop()


async def test_stalled_rcon_caps_online_extrapolation(tmp_path):
    config, manager, _ = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        manager.client("主服").fail = True

        clock.advance(3600)  # 一小时里对账一直失败
        await service.reconcile_once()
        board = await service.get_board("playtime")
        # 权威 100 秒 + 最多一个对账周期（60 秒），不会无限外推
        assert board.servers[0].rows[0].value == 160
        assert MAX_ONLINE_EXTRAPOLATION_SECONDS == 60
    finally:
        await service.stop()


async def test_crash_sessions_converge_at_next_reconcile(tmp_path):
    config, manager, client = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        clock.advance(45)

        # 服务器崩溃/强杀：没有 player_quit，连接也断了
        client.connected = False
        await service.reconcile_once()
        assert service._sessions == {}

        # 收敛后不再加时（估算秒不回写累计值）
        clock.advance(600)
        board = await service.get_board("playtime")
        assert board.servers[0].rows[0].value == 100
        assert board.servers[0].rows[0].online is False
    finally:
        await service.stop()


async def test_disconnect_callback_closes_sessions_immediately(tmp_path):
    config, manager, client = prepare(tmp_path, players=["MUEZORE"])
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()
        assert service._sessions
        service.on_connection_change("主服", False)
        await asyncio.sleep(0)
        await asyncio.gather(*service._aux_tasks)
        assert service._sessions == {}
    finally:
        await service.stop()


async def test_unresolved_name_does_not_fabricate_uuid(tmp_path):
    config, manager, client = prepare(tmp_path)
    client.players = ["GhostPlayer"]  # usercache 里没有这个人
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()
        assert service._sessions == {}
        board = await service.get_board("playtime")
        assert [row.player for row in board.servers[0].rows] == ["MUEZORE"]
        assert board.servers[0].rows[0].online is False
    finally:
        await service.stop()


async def test_reconcile_does_not_overwrite_concurrent_join(tmp_path):
    config, manager, client = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)

    outer = manager

    class GatedManager(FakeManager):
        def __init__(self) -> None:
            super().__init__([client])
            self.entered = asyncio.Event()
            self.gate = asyncio.Event()

        async def query_rcon(self, names=None):
            self.entered.set()
            await self.gate.wait()
            return await outer.query_rcon(names)

    gated = GatedManager()
    service.manager = gated
    try:
        task = asyncio.create_task(service.reconcile_once())
        await gated.entered.wait()
        # 查询在途时来了一条 join（不在旧快照里）
        service.offer_event(join_event("主服", "Other", UUID_OTHER))
        await service._drain_events()
        assert ("主服", UUID_OTHER) in service._sessions

        gated.gate.set()
        await task
        # 旧快照不得删掉并发新开的会话
        assert ("主服", UUID_OTHER) in service._sessions
    finally:
        await service.stop()


async def test_leave_without_uuid_closes_by_unique_name(tmp_path):
    config, manager, _ = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        service.offer_event(leave_event("主服", "MUEZORE", ""))  # 事件没带 UUID
        await service._drain_events()
        assert service._sessions == {}
    finally:
        await service.stop()


async def test_join_without_any_uuid_evidence_is_ignored(tmp_path):
    config, manager, _ = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        service.offer_event(join_event("主服", "GhostPlayer", ""))
        await service._drain_events()
        assert service._sessions == {}
        # 不生成 name:xxx 之类的假玩家行
        assert (await service.get_board("playtime")).servers[0].total_players == 1
    finally:
        await service.stop()


async def test_death_event_never_increments_deaths(tmp_path):
    config, manager, _ = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        service.offer_event(
            MCEvent(server="主服", type="death", player="MUEZORE", uuid=UUID_MAIN, event_id="d1")
        )
        await service._drain_events()
        # 死亡数只来自权威 stats 文件（1 次），死亡事件不加数
        assert (await service.get_board("deaths")).servers[0].rows[0].value == 1
    finally:
        await service.stop()
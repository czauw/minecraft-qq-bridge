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


async def test_rcon_failure_converges_failed_server_only(tmp_path):
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
        assert service._server_state["齿轮盛宴"].last_verified_at == T0

        client_a.fail = True  # 单台 RCON 失败
        clock.advance(30)
        await service.reconcile_once()
        # A 的会话在本轮内关闭（不是留着等恢复），增量按封顶折进累计值
        assert ("齿轮盛宴", UUID_MAIN) not in service._sessions
        # B 不受影响，证据时刻正常刷新
        assert ("亡者世界", UUID_MAIN) in service._sessions
        assert service._sessions[("亡者世界", UUID_MAIN)].evidence_at == clock.now
        boards = {board.server: board for board in (await service.get_board("playtime")).servers}
        assert boards["齿轮盛宴"].rows[0].value == 130
        assert boards["齿轮盛宴"].rows[0].online is False
        assert boards["亡者世界"].rows[0].value == 130
    finally:
        await service.stop()


async def test_rcon_failure_caps_persisted_ticks_at_last_verified_plus_60(tmp_path):
    """RCON 持续失败：下一轮对账内关闭会话，落盘 ticks 只算到「最后确证在线 + 60 秒」。"""
    config, manager, client = prepare(tmp_path, players=["MUEZORE"])
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()  # 成功名单：会话+T0 作为最后确证时刻
        assert service._server_state["主服"].last_verified_at == T0
        assert (await service.get_board("playtime")).servers[0].rows[0].online is True

        clock.advance(30)
        client.fail = True  # 服务器崩溃 / RCON 挂掉
        clock.advance(3600)  # 一小时后才有下一次对账机会
        await service.reconcile_once()

        assert service._sessions == {}
        board = await service.get_board("playtime")
        # 权威 100 秒 + 封顶 60 秒 = 160（不是 3600 秒，也不是残留会话继续加时）
        assert board.servers[0].rows[0].value == 160
        assert board.servers[0].rows[0].online is False

        # 落盘口径与展示完全一致：库里就是 3200 ticks（160 秒），不只是显示被截断
        assert service._store is not None
        _, stats, sessions = await service._store.load_all()
        assert sessions == []
        assert [(row.server, row.ticks) for row in stats] == [("主服", 3200)]
    finally:
        await service.stop()


async def test_rcon_failure_after_post_verify_join_caps_at_last_verified_plus_60(tmp_path):
    """成功 list 之后才 join 的会话，再遇 RCON 失败也必须严格按 last_verified+60s 封顶。

    边界（reviewer 评论#34）：t=0 成功 list，t=30 join 开新会话，t=120 对账失败。
    封顶只能是 t=0+60=60（不是「证据时刻 30 + 60 = 90」），落盘/展示都不得超过它。
    """
    config, manager, client = prepare(tmp_path)  # 首次对账名单为空：不会有会话
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()
        assert service._server_state["主服"].last_verified_at == T0
        assert service._sessions == {}

        clock.advance(30)
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        session = service._sessions[("主服", UUID_MAIN)]
        assert session.started_at == T0 + 30

        clock.advance(90)  # now = T0 + 120，明显晚于 last_verified + 60
        client.fail = True
        await service.reconcile_once()

        assert service._sessions == {}
        board = await service.get_board("playtime")
        # 权威 100 秒 + 封顶内 30 秒（30→60），而不是证据时刻外推的 60 秒
        assert board.servers[0].rows[0].value == 130
        assert board.servers[0].rows[0].online is False

        assert service._store is not None
        _, stats, sessions = await service._store.load_all()
        assert sessions == []
        assert [(row.server, row.ticks) for row in stats] == [("主服", 2600)]
    finally:
        await service.stop()


async def test_degraded_session_display_and_persist_share_strict_cap(tmp_path):
    """对账失败后新开的会话：展示同样按 last_verified+60s 封顶，下轮关闭时落盘同值。"""
    config, manager, client = prepare(tmp_path)
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()  # last_verified = T0
        client.fail = True
        clock.advance(120)
        await service.reconcile_once()  # 失败轮：degraded=True
        assert service._server_state["主服"].degraded is True

        # 失败期间又来一条 join：会话起点已晚于 last_verified + 60，展示不得再加时
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        clock.advance(600)
        board = await service.get_board("playtime")
        assert board.servers[0].rows[0].value == 100  # 权威 100 秒，加时 0

        # 下一轮失败关闭该会话：落盘同样不加时，展示与库同口径
        await service.reconcile_once()
        assert service._sessions == {}
        assert service._store is not None
        _, stats, _ = await service._store.load_all()
        assert [(row.server, row.ticks) for row in stats] == [("主服", 2000)]
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 100
    finally:
        await service.stop()


async def test_recovered_rcon_does_not_backfill_crashed_time(tmp_path):
    """RCON 恢复后不得把崩溃期间的时间补加到玩家身上。"""
    config, manager, client = prepare(tmp_path, players=["MUEZORE"])
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()
        clock.advance(30)
        client.fail = True
        clock.advance(3600)
        await service.reconcile_once()  # 失败轮：已收敛到 160 秒
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 160

        # 服务器重启完成：RCON 恢复，但玩家已经不在线
        client.fail = False
        client.players = []
        clock.advance(600)
        await service.reconcile_once()
        assert service._sessions == {}
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 160

        # 再连续失败几轮也不会重新加时（没有会话可加，累计值不再变化）
        client.fail = True
        clock.advance(600)
        await service.reconcile_once()
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 160
    finally:
        await service.stop()


async def test_rcon_failure_logs_one_warning_with_server_and_reason(tmp_path, caplog):
    config, manager, client = prepare(tmp_path, players=["MUEZORE"])
    clock = FakeClock(T0)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()
        client.fail = True
        clock.advance(60)
        with caplog.at_level("WARNING", logger="mcqq_bridge.leaderboards"):
            await service.reconcile_once()
        warnings = [record.getMessage() for record in caplog.records if "对账失败" in record.getMessage()]
        assert len(warnings) == 1
        assert "主服" in warnings[0]
        assert "rcon_offline" in warnings[0]
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
        # 封顶后会话立即关闭：库里不再有打开着的会话（不是只截断展示）
        assert service._sessions == {}
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

        # 封顶内的 45 秒已折进累计值（下一轮对账内收敛，误差 ≤ 60 秒）
        clock.advance(600)
        board = await service.get_board("playtime")
        assert board.servers[0].rows[0].value == 145
        assert board.servers[0].rows[0].online is False
        assert service._store is not None
        _, stats, sessions = await service._store.load_all()
        assert sessions == []
        assert [(row.server, row.ticks) for row in stats] == [("主服", 2900)]
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
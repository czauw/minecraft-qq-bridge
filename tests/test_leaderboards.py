"""榜单聚合与在线加时口径（需求 §4/§5，验收 1、2、5、7、8、9、10）。"""

from __future__ import annotations

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
from mcqq_bridge.leaderboard_models import LeaderboardUnavailable
from mcqq_bridge.leaderboards import (
    MAX_ONLINE_EXTRAPOLATION_SECONDS,
    RECONCILE_INTERVAL_SECONDS,
    SCAN_INTERVAL_SECONDS,
)
from mcqq_bridge.stats_scanner import ScanResult, StatsScanner

T0 = 1_760_000_000.0


def test_intervals_are_fixed_at_sixty_seconds() -> None:
    # 需求 §9/§5.3 固定 60 秒；不暴露成配置项，避免把验收要求改掉
    assert SCAN_INTERVAL_SECONDS == 60
    assert RECONCILE_INTERVAL_SECONDS == 60
    assert MAX_ONLINE_EXTRAPOLATION_SECONDS == 60


async def test_same_server_same_name_sums_uuid_values_once(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(
        root,
        [
            ("MUEZORE", UUID_MAIN, "2026-11-02 00:00:00 +0000"),
            ("MUEZORE", UUID_MAIN_ALT, "2026-11-01 00:00:00 +0000"),
        ],
    )
    # 同服同名两个 UUID（离线模式 v3 + 正版 v4）：40 + 42 = 82 次
    write_stats(stats_dir, UUID_MAIN, ticks=600_000, deaths=40, mtime=T0 - 100)
    write_stats(stats_dir, UUID_MAIN_ALT, ticks=700_000, deaths=42, mtime=T0 - 100)
    config = make_lb_config(tmp_path, [{"name": "齿轮盛宴", "stats_dir": str(stats_dir)}])
    clock = FakeClock(T0)
    client = FakeClient("齿轮盛宴", ["MUEZORE"])
    service = await start_service(config, manager=FakeManager([client]), clock=clock)
    try:
        await service.reconcile_once()  # RCON list 补开在线会话（同名归属 usercache 最新 UUID）
        deaths = await service.get_board("deaths")
        assert len(deaths.servers[0].rows) == 1  # 榜上一人一行
        assert deaths.servers[0].rows[0].value == 82
        assert deaths.servers[0].total_players == 1

        playtime = await service.get_board("playtime")
        # 每次会话只计一次：600000//20 + 700000//20 = 30000 + 35000
        assert playtime.servers[0].rows[0].value == 65_000
        assert playtime.servers[0].rows[0].online is True

        clock.advance(10)
        playtime = await service.get_board("playtime")
        # 会话只归属一个 UUID（usercache 最新那条），加时只加一次，不是两份
        assert playtime.servers[0].rows[0].value == 65_010
    finally:
        await service.stop()


async def test_same_name_different_servers_stays_separate(tmp_path):
    root_a, stats_a = make_instance(tmp_path, "a")
    root_b, stats_b = make_instance(tmp_path, "b")
    for root in (root_a, root_b):
        write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_a, UUID_MAIN, ticks=2000, deaths=82)
    write_stats(stats_b, UUID_MAIN, ticks=4000, deaths=115)
    config = make_lb_config(
        tmp_path,
        [
            {"name": "齿轮盛宴", "stats_dir": str(stats_a)},
            {"name": "亡者世界", "stats_dir": str(stats_b)},
        ],
    )
    service = await start_service(config)
    try:
        board = await service.get_board("deaths")
        assert [item.server for item in board.servers] == ["齿轮盛宴", "亡者世界"]
        assert [item.rows[0].value for item in board.servers] == [82, 115]
    finally:
        await service.stop()


async def test_ties_sort_by_unicode_name_and_include_zero(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(
        root,
        [
            ("bravo", UUID_MAIN, "2026-11-01 00:00:00 +0000"),
            ("Alpha", UUID_MAIN_ALT, "2026-11-01 00:00:00 +0000"),
            ("Zero", UUID_OTHER, "2026-11-01 00:00:00 +0000"),
        ],
    )
    write_stats(stats_dir, UUID_MAIN, ticks=20, deaths=5)
    write_stats(stats_dir, UUID_MAIN_ALT, ticks=20, deaths=5)
    write_stats(stats_dir, UUID_OTHER, ticks=0, deaths=0)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        rows = (await service.get_board("deaths")).servers[0].rows
        assert [(row.rank, row.player, row.value) for row in rows] == [
            (1, "Alpha", 5),  # 数值相同按名字 Unicode 升序
            (2, "bravo", 5),
            (3, "Zero", 0),  # 0 值玩家必须在榜上
        ]
    finally:
        await service.stop()


async def test_online_increment_and_save_rebase(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    # 权威值 2000 tick = 100 秒，落盘时刻 = T0-100
    write_stats(stats_dir, UUID_MAIN, ticks=2000, deaths=1, mtime=T0 - 100)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    clock = FakeClock(T0)
    client = FakeClient("主服", ["MUEZORE"])
    service = await start_service(config, manager=FakeManager([client]), clock=clock)
    try:
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 100

        clock.advance(30)
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 130

        # MC 落盘：ticks 增加 30 秒，mtime = 落盘时刻；anchor 必须推进，不能双计
        write_stats(stats_dir, UUID_MAIN, ticks=2000 + 30 * 20, deaths=1, mtime=T0 + 30)
        await service.scan_once()
        assert (await service.get_board("playtime")).servers[0].rows[0].value == 130

        clock.advance(10)
        board = await service.get_board("playtime")
        assert board.servers[0].rows[0].value == 140
        assert board.servers[0].rows[0].display.endswith("（在线中）")
    finally:
        await service.stop()


async def test_leave_stops_the_clock_and_keeps_last_authoritative_value(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_dir, UUID_MAIN, ticks=2000, deaths=1, mtime=T0 - 100)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    clock = FakeClock(T0)
    service = await start_service(config, manager=FakeManager([FakeClient("主服", [])]), clock=clock)
    try:
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        clock.advance(20)
        service.offer_event(leave_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        await service.scan_once()
        clock.advance(500)
        board = await service.get_board("playtime")
        # 退出后不再加时；估算秒不回写累计值（仍是权威的 100 秒）
        assert board.servers[0].rows[0].value == 100
        assert board.servers[0].rows[0].online is False
    finally:
        await service.stop()


async def test_disabled_server_is_invisible_in_board(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_dir, UUID_MAIN, ticks=200, deaths=9)
    config = make_lb_config(
        tmp_path,
        [
            {"name": "齿轮盛宴", "stats_dir": str(stats_dir)},
            # 禁用项的 stats_dir 指向不存在的位置：整段隐藏，且绝不访问该目录
            {"name": "资源服", "stats_dir": str(tmp_path / "nope" / "world" / "stats"), "enabled": False},
        ],
    )
    service = await start_service(config)
    try:
        assert "资源服" not in service._scanners  # 禁用项连扫描器都不建
        board = await service.get_board("deaths")
        assert [item.server for item in board.servers] == ["齿轮盛宴"]
    finally:
        await service.stop()


async def test_server_without_stats_dir_shows_empty_board(tmp_path):
    config = make_lb_config(tmp_path, [{"name": "主服"}])
    service = await start_service(config)
    try:
        assert service.enabled is False  # 没有数据源：统计层不启动
        board = await service.get_board("deaths")
        assert [(item.server, item.rows) for item in board.servers] == [("主服", [])]
    finally:
        await service.stop()


async def test_no_data_and_unavailable_are_distinct(tmp_path, monkeypatch):
    _, stats_dir = make_instance(tmp_path, "a")
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])

    # 目录存在但没有任何玩家：空榜单，不是「不可用」
    service = await start_service(config)
    try:
        board = await service.get_board("deaths")
        assert board.servers[0].rows == []
    finally:
        await service.stop()

    # 数据源整体不可用且从未成功导入：不得伪造 0，必须报不可用
    monkeypatch.setattr(
        StatsScanner, "scan", lambda self, *, known_uuids: ScanResult(kind="unavailable")
    )
    service = await start_service(config)
    try:
        with pytest.raises(LeaderboardUnavailable):
            await service.get_board("deaths")
    finally:
        await service.stop()


async def test_partial_failure_keeps_other_servers_visible(tmp_path, monkeypatch):
    root_a, stats_a = make_instance(tmp_path, "a")
    root_b, stats_b = make_instance(tmp_path, "b")
    write_usercache(root_a, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_a, UUID_MAIN, ticks=200, deaths=8)
    config = make_lb_config(
        tmp_path,
        [
            {"name": "齿轮盛宴", "stats_dir": str(stats_a)},
            {"name": "亡者世界", "stats_dir": str(stats_b)},
        ],
    )
    service = await start_service(config)
    try:
        original = StatsScanner.scan

        def partly(self, *, known_uuids):
            if self.server == "亡者世界":
                return ScanResult(kind="unavailable")
            return original(self, known_uuids=known_uuids)

        monkeypatch.setattr(StatsScanner, "scan", partly)
        await service.scan_once()
        board = await service.get_board("deaths")
        assert [item.server for item in board.servers] == ["齿轮盛宴", "亡者世界"]
        assert board.servers[0].rows[0].value == 8
        assert board.servers[1].rows == []  # 故障服不伪造 0 值玩家
    finally:
        await service.stop()
"""SQLite 持久化：三张表结构、串行访问、损坏重建、重启不丢（验收 9）。"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from leaderboard_helpers import (
    UUID_MAIN,
    FakeClient,
    FakeClock,
    FakeManager,
    join_event,
    make_instance,
    make_lb_config,
    start_service,
    write_stats,
    write_usercache,
)
from mcqq_bridge.leaderboard_models import LeaderboardUnavailable
from mcqq_bridge.stats_store import StatsStore, StoreUnavailable


def read_schema(path: Path) -> dict[str, list[tuple]]:
    conn = sqlite3.connect(path)
    try:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        return {
            table: list(conn.execute(f"PRAGMA table_info({table})"))
            for table in tables
        }
    finally:
        conn.close()


async def test_exact_three_table_schema(tmp_path):
    db_path = tmp_path / "stats.db"
    store = StatsStore(str(db_path), root=str(tmp_path))
    await store.start()
    await store.stop()

    schema = read_schema(db_path)
    assert set(schema) == {"players", "player_stats", "sessions"}
    assert [row[1] for row in schema["players"]] == ["server", "uuid", "name", "first_seen", "last_seen"]
    assert [row[1] for row in schema["player_stats"]] == ["server", "uuid", "ticks", "deaths", "updated_at"]
    assert [row[1] for row in schema["sessions"]] == ["server", "uuid", "name", "started_at"]
    # 主键固定 (server, uuid)
    assert [row[1] for row in schema["player_stats"] if row[5]] == ["server", "uuid"]
    assert [row[1] for row in schema["sessions"] if row[5]] == ["server", "uuid"]


async def test_to_thread_jobs_never_overlap_connection_access(tmp_path):
    store = StatsStore(str(tmp_path / "stats.db"), root=str(tmp_path))
    await store.start()
    events: list[tuple[str, int]] = []

    def make(index: int):
        def job(conn):
            events.append(("enter", index))
            time.sleep(0.01)
            events.append(("exit", index))
            return index

        return job

    try:
        results = await asyncio.gather(*(store._submit(make(index)) for index in range(5)))
        assert results == [0, 1, 2, 3, 4]
        # 严格串行：enter/exit 必须成对且不交错
        assert events == [
            item for index in range(5) for item in (("enter", index), ("exit", index))
        ]
    finally:
        await store.stop()


async def test_corrupt_database_is_quarantined_and_rebuilt(tmp_path):
    db_path = tmp_path / "stats.db"
    db_path.write_bytes(b"this is definitely not a sqlite database" * 10)
    store = StatsStore(str(db_path), root=str(tmp_path))
    await store.start()
    try:
        players, stats, sessions = await store.load_all()
        assert (players, stats, sessions) == ([], [], [])
    finally:
        await store.stop()
    # 旧库被隔离重命名而不是直接删除
    assert any(path.name.startswith("stats.db.corrupt-") for path in tmp_path.iterdir())
    schema = read_schema(db_path)
    assert set(schema) == {"players", "player_stats", "sessions"}


async def test_out_of_root_path_is_refused_without_creating_directories(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside" / "deep"
    store = StatsStore(str(outside / "stats.db"), root=str(root))
    with pytest.raises(StoreUnavailable):
        await store.start()
    # 越界路径连父目录都不创建（需求 §11：唯一可写路径）
    assert not outside.exists()
    assert not (tmp_path / "outside").exists()


async def test_unwritable_database_degrades_to_unavailable(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    _, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=200, deaths=3)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    config = config.model_copy(
        update={"stats": config.stats.model_copy(update={"db_path": str(blocker / "stats.db")})}
    )
    service = await start_service(config)
    try:
        assert service.enabled is True
        with pytest.raises(LeaderboardUnavailable):
            await service.get_board("deaths")
    finally:
        await service.stop()


async def test_restart_preserves_authoritative_totals(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_dir, UUID_MAIN, ticks=740_000, deaths=82, mtime=1_760_000_000)
    config = make_lb_config(tmp_path, [{"name": "齿轮盛宴", "stats_dir": str(stats_dir)}])

    service = await start_service(config)
    first = await service.get_board("deaths")
    assert first.servers[0].rows[0].value == 82
    await service.stop()

    # 直接读库确认累计值真的落盘（不是只存在内存里）
    store = StatsStore(config.stats.db_path, root=config.stats.root)
    await store.start()
    try:
        _, stats, _ = await store.load_all()
    finally:
        await store.stop()
    assert [(row.server, row.ticks, row.deaths) for row in stats] == [("齿轮盛宴", 740_000, 82)]

    # 重启后数字不丢、不清零
    service = await start_service(config)
    try:
        again = await service.get_board("deaths")
        assert again.servers[0].rows[0].value == 82
        assert again.servers[0].rows[0].player == "MUEZORE"
    finally:
        await service.stop()


async def test_restart_does_not_count_bridge_downtime(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_dir, UUID_MAIN, ticks=2000, deaths=1, mtime=1_760_000_000)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    clock = FakeClock(1_760_000_100.0)
    client = FakeClient("主服", ["MUEZORE"])
    manager = FakeManager([client])

    service = await start_service(config, manager=manager, clock=clock)
    service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
    await service._drain_events()
    clock.advance(10)
    before = await service.get_board("playtime")
    assert before.servers[0].rows[0].value == 110  # 100 秒权威 + 10 秒在线
    await service.stop()

    # 桥接器停机 1000 秒后重启：绝不允许把停机时间算成在线时长
    clock.advance(1000)
    service = await start_service(config, manager=manager, clock=clock)
    try:
        await service.reconcile_once()
        board = await service.get_board("playtime")
        assert board.servers[0].rows[0].value == 100
        assert board.servers[0].rows[0].online is True
    finally:
        await service.stop()


async def test_store_survives_write_failure_and_keeps_serving(tmp_path, caplog):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_dir, UUID_MAIN, ticks=2000, deaths=2, mtime=1_760_000_000)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        assert (await service.get_board("deaths")).servers[0].rows[0].value == 2

        def broken(patch):
            raise sqlite3.OperationalError("disk I/O error")

        service._store.apply = broken  # type: ignore[union-attr]  # 模拟写失败

        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        # 写失败只记日志：榜单仍可读，服务不崩
        assert (await service.get_board("deaths")).servers[0].rows[0].value == 2
    finally:
        await service.stop()

async def test_stop_waits_for_inflight_write_before_closing_connection(tmp_path):
    """在途写入的线程必须跑完才关连接，避免「to_thread 还在跑就 close」。"""
    store = StatsStore(str(tmp_path / "stats.db"), root=str(tmp_path))
    await store.start()
    finished: list[int] = []

    def slow(conn):
        time.sleep(0.2)
        finished.append(1)
        return 1

    task = asyncio.create_task(store._submit(slow))
    await asyncio.sleep(0.05)
    await store.stop()
    await asyncio.gather(task, return_exceptions=True)
    assert finished == [1]

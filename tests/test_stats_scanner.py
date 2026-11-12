"""统计文件扫描与降级语义（需求 §9 / §10，验收 5、9）。

覆盖：mtime 缓存、提交失败不推进缓存、半写文件、usercache 改名、
缺失映射回退、换世界归零、缺目录 vs 权限错误、只读直属 UUID 文件。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from leaderboard_helpers import (
    UUID_MAIN,
    UUID_OTHER,
    join_event,
    make_instance,
    make_lb_config,
    start_service,
    write_raw_stats,
    write_stats,
    write_usercache,
)
from mcqq_bridge import stats_scanner as scanner_module
from mcqq_bridge.stats_store import StoreUnavailable
from mcqq_bridge.stats_scanner import StatsScanner


def test_unchanged_mtime_skips_json_reads(monkeypatch, tmp_path):
    _, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=1000, deaths=3)
    scanner = StatsScanner("主服", str(stats_dir))
    first = scanner.scan(known_uuids=set())
    assert [entry.uuid for entry in first.entries] == [UUID_MAIN]
    scanner.commit(first)

    calls = {"count": 0}
    original = scanner_module.parse_stats_document

    def counting(raw: str):
        calls["count"] += 1
        return original(raw)

    monkeypatch.setattr(scanner_module, "parse_stats_document", counting)
    second = scanner.scan(known_uuids={UUID_MAIN})
    assert second.kind == "ok"
    assert second.entries == []  # 签名未变：不读 JSON
    assert calls["count"] == 0


def test_failed_commit_does_not_advance_cache(tmp_path):
    _, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=200, deaths=1)
    scanner = StatsScanner("主服", str(stats_dir))
    first = scanner.scan(known_uuids=set())
    assert len(first.entries) == 1
    # 不调用 commit（模拟数据库写入失败）：下一轮必须重试同一个文件
    second = scanner.scan(known_uuids=set())
    assert [entry.uuid for entry in second.entries] == [UUID_MAIN]
    scanner.commit(second)
    assert scanner.scan(known_uuids=set()).entries == []


def test_half_written_json_keeps_previous_values_and_warns_once(tmp_path, caplog):
    _, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=400, deaths=2)
    scanner = StatsScanner("主服", str(stats_dir))
    scanner.commit(scanner.scan(known_uuids=set()))

    write_raw_stats(stats_dir, UUID_MAIN, '{"stats": {"minecraft:custom": {"minecraft:pl', mtime=1_760_000_500)
    with caplog.at_level(logging.WARNING, logger="mcqq_bridge.stats_scanner"):
        broken = scanner.scan(known_uuids={UUID_MAIN})
    assert broken.kind == "ok"
    assert broken.entries == []  # 保留上次有效值，不返回坏数据
    warnings = [record for record in caplog.records if "统计文件不可用" in record.getMessage()]
    assert len(warnings) == 1
    # 日志只含服务器名与受控原因，不含 UUID / 路径 / 玩家名
    assert UUID_MAIN not in warnings[0].getMessage()
    assert str(stats_dir) not in warnings[0].getMessage()

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="mcqq_bridge.stats_scanner"):
        scanner.scan(known_uuids={UUID_MAIN})
    assert not [r for r in caplog.records if "统计文件不可用" in r.getMessage()]


def test_invalid_numeric_stats_are_not_fabricated_zero(tmp_path):
    _, stats_dir = make_instance(tmp_path, "a")
    write_raw_stats(
        stats_dir,
        UUID_MAIN,
        '{"stats": {"minecraft:custom": {"minecraft:play_time": -5, "minecraft:deaths": 3}}}',
    )
    write_raw_stats(
        stats_dir,
        UUID_OTHER,
        '{"stats": {"minecraft:custom": {"minecraft:play_time": "abc", "minecraft:deaths": 1}}}',
    )
    scanner = StatsScanner("主服", str(stats_dir))
    result = scanner.scan(known_uuids=set())
    assert result.kind == "ok"
    assert result.entries == []  # 非法数值不当 0，跳过该文件


def test_only_direct_uuid_json_and_usercache_are_read(tmp_path):
    instance, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=20, deaths=0)
    (stats_dir / "README.txt").write_text("ignore me", encoding="utf-8")
    (stats_dir / "playerdata.dat").write_bytes(b"\x00\x01")
    nested = stats_dir / "sub"
    nested.mkdir()
    write_stats(nested, UUID_OTHER, ticks=999, deaths=9)
    outside = Path(tmp_path) / "outside.json"
    write_stats(Path(tmp_path), UUID_OTHER, ticks=999, deaths=9)
    os.symlink(outside, stats_dir / f"{UUID_OTHER}.json")

    scanner = StatsScanner("主服", str(stats_dir))
    result = scanner.scan(known_uuids=set())
    assert [entry.uuid for entry in result.entries] == [UUID_MAIN]
    assert scanner.usercache_path == instance / "usercache.json"


def test_missing_directory_is_missing_kind(tmp_path):
    stats_dir = Path(tmp_path) / "nowhere" / "world" / "stats"
    scanner = StatsScanner("主服", str(stats_dir))
    assert scanner.scan(known_uuids={UUID_MAIN}).kind == "missing"


def test_permission_error_is_unavailable_kind(tmp_path, monkeypatch):
    _, stats_dir = make_instance(tmp_path, "a")
    scanner = StatsScanner("主服", str(stats_dir))

    def deny(path):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(scanner_module.os, "scandir", deny)
    result = scanner.scan(known_uuids={UUID_MAIN})
    assert result.kind == "unavailable"


def test_deleted_file_reports_removed(tmp_path):
    _, stats_dir = make_instance(tmp_path, "a")
    path = write_stats(stats_dir, UUID_MAIN, ticks=60, deaths=1)
    scanner = StatsScanner("主服", str(stats_dir))
    scanner.commit(scanner.scan(known_uuids=set()))
    path.unlink()
    result = scanner.scan(known_uuids={UUID_MAIN})
    assert result.removed == [UUID_MAIN]


# --- service 层：展示名、缺目录、权限错误、归零、提交失败 -------------------


async def test_usercache_only_change_updates_names(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=200, deaths=4)
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    config = make_lb_config(tmp_path, [{"name": "齿轮盛宴", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        board = await service.get_board("deaths")
        assert board.servers[0].rows[0].player == "MUEZORE"

        # 只改 usercache（stats 文件与 mtime 都不动）：展示名必须跟着变
        write_usercache(root, [("Renamed", UUID_MAIN, "2026-11-02 00:00:00 +0000")])
        await service.scan_once()
        board = await service.get_board("deaths")
        assert [row.player for row in board.servers[0].rows] == ["Renamed"]
    finally:
        await service.stop()


async def test_missing_mapping_uses_uuid_prefix_question_mark(tmp_path):
    _, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=200, deaths=4)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        board = await service.get_board("deaths")
        assert board.servers[0].rows[0].player == f"{UUID_MAIN[:8]}?"
    finally:
        await service.stop()


async def test_world_reset_replaces_old_totals(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_dir, UUID_MAIN, ticks=20_000, deaths=50)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        board = await service.get_board("deaths")
        assert board.servers[0].rows[0].value == 50
        # 换世界：ticks 掉回 400（20 秒）、deaths 归零，文件必须整体替换而不是取 MAX
        write_stats(stats_dir, UUID_MAIN, ticks=400, deaths=0, mtime=1_760_000_900)
        await service.scan_once()
        board = await service.get_board("deaths")
        assert board.servers[0].rows[0].value == 0
        playtime = await service.get_board("playtime")
        assert playtime.servers[0].rows[0].value == 20
    finally:
        await service.stop()


async def test_missing_directory_shows_no_data_but_permission_keeps_history(tmp_path, monkeypatch):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    write_stats(stats_dir, UUID_MAIN, ticks=200, deaths=7)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        assert (await service.get_board("deaths")).servers[0].rows[0].value == 7

        # 权限/IO 失败：保留上次有效值，不当成空目录
        def deny(path):
            raise PermissionError(13, "denied")

        monkeypatch.setattr(scanner_module.os, "scandir", deny)
        await service.scan_once()
        assert (await service.get_board("deaths")).servers[0].rows[0].value == 7

        # 目录不存在：该服「暂无数据」，不展示旧 DB 行
        monkeypatch.undo()
        for child in stats_dir.iterdir():
            child.unlink()
        stats_dir.rmdir()
        await service.scan_once()
        board = await service.get_board("deaths")
        assert board.servers[0].rows == []
    finally:
        await service.stop()


async def test_write_failure_does_not_advance_cache_or_fabricate(tmp_path):
    _, stats_dir = make_instance(tmp_path, "a")
    write_stats(stats_dir, UUID_MAIN, ticks=200, deaths=7)
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        assert (await service.get_board("deaths")).servers[0].rows[0].value == 7

        async def failing(patch):
            raise StoreUnavailable("disk full")

        service._store.apply = failing  # type: ignore[union-attr]
        write_stats(stats_dir, UUID_MAIN, ticks=400, deaths=9, mtime=1_760_000_800)
        await service.scan_once()
        # 写失败：内存里仍是旧值，且扫描缓存未推进
        assert (await service.get_board("deaths")).servers[0].rows[0].value == 7
        scanner = service._scanners["主服"]
        assert scanner.scan(known_uuids={UUID_MAIN}).entries  # 还能重试同一文件
    finally:
        await service.stop()


async def test_join_event_without_stats_file_creates_zero_baseline_row(tmp_path):
    root, stats_dir = make_instance(tmp_path, "a")
    write_usercache(root, [("MUEZORE", UUID_MAIN, "2026-11-01 00:00:00 +0000")])
    config = make_lb_config(tmp_path, [{"name": "主服", "stats_dir": str(stats_dir)}])
    service = await start_service(config)
    try:
        service.offer_event(join_event("主服", "MUEZORE", UUID_MAIN))
        await service._drain_events()
        board = await service.get_board("playtime")
        rows = board.servers[0].rows
        assert [row.player for row in rows] == ["MUEZORE"]
        assert rows[0].value == 0 and rows[0].online is True
        assert rows[0].display == "0s（在线中）"
    finally:
        await service.stop()


@pytest.mark.parametrize("raw", ["[]", "{}", '{"stats": []}', '{"stats": {"minecraft:custom": []}}'])
def test_structurally_invalid_documents_are_skipped(tmp_path, raw: str):
    _, stats_dir = make_instance(tmp_path, "a")
    write_raw_stats(stats_dir, UUID_MAIN, raw)
    scanner = StatsScanner("主服", str(stats_dir))
    result = scanner.scan(known_uuids=set())
    assert result.kind == "ok"
    assert result.entries == []
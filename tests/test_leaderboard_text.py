"""排行榜文本渲染：秒级格式、整服优先分片、截断标注（需求 §7 / 验收 6、11）。"""

from __future__ import annotations

from typing import Literal

import pytest

from mcqq_bridge.leaderboard_models import Leaderboard, RankRow, ServerBoard
from mcqq_bridge.leaderboard_text import (
    NO_DATA_GLOBAL,
    TRUNCATION_NOTE,
    format_duration,
    render_leaderboard,
)


def board(kind: Literal["deaths", "playtime"], boards: list[tuple[str, list[tuple[str, int]]]]) -> Leaderboard:
    servers = []
    for name, rows in boards:
        rank_rows = [
            RankRow(
                rank=index,
                player=player,
                value=value,
                display=f"{value} 次" if kind == "deaths" else format_duration(value),
            )
            for index, (player, value) in enumerate(rows, start=1)
        ]
        servers.append(ServerBoard(server=name, rows=rank_rows, total_players=len(rank_rows)))
    return Leaderboard(kind=kind, generated_at="2026-11-12T08:00:00Z", servers=servers)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (7, "07s"),
        (42, "42s"),
        (59, "59s"),
        (60, "01m00s"),
        (72, "01m12s"),
        (3599, "59m59s"),
        (3600, "1h00m00s"),
        (74107, "20h35m07s"),
        (360000, "100h00m00s"),
    ],
)
def test_second_precision_and_duration_boundaries(seconds: int, expected: str) -> None:
    # 秒级精度：绝不出现分钟粒度或四舍五入到分钟
    assert format_duration(seconds) == expected


def test_ticks_to_seconds_uses_floor_division() -> None:
    from mcqq_bridge.leaderboards import LeaderboardService

    # 19 tick 不足 1 秒 → 0s；20 tick → 01s（需求 §12.6）
    assert LeaderboardService is not None
    assert format_duration(19 // 20) == "0s"
    assert format_duration(20 // 20) == "01s"
    assert format_duration(1_220_773 // 20) == "16h57m18s"


def test_render_includes_zero_value_players_and_blank_line_between_servers() -> None:
    rendered = render_leaderboard(
        board("deaths", [("齿轮盛宴", [("MUEZORE", 82), ("yui_Thefish", 0)]), ("亡者世界", [("lazyhape", 5)])]),
        max_chars=500,
        max_parts=5,
    )
    assert rendered == [
        "齿轮盛宴 死亡榜：\n1. MUEZORE  82 次\n2. yui_Thefish  0 次\n\n亡者世界 死亡榜：\n1. lazyhape  5 次"
    ]


def test_online_marker_is_appended_for_playtime() -> None:
    item = Leaderboard(
        kind="playtime",
        generated_at="2026-11-12T08:00:00Z",
        servers=[
            ServerBoard(
                server="齿轮盛宴",
                rows=[
                    RankRow(rank=1, player="MUEZORE", value=69007, display="19h10m07s（在线中）", online=True),
                ],
                total_players=1,
            )
        ],
    )
    assert render_leaderboard(item, 500, 5) == ["齿轮盛宴 在线时长榜：\n1. MUEZORE  19h10m07s（在线中）"]


def test_empty_server_uses_per_server_no_data_text() -> None:
    rendered = render_leaderboard(
        board("deaths", [("齿轮盛宴", [("MUEZORE", 1)]), ("原版生电", [])]), 500, 5
    )
    assert "原版生电 暂无数据" in rendered[0]


def test_all_servers_empty_uses_global_text() -> None:
    item = board("deaths", [("齿轮盛宴", []), ("亡者世界", [])])
    assert render_leaderboard(item, 500, 5) == [NO_DATA_GLOBAL]


def test_single_server_query_keeps_server_name_when_empty() -> None:
    item = board("deaths", [("齿轮盛宴", [])])
    assert render_leaderboard(item, 500, 5, single_server=True) == ["齿轮盛宴 暂无数据"]


def test_pack_server_blocks_before_splitting_rows() -> None:
    first = [("A", 10), ("B", 9)]
    second = [("C", 8), ("D", 7)]
    item = board("deaths", [("服一", first), ("服二", second)])
    full = render_leaderboard(item, 500, 5)
    assert len(full) == 1
    # 把上限压到刚好放不下两段：应整段换片，而不是把「服一」的玩家行拆到两片
    limit = len("服一 死亡榜：\n1. A  10 次\n2. B  9 次") + 1
    parts = render_leaderboard(item, limit, 5)
    assert parts[0] == "服一 死亡榜：\n1. A  10 次\n2. B  9 次"
    assert parts[1] == "服二 死亡榜：\n1. C  8 次\n2. D  7 次"


def test_oversized_server_block_splits_between_rows_and_repeats_header() -> None:
    rows = [(f"玩家{index:02d}", 100 - index) for index in range(20)]
    item = board("deaths", [("主服", rows)])
    limit = len("主服 死亡榜：\n1. 玩家00  100 次\n2. 玩家01  99 次\n3. 玩家02  98 次")
    parts = render_leaderboard(item, limit, 10)
    assert len(parts) > 1
    for part in parts:
        assert len(part) <= limit
        assert part.splitlines()[0] == "主服 死亡榜："
    # 每个玩家行都完整出现（不切断行），且没有被截断
    joined = "\n".join(parts)
    for name, _ in rows:
        assert f"{name}  " in joined
    assert TRUNCATION_NOTE not in joined


def test_more_than_five_parts_reserves_truncation_note() -> None:
    rows = [(f"P{index:03d}", 1000 - index) for index in range(300)]
    item = board("deaths", [("主服", rows)])
    parts = render_leaderboard(item, 200, 5)
    assert len(parts) == 5
    assert all(len(part) <= 200 for part in parts)
    assert parts[-1].endswith(TRUNCATION_NOTE)
    # 截断提示独占一行，且它替换掉的是完整玩家行而不是半行
    for line in parts[-1].splitlines():
        assert line == TRUNCATION_NOTE or line == "主服 死亡榜：" or line.startswith(
            tuple(f"{index}. " for index in range(1, 400))
        )


def test_oversized_single_row_is_not_cut() -> None:
    long_name = "N" * 120
    item = board("deaths", [("主服", [(long_name, 5)])])
    parts = render_leaderboard(item, 60, 5)
    assert all(len(part) <= 60 for part in parts)
    for part in parts:
        assert long_name[:20] not in part  # 放不下就整行不出现，绝不切成半行
    assert parts[-1].endswith(TRUNCATION_NOTE)


def test_truncation_note_fits_within_budget() -> None:
    rows = [(f"P{index:03d}", 1000 - index) for index in range(50)]
    item = board("deaths", [("主服", rows)])
    for limit in (50, 60, 80, 120):
        parts = render_leaderboard(item, limit, 2)
        assert all(len(part) <= limit for part in parts)
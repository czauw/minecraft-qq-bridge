"""排行榜文本渲染：只消费 :class:`Leaderboard` DTO，不访问数据库/MC/QQ。

分片规则（需求 §7 + 验收 §12.11）：

* 优先整块搬运「一台服务器」的段落，放不下才在**完整玩家行之间**切分；
* 续片重复该服标题，**永不切断单个玩家行**；
* 单片上限 ``max_message_chars``、片数上限 ``max_reply_parts``；
* 只要还有玩家行/服务器没展示，就在最后一片末尾加 ``（列表过长，已截断）``，
  标注本身也计入片长预算。
"""

from __future__ import annotations

from .leaderboard_models import Leaderboard, ServerBoard

__all__ = ["TRUNCATION_NOTE", "format_deaths", "format_duration", "render_leaderboard"]

TITLES = {"deaths": "死亡榜", "playtime": "在线时长榜"}
TRUNCATION_NOTE = "（列表过长，已截断）"
NO_DATA_GLOBAL = "排行榜暂无数据"


def format_duration(seconds: int) -> str:
    """秒级格式：``20h35m07s`` / ``07m12s`` / ``42s`` / ``0s``（小时不补零）。"""
    value = int(seconds)
    if value <= 0:
        return "0s"
    hours, remainder = divmod(value, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes:02d}m{secs:02d}s"
    return f"{secs:02d}s"


def format_deaths(count: int) -> str:
    return f"{int(count)} 次"


def _server_block(board: ServerBoard, kind: str) -> list[str]:
    if not board.rows:
        return [f"{board.server} 暂无数据"]
    header = f"{board.server} {TITLES.get(kind, '排行榜')}："
    return [header] + [f"{row.rank}. {row.player}  {row.display}" for row in board.rows]


def _append_note(lines: list[str], max_chars: int) -> list[str]:
    """在保持完整行的前提下腾出空间放截断标注；必要时只保留标注。"""
    note = TRUNCATION_NOTE
    if len(note) > max_chars:
        return [note[:max_chars]]
    kept = list(lines)
    while kept and len("\n".join(kept)) + 1 + len(note) > max_chars:
        kept.pop()
    if not kept:
        return [note]
    return kept + [note]


def render_leaderboard(
    board: Leaderboard,
    max_chars: int,
    max_parts: int,
    *,
    single_server: bool = False,
) -> list[str]:
    """把 DTO 渲染成 1..max_parts 条消息（每条都不超过 ``max_chars``）。"""
    max_chars = max(1, int(max_chars))
    max_parts = max(1, int(max_parts))

    blocks = [block for block in (_server_block(item, board.kind) for item in board.servers) if block]
    has_rows = any(item.rows for item in board.servers)
    if not has_rows:
        if single_server and blocks:
            return [_fit(blocks[0][0], max_chars)]
        return [_fit(NO_DATA_GLOBAL, max_chars)]

    parts: list[list[str]] = []
    current: list[str] = []
    truncated = False

    def flush() -> None:
        nonlocal current
        if current:
            parts.append(current)
            current = []

    for block in blocks:
        separator = [""] if current else []  # 服务器段之间空行分隔（需求 §7）
        joined = "\n".join(current + separator + block)
        if len(joined) <= max_chars:
            current = current + separator + block
            continue
        flush()
        if len("\n".join(block)) <= max_chars:
            current = list(block)
            continue
        # 单服整块超过一片：在完整玩家行之间切分，续片重复标题
        header, rows = block[0], block[1:]
        current = [header]
        for row in rows:
            if len("\n".join(current + [row])) <= max_chars:
                current.append(row)
                continue
            if len("\n".join([header, row])) > max_chars:
                # 单行本身就放不下：绝不拆行，就地停止并标注截断
                truncated = True
                break
            flush()
            current = [header, row]
        if truncated:
            flush()
            break
    else:
        flush()

    if truncated is False and len(parts) > max_parts:
        truncated = True
    if len(parts) > max_parts:
        parts = parts[:max_parts]
    if truncated:
        if not parts:
            return [_fit(TRUNCATION_NOTE, max_chars)]
        parts[-1] = _append_note(parts[-1], max_chars)

    rendered = ["\n".join(lines) for lines in parts if lines]
    return rendered or [_fit(NO_DATA_GLOBAL, max_chars)]


def _fit(text: str, max_chars: int) -> str:
    return text if len(text) <= max_chars else text[:max_chars]
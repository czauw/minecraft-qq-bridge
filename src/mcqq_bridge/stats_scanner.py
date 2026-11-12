"""统计文件扫描：只读 ``world/stats/*.json`` 与 ``usercache.json``。

职责边界：本模块只做「读文件 + 校验 + 算出增量批次」，不碰 SQLite、QQ、HTTP，
也不决定榜单展示。mtime/size/inode 签名缓存让每 60 秒的增量扫描只解析真正
变化的文件；**缓存只在数据库事务提交成功后由 :meth:`StatsScanner.commit`
推进**，写失败不会被误标成「已导入」。

失败语义（需求 §10）：

* 半写/坏 JSON → 跳过该文件、保留上次有效值、按签名只 WARNING 一次；
* 目录不存在 → ``missing``（该服显示「暂无数据」），不删除已有累计值；
* 权限/IO 错误 → ``unavailable``（该服本轮不可用），保留旧值，绝不批量删除。
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid as uuid_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

__all__ = [
    "ScanResult",
    "StatsFileEntry",
    "StatsScanner",
    "UserCache",
    "normalize_uuid",
]

MAX_PLAYER_NAME = 64
STATS_FILENAME = re.compile(r"^([0-9a-fA-F-]{32,36})\.json$")
_CUSTOM_KEY = "minecraft:custom"
_PLAY_TIME_KEY = "minecraft:play_time"
_DEATHS_KEY = "minecraft:deaths"

ScanKind = Literal["ok", "missing", "unavailable"]


def normalize_uuid(value: str) -> str:
    """把 UUID 规范化成小写带连字符形式；非法值返回空串（不猜、不补零）。"""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 64:
        return ""
    try:
        return str(uuid_module.UUID(text))
    except (ValueError, AttributeError, TypeError):
        return ""


@dataclass(frozen=True)
class StatsFileEntry:
    uuid: str
    ticks: int
    deaths: int
    mtime: float  # 文件落盘时刻（epoch 秒），用于推进在线会话 anchor


@dataclass
class ScanResult:
    kind: ScanKind
    entries: list[StatsFileEntry] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    current: set[str] = field(default_factory=set)
    signatures: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    detail: str = ""


@dataclass(frozen=True)
class UserCache:
    uuid_to_name: dict[str, str] = field(default_factory=dict)
    name_to_uuid: dict[str, str] = field(default_factory=dict)


class _BadFile(Exception):
    """统计文件内容不可用（半写、结构错误、数值非法）。"""


def _read_count(custom: dict, key: str) -> int:
    if key not in custom:
        return 0  # 原版对 0 计数可以省略键
    value = custom[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _BadFile("invalid_numeric")
    return int(value)


def parse_stats_document(raw: str) -> tuple[int, int]:
    """解析一份 stats JSON，返回 ``(play_time_ticks, deaths)``。"""
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise _BadFile("invalid_json") from exc
    if not isinstance(document, dict):
        raise _BadFile("invalid_document")
    if "stats" not in document:
        # 缺少顶级 stats 键不是「0 次」而是结构可疑：宁可保留上次有效值
        raise _BadFile("missing_stats")
    stats = document["stats"]
    if not isinstance(stats, dict):
        raise _BadFile("invalid_stats")
    custom = stats.get(_CUSTOM_KEY, {})
    if not isinstance(custom, dict):
        raise _BadFile("invalid_custom")
    return _read_count(custom, _PLAY_TIME_KEY), _read_count(custom, _DEATHS_KEY)


def _expires_rank(value: object) -> str:
    """usercache 的 ``expiresOn`` 是 ``YYYY-MM-DD HH:MM:SS +0000``，可直接按字典序比较。"""
    return value if isinstance(value, str) else ""


def _clean_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > MAX_PLAYER_NAME:
        return ""
    return text


class StatsScanner:
    """单个服务器的统计文件扫描器（无状态以外的缓存都在实例内）。"""

    def __init__(
        self,
        server: str,
        stats_dir: str,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.server = server
        self.stats_dir = stats_dir
        self._log = logger or logging.getLogger(f"mcqq_bridge.stats_scan.{server}")
        self._signatures: dict[str, tuple[int, int, int]] = {}
        self._failed_signatures: set[tuple[int, int, int]] = set()
        self._usercache_signature: tuple[int, int, int] | None = None
        self._usercache = UserCache()

    # -- 名称映射 ----------------------------------------------------------

    @property
    def usercache_path(self) -> Path | None:
        """``usercache.json`` 的唯一推导位置：``<stats 的上级上级>/usercache.json``。"""
        if not self.stats_dir:
            return None
        return Path(self.stats_dir).parent.parent / "usercache.json"

    def usercache(self) -> UserCache:
        """读取并缓存 usercache；文件缺失/损坏时返回上一次的有效映射（可能为空）。"""
        path = self.usercache_path
        if path is None:
            return self._usercache
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._usercache_signature = None
            self._usercache = UserCache()
            return self._usercache
        except OSError:
            return self._usercache
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if signature == self._usercache_signature:
            return self._usercache
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._log.warning("usercache 读取失败（%s），继续使用上次的名字映射", self.server)
            return self._usercache
        if not isinstance(document, list):
            self._log.warning("usercache 结构异常（%s），继续使用上次的名字映射", self.server)
            return self._usercache

        uuid_to_name: dict[str, tuple[str, str, int]] = {}
        name_to_uuid: dict[str, tuple[str, str, int]] = {}
        for index, item in enumerate(document):
            if not isinstance(item, dict):
                continue
            uuid_value = normalize_uuid(item.get("uuid", ""))
            name = _clean_name(item.get("name"))
            if not uuid_value or not name:
                continue
            rank = (_expires_rank(item.get("expiresOn")), "", index)
            previous = uuid_to_name.get(uuid_value)
            if previous is None or (previous[0], previous[2]) <= (rank[0], rank[2]):
                uuid_to_name[uuid_value] = (name, rank[0], index)
            # 同一名字可能有多个 UUID（离线模式 v3 与正版 v4 并存）：取最近一次登录的
            current = name_to_uuid.get(name)
            if current is None or (current[0], current[2]) <= (rank[0], rank[2]):
                name_to_uuid[name] = (uuid_value, rank[0], index)

        self._usercache = UserCache(
            uuid_to_name={key: value[0] for key, value in uuid_to_name.items()},
            name_to_uuid={key: value[0] for key, value in name_to_uuid.items()},
        )
        self._usercache_signature = signature
        return self._usercache

    # -- 扫描 --------------------------------------------------------------

    def scan(self, *, known_uuids: set[str]) -> ScanResult:
        """扫一轮目录。缓存不在这里推进 —— 由调用方在事务提交后调 :meth:`commit`。"""
        if not self.stats_dir:
            return ScanResult(kind="missing")
        try:
            with os.scandir(self.stats_dir) as iterator:
                listing = list(iterator)
        except (FileNotFoundError, NotADirectoryError):
            return ScanResult(kind="missing")
        except OSError as exc:
            return ScanResult(kind="unavailable", detail=type(exc).__name__)

        entries: list[StatsFileEntry] = []
        signatures: dict[str, tuple[int, int, int]] = {}
        current: set[str] = set()
        for item in listing:
            match = STATS_FILENAME.match(item.name)
            if match is None:
                continue
            uuid_value = normalize_uuid(match.group(1))
            if not uuid_value:
                continue
            try:
                if not item.is_file(follow_symlinks=False):
                    continue
                stat = item.stat(follow_symlinks=False)
            except OSError:
                continue
            signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
            current.add(uuid_value)
            signatures[uuid_value] = signature
            if self._signatures.get(uuid_value) == signature:
                continue  # mtime/size/inode 未变：不重复读 JSON
            entry = self._parse_file(item.path, uuid_value, signature)
            if entry is not None:
                entries.append(entry)

        return ScanResult(
            kind="ok",
            entries=entries,
            removed=sorted(known_uuids - current),
            current=current,
            signatures=signatures,
        )

    def _parse_file(
        self, path: str, uuid_value: str, signature: tuple[int, int, int]
    ) -> StatsFileEntry | None:
        try:
            raw = Path(path).read_text(encoding="utf-8")
            # 读前后签名必须一致，否则可能读到半写内容：本轮跳过
            try:
                after = os.stat(path, follow_symlinks=False)
            except OSError:
                return None
            if (after.st_mtime_ns, after.st_size, after.st_ino) != signature:
                return None
            ticks, deaths = parse_stats_document(raw)
        except _BadFile as exc:
            if signature not in self._failed_signatures:
                self._failed_signatures.add(signature)
                self._log.warning(
                    "统计文件不可用（%s，原因 %s），本轮保留上次有效值", self.server, str(exc)
                )
            return None
        except OSError:
            return None
        self._failed_signatures.discard(signature)
        return StatsFileEntry(uuid=uuid_value, ticks=ticks, deaths=deaths, mtime=float(after.st_mtime))

    def commit(self, result: ScanResult) -> None:
        """数据库事务提交成功后才推进缓存签名。"""
        if result.kind != "ok":
            return
        self._signatures = dict(result.signatures)


def merge_usercache_names(cache: UserCache, uuid_value: str) -> str:
    """展示名：usercache 最新名字；无映射时用 UUID 前 8 位并标记 ``?``（需求 §4）。"""
    name = cache.uuid_to_name.get(uuid_value, "")
    if name:
        return name
    return f"{uuid_value[:8]}?"
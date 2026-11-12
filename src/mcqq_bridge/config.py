"""严格 YAML 配置模型：QQ 官方机器人 + QueQiao 多服务器。

设计要点（对应 docs/MULTISERVER_REQUIREMENTS.md）：
* 顶层固定 ``bot`` / ``servers`` / ``relay`` / ``stats`` / ``runtime`` / ``logging``，
  未知字段直接拒绝。
* 每个 server 的 ``name`` 唯一，且（规范化 host, port）组合全局唯一（含禁用项）。
* 密钥支持 ``${ENV_NAME}`` 严格引用；结构校验（--check-config）不要求密钥存在，
  真正运行前会再校验一次，缺失时给出字段路径并清晰退出。
* 本模块不打日志、不打印配置内容，错误信息只包含字段路径和原因。
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "AppConfig",
    "BotConfig",
    "ConfigError",
    "LoggingConfig",
    "PlayerQueryConfig",
    "RelayConfig",
    "RuntimeConfig",
    "ServerConfig",
    "StatsConfig",
    "StatusHttpConfig",
    "load_config",
]

_ENV_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_HOST_ALLOWED = re.compile(r"^[A-Za-z0-9._:\-\[\]]+$")
_URL_BASE = re.compile(r"^https?://[^\s/]+$")
_WS_URL = re.compile(r"^wss?://[^\s]+$")


class ConfigError(Exception):
    """配置不可用。``issues`` 为人类可读的字段路径 + 原因。"""

    def __init__(self, issues: list[str]) -> None:
        self.issues = list(issues)
        super().__init__("；".join(self.issues))


class _Model(BaseModel):
    # strict=True：拒绝一切隐式类型转换（bool/int/float/str 之间不互转），
    # 避免 ``enabled: "yes"``、``port: true``、``tls: 1`` 这类错误配置被静默接受。
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class PlayerQueryConfig(_Model):
    """单个服务器的在线玩家名单查询策略。"""

    mode: Literal["auto", "rcon", "status"] = "auto"
    rcon_command: str = "list"
    timeout_seconds: float = Field(default=8.0, gt=0, le=60)

    @field_validator("rcon_command")
    @classmethod
    def _command(cls, value: str) -> str:
        if not value or len(value) > 64 or "\n" in value or "\r" in value:
            raise ValueError("rcon_command 必须是 1-64 字符的单行命令")
        return value


class ServerConfig(_Model):
    name: str = Field(min_length=1, max_length=64)
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    enabled: bool = True
    path: str = "/"
    tls: bool = False
    access_token: str = Field(default="", max_length=512)
    self_name: str = Field(default="", max_length=64)
    # 排行榜数据源：该服实例目录下的 ``world/stats``。留空 = 该服不采集（排行榜里显示「暂无数据」）。
    stats_dir: str = Field(default="", max_length=1024)
    players: PlayerQueryConfig = Field(default_factory=PlayerQueryConfig)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    reconnect_initial_seconds: float = Field(default=1.0, gt=0, le=60)
    reconnect_max_seconds: float = Field(default=60.0, ge=1, le=600)

    @field_validator("stats_dir")
    @classmethod
    def _stats_dir(cls, value: str) -> str:
        text = value.strip()
        if not text:
            return ""
        if "\n" in text or "\r" in text or "\x00" in text:
            raise ValueError("stats_dir 不能包含换行或 NUL")
        if not text.startswith("/"):
            raise ValueError("stats_dir 必须是绝对路径（形如 <实例根>/world/stats）")
        if text.rstrip("/").rsplit("/", 1)[-1] != "stats":
            raise ValueError("stats_dir 必须以 stats 结尾（Minecraft 的 <实例根>/world/stats 目录）")
        return text.rstrip("/") or "/"

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("name 不能包含换行")
        return value

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("host 不能为空")
        if "://" in text or "/" in text:
            raise ValueError("host 只能是主机名或 IP，不能带协议或路径")
        if any(ch.isspace() for ch in text):
            raise ValueError("host 不能包含空白字符")
        if not _HOST_ALLOWED.match(text):
            raise ValueError("host 只能包含主机名/IP 合法字符")
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        try:
            return str(ipaddress.ip_address(text))
        except ValueError:
            pass
        if ":" in text:
            raise ValueError("host 不能包含端口（IPv6 请直接写地址，不要加 [ ] 以外的冒号端口）")
        # DNS 名：只做大小写/末尾点规范化，不做解析
        dns = text.rstrip(".").lower()
        if not dns or len(dns) > 253:
            raise ValueError("host 长度必须在 1-253 之间")
        if any(len(label) == 0 or len(label) > 63 for label in dns.split(".")):
            raise ValueError("host 存在空标签或超过 63 字符的标签")
        return dns

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        text = value.strip() or "/"
        if not text.startswith("/"):
            raise ValueError("path 必须以 / 开头")
        if "?" in text or "#" in text or " " in text:
            raise ValueError("path 不能包含查询串、片段或空格")
        return text

    @field_validator("self_name")
    @classmethod
    def _self_name(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("self_name 不能包含换行")
        return value

    @model_validator(mode="after")
    def _backoff(self) -> "ServerConfig":
        if self.reconnect_initial_seconds > self.reconnect_max_seconds:
            raise ValueError("reconnect_initial_seconds 不能大于 reconnect_max_seconds")
        return self

    @property
    def identity(self) -> tuple[str, int]:
        """规范化后的（host, port）唯一标识。"""
        return (self.host, self.port)

    @property
    def ws_uri(self) -> str:
        scheme = "wss" if self.tls else "ws"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}{self.path}"

    @property
    def handshake_self_name(self) -> str:
        return self.self_name or self.name


class BotConfig(_Model):
    app_id: str = Field(default="", max_length=64)
    client_secret: str = Field(default="", max_length=512)
    group_openid: str = Field(default="", max_length=128)
    api_base: str = "https://api.bot.qq.com"
    gateway_url: str = ""
    intents: int = Field(default=1 << 25, ge=0, le=(1 << 32) - 1)
    receive_mode: Literal["all", "mention"] = "all"
    proactive_enabled: bool = True
    send_rate_per_minute: int = Field(default=20, ge=1, le=600)
    request_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    token_refresh_skew_seconds: float = Field(default=60.0, ge=0, le=600)
    reconnect_initial_seconds: float = Field(default=1.0, gt=0, le=60)
    reconnect_max_seconds: float = Field(default=60.0, ge=1, le=600)
    max_message_chars: int = Field(default=1500, ge=50, le=4000)
    max_reply_parts: int = Field(default=5, ge=1, le=5)
    heartbeat_ack_timeout_seconds: float = Field(default=90.0, gt=0, le=600)
    # -- 事件队列（有界）与并发度 --
    # 队列满时不再接收新事件，也不会把未处理事件标记为已完成（见 qq_official 水位逻辑）
    event_queue_size: int = Field(default=64, ge=1, le=4096)
    event_workers: int = Field(default=4, ge=1, le=32)
    # -- 群内状态（bot_state）探测 --
    bot_state_probe_enabled: bool = True
    bot_state_interval_seconds: float = Field(default=300.0, ge=0, le=86400)
    # -- 主动消息被拒后的有界冷却（避免每条 MC 事件都去打被拒绝的接口）--
    proactive_cooldown_initial_seconds: float = Field(default=30.0, gt=0, le=600)
    proactive_cooldown_max_seconds: float = Field(default=600.0, gt=0, le=3600)

    @field_validator("api_base")
    @classmethod
    def _api_base(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        if not _URL_BASE.match(text):
            raise ValueError("api_base 必须形如 https://api.bot.qq.com（不含路径与结尾斜杠）")
        return text

    @model_validator(mode="after")
    def _cooldown_bounds(self) -> "BotConfig":
        if self.proactive_cooldown_initial_seconds > self.proactive_cooldown_max_seconds:
            raise ValueError(
                "proactive_cooldown_initial_seconds 不能大于 proactive_cooldown_max_seconds"
            )
        if self.reconnect_initial_seconds > self.reconnect_max_seconds:
            raise ValueError("reconnect_initial_seconds 不能大于 reconnect_max_seconds")
        return self

    @field_validator("gateway_url")
    @classmethod
    def _gateway(cls, value: str) -> str:
        text = value.strip()
        if text and not _WS_URL.match(text):
            raise ValueError("gateway_url 必须为空或 ws://、wss:// 地址")
        return text

    @field_validator("group_openid")
    @classmethod
    def _group(cls, value: str) -> str:
        if any(ch.isspace() for ch in value):
            raise ValueError("group_openid 不能包含空白字符")
        return value


class McToQqConfig(_Model):
    chat: bool = True
    death: bool = True
    join: bool = True
    leave: bool = True

    def enabled(self, event_type: str) -> bool:
        return bool(getattr(self, event_type, False))


class RelayConfig(_Model):
    qq_to_mc_enabled: bool = True
    mc_to_qq: McToQqConfig = Field(default_factory=McToQqConfig)
    list_command: str = "/list"
    dedupe_ttl_seconds: float = Field(default=120.0, gt=0, le=3600)
    dedupe_max_entries: int = Field(default=4096, ge=16, le=200_000)
    loop_guard_seconds: float = Field(default=5.0, gt=0, le=120)
    nickname_fallback: str = "QQ群友"
    image_placeholder: str = "[图片]"
    attachment_placeholder: str = "[附件]"
    max_nickname_chars: int = Field(default=32, ge=1, le=128)
    max_mc_message_chars: int = Field(default=2000, ge=50, le=8000)

    @field_validator("list_command")
    @classmethod
    def _list_command(cls, value: str) -> str:
        text = value.strip()
        if not text.startswith("/") or len(text) > 32 or any(ch.isspace() for ch in text):
            raise ValueError("list_command 必须是以 / 开头且不含空白的命令")
        return text


class StatusHttpConfig(_Model):
    enabled: bool = True
    host: str = "127.0.0.1"
    # 与 QueQiao 常见的 8080 默认端口错开，避免同机端口冲突
    port: int = Field(default=18080, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        text = value.strip()
        if not text or "/" in text or "://" in text:
            raise ValueError("runtime.status_http.host 只能是主机名或 IP")
        try:
            return str(ipaddress.ip_address(text))
        except ValueError:
            return text.rstrip(".").lower()


class StatsConfig(_Model):
    """排行榜持久化（SQLite）；只放数据库参数，数据源路径在 ``servers[].stats_dir``。"""

    # SQLite 单文件；必须落在 systemd 的 ReadWritePaths=/home/czauw/.local/share/mcqq-bridge 内
    db_path: str = "/home/czauw/.local/share/mcqq-bridge/stats.db"

    @field_validator("db_path")
    @classmethod
    def _db_path(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("stats.db_path 不能为空")
        if "\n" in text or "\r" in text or "\x00" in text:
            raise ValueError("stats.db_path 不能包含换行或 NUL")
        if not text.startswith("/"):
            raise ValueError("stats.db_path 必须是绝对路径")
        return text


class RuntimeConfig(_Model):
    status_http: StatusHttpConfig = Field(default_factory=StatusHttpConfig)
    shutdown_timeout_seconds: float = Field(default=10.0, gt=0, le=120)


class LoggingConfig(_Model):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: str = ""

    @field_validator("file")
    @classmethod
    def _file(cls, value: str) -> str:
        text = value.strip()
        if "\n" in text:
            raise ValueError("logging.file 不能包含换行")
        return text


class AppConfig(_Model):
    bot: BotConfig = Field(default_factory=BotConfig)
    servers: list[ServerConfig] = Field(default_factory=list)
    relay: RelayConfig = Field(default_factory=RelayConfig)
    stats: StatsConfig = Field(default_factory=StatsConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    @model_validator(mode="after")
    def _uniqueness(self) -> "AppConfig":
        if not self.servers:
            raise ValueError("servers 至少需要配置一个服务器")
        names: dict[str, int] = {}
        identities: dict[tuple[str, int], str] = {}
        for index, server in enumerate(self.servers):
            if server.name in names:
                raise ValueError(
                    f"servers[{index}].name 与 servers[{names[server.name]}] 重复：{server.name}"
                )
            names[server.name] = index
            if server.identity in identities:
                raise ValueError(
                    "servers[{0}]（{1}） 与「{2}」的 host+port 重复：{3}:{4}（禁用项也参与唯一性校验）".format(
                        index, server.name, identities[server.identity], server.host, server.port
                    )
                )
            identities[server.identity] = server.name
        return self

    def server(self, name: str) -> ServerConfig | None:
        for server in self.servers:
            if server.name == name:
                return server
        return None

    def enabled_servers(self) -> list[ServerConfig]:
        return [server for server in self.servers if server.enabled]

    def runtime_issues(self) -> list[str]:
        """真正运行前必须满足的条件（--check-config 不检查这些）。"""
        issues: list[str] = []
        if not self.bot.app_id:
            issues.append("bot.app_id 未配置")
        if not self.bot.client_secret:
            issues.append("bot.client_secret 未配置（可用 ${QQ_BOT_CLIENT_SECRET} 引用环境变量）")
        if not self.bot.group_openid:
            issues.append("bot.group_openid 未配置（群 OpenID，不是群号）")
        if not self.enabled_servers():
            issues.append("servers 中没有启用的服务器，运行时不会连接任何 QueQiao")
        return issues


def _interpolate_env(raw: Any, path: str, issues: list[str], missing: list[str]) -> Any:
    """递归解析 ``${ENV}`` 引用；未设置时记录字段路径，不把空值写进错误里当成功。"""
    if isinstance(raw, dict):
        return {
            key: _interpolate_env(value, f"{path}.{key}" if path else str(key), issues, missing)
            for key, value in raw.items()
        }
    if isinstance(raw, list):
        return [
            _interpolate_env(value, f"{path}[{index}]", issues, missing)
            for index, value in enumerate(raw)
        ]
    if isinstance(raw, str):
        match = _ENV_REF.match(raw.strip())
        if match:
            name = match.group(1)
            value = os.environ.get(name, "")
            if not value:
                missing.append(f"{path} 引用的环境变量 {name} 未设置或为空")
            return value
    return raw


def _flatten_pydantic_errors(exc: Exception) -> list[str]:
    issues: list[str] = []
    errors = getattr(exc, "errors", None)
    if callable(errors):
        for item in errors():
            detail = dict(item) if isinstance(item, dict) else {}
            location = ".".join(str(part) for part in detail.get("loc", ())) or "<root>"
            issues.append(f"{location}: {detail.get('msg', 'invalid value')}")
    else:  # pragma: no cover - pydantic 之外的异常兜底
        issues.append(str(exc))
    return issues


def load_config(path: str | Path, *, require_secrets: bool = True) -> AppConfig:
    """加载并校验 YAML 配置。

    ``require_secrets=False`` 用于 ``--check-config``：只做结构与唯一性校验，
    不要求 app_id/client_secret/group_openid 就绪，也不会发起任何网络请求。
    """
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError([f"配置文件不存在：{config_path}"]) from exc
    except OSError as exc:
        raise ConfigError([f"无法读取配置文件 {config_path}：{exc.strerror or exc}"]) from exc

    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        marker = getattr(exc, "problem_mark", None)
        where = f"第 {marker.line + 1} 行" if marker is not None else "未知位置"
        raise ConfigError([f"YAML 解析失败（{where}）：{getattr(exc, 'problem', exc)}"]) from exc

    if raw is None:
        raise ConfigError(["配置文件为空"])
    if not isinstance(raw, dict):
        raise ConfigError(["配置顶层必须是映射（mapping）"])

    missing: list[str] = []
    issues: list[str] = []
    data = _interpolate_env(raw, "", issues, missing)

    try:
        config = AppConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(_flatten_pydantic_errors(exc)) from exc

    if require_secrets:
        problems = list(missing)
        for issue in config.runtime_issues():
            field = issue.split(" ", 1)[0]
            # 同一字段已因“环境变量为空”报过，就不要重复报“未配置”
            if any(field and field in detail for detail in missing):
                continue
            problems.append(issue)
        if problems:
            raise ConfigError(problems)
    return config


def setup_logging(level: str, file_path: str = "") -> None:
    """统一日志配置；不输出任何配置内容或密钥。"""
    import logging as _logging

    handlers: list[_logging.Handler] = [_logging.StreamHandler()]
    if file_path:
        target = Path(file_path).expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(_logging.FileHandler(target, encoding="utf-8"))
    _logging.basicConfig(
        level=getattr(_logging, level, _logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )

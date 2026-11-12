"""启动入口：``python -m mcqq_bridge --config config.yaml``。

* ``--check-config``：只做 YAML 结构与唯一性校验，不发起任何网络请求，
  不要求 app_id/client_secret/group_openid 已就绪；失败返回非零。
* 正常运行前会再校验一次密钥/群 OpenID，缺失时只输出字段路径并退出。
* 本模块只使用官方 QQ 接口 + QueQiao WebSocket，不含 OneBot 回退路径。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import signal
import sys
from pathlib import Path

from .config import ConfigError, AppConfig, load_config, setup_logging
from .runtime import BridgeRuntime

__all__ = ["main"]

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mcqq_bridge",
        description="QQ 官方机器人 × Minecraft QueQiao 多服务器互通",
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="YAML 配置文件路径（默认 config.yaml）",
    )
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="只校验配置（不联网、不需要真实密钥），失败返回非零",
    )
    parser.add_argument(
        "--env-file",
        default="",
        help=(
            "额外加载的 KEY=VALUE 环境变量文件（如 .env）；"
            "本程序默认不会自动读取 .env，只有显式指定才加载"
        ),
    )
    return parser


def load_env_file(path: str | Path) -> list[str]:
    """极简 ``.env`` 解析：``KEY=VALUE``，忽略注释/空行，已存在的变量不覆盖。

    只支持最基本语法（可选 ``export`` 前缀、成对引号），不做变量展开。
    返回问题列表（空列表表示成功）。
    """
    target = Path(path)
    try:
        text = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [f"env 文件不存在：{target}"]
    except OSError as exc:
        return [f"无法读取 env 文件 {target}：{exc.strerror or exc}"]

    issues: list[str] = []
    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            issues.append(f"env 文件第 {line_no} 行不是 KEY=VALUE 形式")
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _ENV_KEY.match(key):
            issues.append(f"env 文件第 {line_no} 行变量名不合法")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return issues


def _print_issues(issues: list[str]) -> None:
    print("配置不可用：", file=sys.stderr)
    for issue in issues:
        print(f"  - {issue}", file=sys.stderr)


def _check_config(path: str) -> int:
    try:
        config = load_config(path, require_secrets=False)
    except ConfigError as exc:
        _print_issues(exc.issues)
        return 2

    print(
        f"配置校验通过：{len(config.servers)} 个服务器"
        f"（{len(config.enabled_servers())} 个启用）"
    )
    for server in config.servers:
        state = "启用" if server.enabled else "禁用"
        print(
            f"  - {server.name}: {server.host}:{server.port}{server.path}"
            f" [{state}] 查询={server.players.mode} tls={server.tls}"
        )
    missing = config.runtime_issues()
    if missing:
        print("真正运行前还需要补齐：")
        for issue in missing:
            print(f"  - {issue}")
    return 0


async def _run(config: AppConfig) -> None:
    setup_logging(config.logging.level, config.logging.file)
    runtime = BridgeRuntime(config)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass
    await runtime.start()
    try:
        await stop_event.wait()
    finally:
        await runtime.stop()


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = Path(args.config)
    if args.env_file:
        env_issues = load_env_file(args.env_file)
        if env_issues:
            _print_issues(env_issues)
            return 2
    if args.check_config:
        return _check_config(str(path))
    try:
        config = load_config(path, require_secrets=True)
    except ConfigError as exc:
        _print_issues(exc.issues)
        return 2
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:  # pragma: no cover
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

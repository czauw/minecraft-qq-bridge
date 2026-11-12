"""运行时：状态接口、CLI 校验入口、优雅启停。"""

from __future__ import annotations

import httpx
import pytest
import yaml

from helpers import make_config, server_entry
from mcqq_bridge.config import load_config
from mcqq_bridge.__main__ import main
from mcqq_bridge.runtime import BridgeRuntime, create_status_app


class FakeRuntime:
    def __init__(self, config, qq_connected=True, connected_count=2) -> None:
        self.config = config
        self.qq = type("QQ", (), {"connected": qq_connected})()
        self.manager = type("MGR", (), {"connected_count": connected_count})()
        self._payload = {"qq": {"connected": qq_connected}, "servers": []}

    def status_payload(self):
        return self._payload


async def get(app, path):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


async def test_status_endpoints_report_readiness():
    config = make_config(
        servers=[server_entry("主服", 18080), server_entry("禁用", 18081, enabled=False)]
    )
    app = create_status_app(FakeRuntime(config, qq_connected=True, connected_count=1))
    assert (await get(app, "/healthz")).json() == {"status": "ok"}
    ready = (await get(app, "/readyz")).json()
    assert ready == {
        "status": "ready",
        "qq_connected": True,
        "servers_enabled": 1,
        "servers_connected": 1,
        "qq_degraded": [],
    }
    assert (await get(app, "/api/v1/status")).json() == {"qq": {"connected": True}, "servers": []}


async def test_readyz_degraded_without_connections():
    config = make_config(servers=[server_entry("主服", 18080)])
    app = create_status_app(FakeRuntime(config, qq_connected=False, connected_count=0))
    ready = (await get(app, "/readyz")).json()
    assert ready["status"] == "degraded"


async def test_runtime_start_and_stop_are_idempotent(monkeypatch):
    config = make_config(servers=[server_entry("主服", 18080)])
    runtime = BridgeRuntime(config)
    started = {"qq": 0, "manager": 0}
    stopped = {"qq": 0, "manager": 0}

    async def qq_start():
        started["qq"] += 1

    async def qq_close():
        stopped["qq"] += 1

    async def manager_start():
        started["manager"] += 1

    async def manager_stop():
        stopped["manager"] += 1

    monkeypatch.setattr(runtime.qq, "start", qq_start)
    monkeypatch.setattr(runtime.qq, "close", qq_close)
    monkeypatch.setattr(runtime.manager, "start", manager_start)
    monkeypatch.setattr(runtime.manager, "stop", manager_stop)

    await runtime.start()
    await runtime.start()
    await runtime.stop()
    await runtime.stop()
    assert started == {"qq": 1, "manager": 1}
    assert stopped == {"qq": 1, "manager": 1}


def write(tmp_path, data):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


def base_dict():
    return {
        "bot": {"app_id": "1", "client_secret": "s", "group_openid": "g"},
        "servers": [server_entry("主服", 18080)],
        "runtime": {"status_http": {"enabled": False}},
    }


def test_cli_check_config_ok(tmp_path):
    path = write(tmp_path, base_dict())
    assert main(["--config", path, "--check-config"]) == 0


def test_cli_check_config_rejects_duplicates(tmp_path):
    data = base_dict()
    data["servers"] = [server_entry("主服", 18080), server_entry("主服", 18081)]
    assert main(["--config", write(tmp_path, data), "--check-config"]) == 2


def test_cli_check_config_does_not_require_secrets(tmp_path, monkeypatch):
    monkeypatch.delenv("MCQQ_TEST_SECRET", raising=False)
    data = base_dict()
    data["bot"] = {"app_id": "", "client_secret": "${MCQQ_TEST_SECRET}", "group_openid": ""}
    assert main(["--config", write(tmp_path, data), "--check-config"]) == 0


def test_cli_missing_config_returns_nonzero(tmp_path):
    assert main(["--config", str(tmp_path / "none.yaml"), "--check-config"]) == 2


def test_load_config_strict_missing_secret(tmp_path):
    data = base_dict()
    data["bot"] = {"app_id": "", "client_secret": "", "group_openid": ""}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(Exception):
        load_config(path, require_secrets=True)


# --- .env 加载与 degraded 暴露 -------------------------------------------------


def test_env_file_is_actually_loaded(tmp_path, monkeypatch):
    monkeypatch.delenv("MCQQ_ENV_SECRET", raising=False)
    data = base_dict()
    data["bot"] = {"app_id": "1", "client_secret": "${MCQQ_ENV_SECRET}", "group_openid": "g"}
    config_path = write(tmp_path, data)
    env_path = tmp_path / ".env"
    env_path.write_text("# 注释\nMCQQ_ENV_SECRET='from-dotenv'\n", encoding="utf-8")

    # 不指定 --env-file 时不会自动读取 .env：运行前校验应因缺密钥失败
    assert main(["--config", config_path, "--check-config"]) == 0
    with pytest.raises(Exception):
        load_config(config_path, require_secrets=True)

    assert main(["--config", config_path, "--check-config", "--env-file", str(env_path)]) == 0
    assert load_config(config_path, require_secrets=True).bot.client_secret == "from-dotenv"


def test_env_file_problems_return_nonzero(tmp_path):
    config_path = write(tmp_path, base_dict())
    assert main(["--config", config_path, "--check-config", "--env-file", str(tmp_path / "nope.env")]) == 2
    broken = tmp_path / "broken.env"
    broken.write_text("NOT_A_PAIR\n", encoding="utf-8")
    assert main(["--config", config_path, "--check-config", "--env-file", str(broken)]) == 2


async def test_readyz_exposes_qq_degraded():
    config = make_config(servers=[server_entry("主服", 18080)])

    class DegradedQQ:
        connected = True
        degraded_reasons = lambda self: ["proactive_state_unknown"]  # noqa: E731

    runtime = FakeRuntime(config, qq_connected=True, connected_count=1)
    runtime.qq = DegradedQQ()
    app = create_status_app(runtime)
    ready = (await get(app, "/readyz")).json()
    assert ready["status"] == "degraded"
    assert ready["qq_degraded"] == ["proactive_state_unknown"]

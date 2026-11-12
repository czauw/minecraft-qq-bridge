"""严格 YAML 配置：结构、唯一性、密钥引用与错误信息。"""

from __future__ import annotations

import pytest
import yaml

from mcqq_bridge.config import ConfigError, load_config

from helpers import bot_entry, server_entry

BASE = {
    "bot": bot_entry(client_secret="TOP-SECRET-VALUE"),
    "servers": [server_entry("主服", 18080)],
    "runtime": {"status_http": {"enabled": False}},
}


def write_config(tmp_path, data, name="config.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_minimal_config_loads(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    assert len(config.servers) == 1
    assert config.servers[0].name == "主服"
    assert config.bot.group_openid == "GROUP_OPENID_1"
    assert config.runtime_issues() == []


def test_unknown_field_rejected(tmp_path):
    data = {**BASE, "servers": [server_entry("主服", 18080, extra_field=1)]}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "extra_field" in " ".join(excinfo.value.issues)


def test_unknown_top_level_key_rejected(tmp_path):
    data = {**BASE, "onebot": {"enabled": True}}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "onebot" in " ".join(excinfo.value.issues)


def test_duplicate_name_rejected(tmp_path):
    data = {**BASE, "servers": [server_entry("主服", 18080), server_entry("主服", 18081)]}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "重复" in " ".join(excinfo.value.issues)


def test_duplicate_host_port_rejected_even_when_disabled(tmp_path):
    data = {
        **BASE,
        "servers": [
            server_entry("主服", 18080),
            server_entry("旧服", 18080, enabled=False),
        ],
    }
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    message = " ".join(excinfo.value.issues)
    assert "host+port" in message and "旧服" in message


def test_same_host_different_port_allowed(tmp_path):
    data = {**BASE, "servers": [server_entry("A", 18080), server_entry("B", 18081)]}
    config = load_config(write_config(tmp_path, data), require_secrets=False)
    assert [s.port for s in config.servers] == [18080, 18081]


def test_different_host_same_port_allowed(tmp_path):
    data = {
        **BASE,
        "servers": [
            server_entry("A", 18080),
            server_entry("B", 18080, host="192.168.10.30"),
        ],
    }
    config = load_config(write_config(tmp_path, data), require_secrets=False)
    assert [s.host for s in config.servers] == ["127.0.0.1", "192.168.10.30"]


def test_ipv6_equivalent_hosts_are_duplicate(tmp_path):
    data = {
        **BASE,
        "servers": [
            server_entry("A", 18080, host="::1"),
            server_entry("B", 18080, host="0:0:0:0:0:0:0:1"),
        ],
    }
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "host+port" in " ".join(excinfo.value.issues)


def test_host_with_scheme_or_path_rejected(tmp_path):
    for host in ("http://127.0.0.1", "127.0.0.1/mc", "127.0.0.1:8080"):
        data = {**BASE, "servers": [server_entry("主服", 18080, host=host)]}
        with pytest.raises(ConfigError):
            load_config(write_config(tmp_path, data), require_secrets=False)


def test_invalid_port_and_path_rejected(tmp_path):
    data = {**BASE, "servers": [server_entry("主服", 70000)]}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data), require_secrets=False)

    data = {**BASE, "servers": [server_entry("主服", 18080, path="ws")]}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data), require_secrets=False)


def test_name_with_newline_rejected(tmp_path):
    data = {**BASE, "servers": [server_entry("主\n服", 18080)]}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data), require_secrets=False)


def test_backoff_order_rejected(tmp_path):
    data = {
        **BASE,
        "servers": [
            server_entry(
                "主服", 18080, reconnect_initial_seconds=30, reconnect_max_seconds=5
            )
        ],
    }
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data), require_secrets=False)


def test_player_mode_validated(tmp_path):
    data = {**BASE, "servers": [server_entry("主服", 18080, players={"mode": "sql"})]}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data), require_secrets=False)


def test_env_reference_structure_passes_but_runtime_requires_it(tmp_path, monkeypatch):
    monkeypatch.delenv("MCQQ_TEST_SECRET", raising=False)
    data = {
        "bot": bot_entry(client_secret="${MCQQ_TEST_SECRET}", app_id="", group_openid=""),
        "servers": [server_entry("主服", 18080)],
        "runtime": {"status_http": {"enabled": False}},
    }
    path = write_config(tmp_path, data)
    config = load_config(path, require_secrets=False)
    assert config.bot.client_secret == ""
    issues = config.runtime_issues()
    assert any("client_secret" in issue for issue in issues)
    assert any("app_id" in issue for issue in issues)
    assert any("group_openid" in issue for issue in issues)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, require_secrets=True)
    assert "MCQQ_TEST_SECRET" in " ".join(excinfo.value.issues)


def test_env_reference_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("MCQQ_TEST_SECRET", "from-env")
    data = {**BASE, "bot": bot_entry(client_secret="${MCQQ_TEST_SECRET}")}
    config = load_config(write_config(tmp_path, data), require_secrets=True)
    assert config.bot.client_secret == "from-env"


def test_secret_never_echoed_in_errors(tmp_path):
    data = {"bot": bot_entry(client_secret="TOP-SECRET-VALUE", api_base="ftp://bad")}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "TOP-SECRET-VALUE" not in " ".join(excinfo.value.issues)


def test_missing_file_and_bad_yaml(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope.yaml", require_secrets=False)
    path = tmp_path / "bad.yaml"
    path.write_text("bot: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path, require_secrets=False)
    assert "YAML" in " ".join(excinfo.value.issues)


def test_empty_servers_rejected(tmp_path):
    data = {**BASE, "servers": []}
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, data), require_secrets=False)


def test_all_disabled_reports_runtime_issue(tmp_path):
    data = {**BASE, "servers": [server_entry("主服", 18080, enabled=False)]}
    config = load_config(write_config(tmp_path, data), require_secrets=False)
    assert any("没有启用的服务器" in issue for issue in config.runtime_issues())


def test_ws_uri_and_self_name(tmp_path):
    data = {
        **BASE,
        "servers": [
            server_entry("主服", 18080, tls=True, path="/mc", self_name="ServerA"),
        ],
    }
    config = load_config(write_config(tmp_path, data), require_secrets=False)
    server = config.servers[0]
    assert server.ws_uri == "wss://127.0.0.1:18080/mc"
    assert server.handshake_self_name == "ServerA"
    assert server.ws_uri.count("://") == 1


# --- M9：严格 bool/int/float 校验（拒绝隐式类型转换） ---------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("enabled", "yes"),
        ("enabled", "true"),
        ("enabled", 1),
        ("enabled", 0),
        ("tls", 1),
        ("tls", "false"),
        ("port", True),
        ("port", "18080"),
        ("port", 18080.0),
        ("connect_timeout_seconds", "10"),
        ("connect_timeout_seconds", True),
    ],
)
def test_server_fields_reject_wrong_types(tmp_path, field, value):
    entry = server_entry("主服", 18080)
    entry[field] = value
    data = {**BASE, "servers": [entry]}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    message = " ".join(excinfo.value.issues)
    assert field in message
    assert "18080" not in message or field != "port"


@pytest.mark.parametrize(
    "field,value",
    [
        ("proactive_enabled", "true"),
        ("proactive_enabled", 1),
        ("intents", 33554432.0),
        ("intents", "33554432"),
        ("request_timeout_seconds", "10"),
        ("send_rate_per_minute", 20.5),
        ("max_reply_parts", 6),
        ("event_queue_size", 0),
        ("bot_state_probe_enabled", "yes"),
        ("proactive_cooldown_initial_seconds", 0),
    ],
)
def test_bot_fields_reject_wrong_types_or_ranges(tmp_path, field, value):
    data = {**BASE, "bot": bot_entry(**{field: value})}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert field in " ".join(excinfo.value.issues)


def test_int_accepted_where_float_expected(tmp_path):
    """契约要求：超时等 float 字段允许写整数（YAML 里 10 很自然），但反过来不行。"""
    data = {
        **BASE,
        "bot": bot_entry(request_timeout_seconds=5, proactive_cooldown_initial_seconds=2),
        "servers": [server_entry("主服", 18080, connect_timeout_seconds=3)],
    }
    config = load_config(write_config(tmp_path, data), require_secrets=False)
    assert config.bot.request_timeout_seconds == 5.0
    assert config.servers[0].connect_timeout_seconds == 3.0
    assert isinstance(config.bot.request_timeout_seconds, float)


def test_type_errors_do_not_echo_values(tmp_path):
    canary = "CANARY_TYPE_VALUE"
    data = {**BASE, "servers": [server_entry("主服", 18080, enabled=canary)]}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    issues = " ".join(excinfo.value.issues)
    assert "enabled" in issues
    assert canary not in issues


def test_bot_cooldown_bounds_rejected(tmp_path):
    data = {
        **BASE,
        "bot": bot_entry(
            proactive_cooldown_initial_seconds=600, proactive_cooldown_max_seconds=30
        ),
    }
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "proactive_cooldown" in " ".join(excinfo.value.issues)


# --- M9：排行榜配置（stats_dir / stats.db_path）--------------------------------


def test_stats_defaults_preserve_existing_config(tmp_path):
    config = load_config(write_config(tmp_path, BASE))
    # 旧配置（不含 stats、不含 stats_dir）必须照旧可用，默认不采集
    assert config.servers[0].stats_dir == ""
    assert config.stats.db_path == "/home/czauw/.local/share/mcqq-bridge/stats.db"
    assert config.stats.db_path.startswith("/")


def test_stats_dir_accepts_absolute_world_stats_path(tmp_path):
    data = {
        **BASE,
        "servers": [server_entry("主服", 18080, stats_dir="/opt/mcsmanager/x/world/stats/")],
    }
    config = load_config(write_config(tmp_path, data))
    # 末尾斜杠被规范化，便于用 parent.parent 推导 usercache.json
    assert config.servers[0].stats_dir == "/opt/mcsmanager/x/world/stats"


@pytest.mark.parametrize(
    "bad",
    [
        "world/stats",  # 相对路径
        "/opt/mcsmanager/x/world/",  # 末级不是 stats
        "/opt/mcsmanager/x/world/stats\n/evil",  # 内嵌换行
        "/opt/mcsmanager/x/world/data",
    ],
)
def test_stats_dir_rejects_relative_or_non_stats_paths(tmp_path, bad):
    data = {**BASE, "servers": [server_entry("主服", 18080, stats_dir=bad)]}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "stats_dir" in " ".join(excinfo.value.issues)


def test_stats_db_path_must_be_absolute(tmp_path):
    data = {**BASE, "stats": {"db_path": "stats.db"}}
    with pytest.raises(ConfigError) as excinfo:
        load_config(write_config(tmp_path, data), require_secrets=False)
    assert "db_path" in " ".join(excinfo.value.issues)


def test_stats_path_can_be_missing_at_check_config(tmp_path):
    from mcqq_bridge.__main__ import main

    data = {
        **BASE,
        "servers": [server_entry("主服", 18080, stats_dir="/nonexistent/instance/world/stats")],
        "stats": {"db_path": str(tmp_path / "not-created-yet" / "stats.db")},
    }
    path = write_config(tmp_path, data)
    # 结构校验不要求目录/数据库已存在
    assert main(["--config", str(path), "--check-config"]) == 0

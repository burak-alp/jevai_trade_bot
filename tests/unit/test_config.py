import pytest

from jevbot.core.config import AppConfig, ConfigError, load_config


def test_base_config_loads():
    cfg = load_config(["config/base.yaml"])
    assert isinstance(cfg, AppConfig)
    assert cfg.binance.ws.stream_routes["kline"] == "market"
    assert cfg.binance.ws.stream_routes["bookTicker"] == "public"
    assert cfg.recorder.depth.symbols_always == ["BTCUSDT", "ETHUSDT"]
    assert isinstance(cfg.sink.rotate_s, float)


def test_unknown_key_rejected(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("recorder:\n  klines_1m: true\n")
    with pytest.raises(ConfigError, match="unknown key"):
        load_config(["config/base.yaml", str(bad)])


def test_overlay_and_set(tmp_path):
    over = tmp_path / "o.yaml"
    over.write_text("binance:\n  ws:\n    base: ws://127.0.0.1:9999\n")
    cfg = load_config(["config/base.yaml", str(over)], ["recorder.depth.top_n_by_volume=3", "universe.exclude=[A,B]"])
    assert cfg.binance.ws.base == "ws://127.0.0.1:9999"
    assert cfg.binance.ws.routes["market"] == "/market"          # deep merge kept siblings
    assert cfg.recorder.depth.top_n_by_volume == 3
    assert cfg.universe.exclude == ["A", "B"]


def test_type_errors():
    with pytest.raises(ConfigError):
        load_config(["config/base.yaml"], ["sink.flush_rows=abc"])


def test_config_hash_stable():
    a = load_config(["config/base.yaml"])
    b = load_config(["config/base.yaml"])
    assert a.config_hash() == b.config_hash()
    c = load_config(["config/base.yaml"], ["sink.rotate_s=60"])
    assert c.config_hash() != a.config_hash()

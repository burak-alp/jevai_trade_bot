import pytest_asyncio

from jevbot.core.config import load_config
from jevbot.testing.fake_binance import FakeBinance, FakeConfig


@pytest_asyncio.fixture
async def fake():
    fb = FakeBinance(FakeConfig(n_symbols=20, n_extra_symbols=5, book_rate_total=300.0, liquidation_rate=5.0))
    await fb.start()
    yield fb
    await fb.stop()


def make_cfg(fake, tmp_path, *overrides):
    return load_config(["config/base.yaml"], [
        f"data_dir={tmp_path / 'data'}", f"run_dir={tmp_path / 'run'}",
        f"binance.rest_base={fake.rest_base}", f"binance.ws.base={fake.ws_base}",
        "logging.json=false", "recorder.smoke.first_event_timeout_s=5", "recorder.oi.cycle_s=2",
        "recorder.health_interval_s=1", "recorder.depth.top_n_by_volume=3", "recorder.depth.sample_interval_s=1",
        "sink.flush_s=0.5", "sink.rotate_s=2", *overrides])

from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from jevbot.marketdata.binance_ws import close_reason


def test_close_reason_separates_remote_local_and_tcp_drops():
    assert close_reason(ConnectionClosedError(Close(1008, "x"), None)) == "closed:1008"
    assert close_reason(ConnectionClosedError(None, Close(1011, "keepalive ping timeout"))) == "closed:local_1011"
    assert close_reason(ConnectionClosedError(None, None)) == "closed:nocode"

import os
import ssl
import sys

import pytest

from jevbot.core.tls import ensure_system_trust


@pytest.mark.skipif(sys.platform != "win32", reason="Windows certificate store")
def test_bundle_includes_certifi_and_windows_roots(monkeypatch, tmp_path):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    path = ensure_system_trust()
    assert path and os.environ["SSL_CERT_FILE"] == path
    ctx = ssl.create_default_context(cafile=path)                    # loads and parses
    assert len(ctx.get_ca_certs()) > 100
    monkeypatch.setenv("SSL_CERT_FILE", "custom.pem")
    assert ensure_system_trust() is None and os.environ["SSL_CERT_FILE"] == "custom.pem"

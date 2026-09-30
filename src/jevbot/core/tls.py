"""Trust the Windows certificate store in addition to certifi.

Why: antivirus "HTTPS scanning" (Avast Web/Mail Shield, ESET, Kaspersky...) re-signs every TLS connection with its own
root certificate, which Windows trusts but Python/httpx (certifi bundle) does not -> ``CERTIFICATE_VERIFY_FAILED:
unable to get local issuer certificate`` on every request (seen 2026-09-30 after a Windows update). We build one PEM
bundle (certifi + Windows ROOT/CA stores) and point ``SSL_CERT_FILE`` at it; httpx and websockets both honour it.
Certificate verification stays ON. A user-set ``SSL_CERT_FILE`` is never overridden.
"""

from __future__ import annotations

import os
import ssl
import sys
import tempfile
from pathlib import Path


def ensure_system_trust() -> str | None:
    """Returns the bundle path when the environment was changed, else None."""
    if sys.platform != "win32" or os.environ.get("SSL_CERT_FILE"):
        return None
    try:
        import certifi
        pems = [Path(certifi.where()).read_text(encoding="utf-8")]
        for store in ("ROOT", "CA"):
            for der, enc, _trust in ssl.enum_certificates(store):
                if enc == "x509_asn":
                    pems.append(ssl.DER_cert_to_PEM_cert(der))
        out = Path(tempfile.gettempdir()) / "jevbot-ca-bundle.pem"
        text = "\n".join(pems)
        if not out.exists() or out.read_text(encoding="utf-8") != text:
            out.write_text(text, encoding="utf-8")
        os.environ["SSL_CERT_FILE"] = str(out)
        return str(out)
    except (OSError, ImportError, ssl.SSLError):
        return None

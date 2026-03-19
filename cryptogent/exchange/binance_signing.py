from __future__ import annotations

import hmac
import hashlib


def hmac_sha256_hex(*, secret: str, payload: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


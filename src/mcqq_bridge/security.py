from __future__ import annotations

import hmac


def bearer_matches(header: str | None, expected: str) -> bool:
    if not header or not expected or not header.startswith("Bearer "):
        return False
    supplied = header.removeprefix("Bearer ")
    return hmac.compare_digest(supplied.encode(), expected.encode())

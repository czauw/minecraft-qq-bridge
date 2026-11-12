"""日志/状态脱敏工具。

约束（见 docs/VERIFICATION.md M8）：QQ/QueQiao 的日志与 ``/api/v1/status``
只允许出现「受控错误种类 / 业务码 / HTTP 状态 / 本地上下文」，不得写入上游
原始 ``message``、关闭原因、消息正文，也不得写入任何凭据（token、密钥、
带 userinfo 的 URL 参数）。
"""

from __future__ import annotations

import re

__all__ = ["error_kind", "scrub"]

_BEARER = re.compile(r"(?i)\b(bearer|qqbot)\s+[A-Za-z0-9._~+/=\-]{4,}")
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*://)[^/\s:@]+(?::[^/\s@]*)?@")
_SECRET_PARAM = re.compile(
    r"(?i)\b(access_token|client_secret|password|secret|token)=([^&\s,;]+)"
)
_SECRET_PAIR = re.compile(
    r"(?i)\b(access_token|client_secret|password|secret|token)\b[\"']?\s*[:=]\s*[\"'][^\"']*[\"']"
)


def scrub(text: object, limit: int = 200) -> str:
    """把任意字符串压成单行并抹掉常见凭据形态。

    只用于**本模块自己构造**的错误文本（业务码/状态码等）；上游原文根本不会
    进入日志，这里是第二道防线。
    """
    value = " ".join(str(text).split())
    value = _BEARER.sub(lambda match: f"{match.group(1)} ***", value)
    value = _URL_USERINFO.sub(lambda match: f"{match.group(1)}***@", value)
    value = _SECRET_PAIR.sub(lambda match: f"{match.group(1)}=***", value)
    value = _SECRET_PARAM.sub(lambda match: f"{match.group(1)}=***", value)
    return value[:limit]


def error_kind(exc: BaseException) -> str:
    """只返回异常类型名，不返回 ``str(exc)``（后者可能含上游原文/URL 凭据）。"""
    return type(exc).__name__

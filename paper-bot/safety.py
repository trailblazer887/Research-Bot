"""出站内容消毒与异常脱敏。

Qmsg 会对消息做内容检测：URL、IPv4、连续数字属于绝对禁止内容，命中
可能直接封号。LLM 摘要、错误通知都可能带出这些内容，所以发送前统一
过一遍 ``sanitize_for_qmsg``。

``safe_error`` 则负责把异常压成“类型 + HTTP 状态 + host”，不包含
URL path/query（Qmsg 的 key 就在 path 里）和异常原文。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# 顺序很重要：先去掉 URL（避免 key 留在 path 里），再处理域名/IP/长数字。
URL_RE = re.compile(r"(?:(?:https?|ftp)://|www\.)[^\s<>\"']+", re.IGNORECASE)
DOMAIN_RE = re.compile(
    r"\b(?:[a-z0-9-]+\.)+(?:com|cn|org|net|io|edu|gov|me|xyz|top|co|dev|ai)\b"
    r"(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Qmsg 的“连续数字”主要针对 QQ 号/手机号/长 ID；3 位以内的正常数字
# （125 篇、4.5 分、23%）在现有消息里可以正常送达，阈值取 5 位。
LONG_DIGITS_RE = re.compile(r"\d{5,}")

RULES = (
    (URL_RE, "[链接]"),
    (DOMAIN_RE, "[链接]"),
    (IPV4_RE, "[IP]"),
    (LONG_DIGITS_RE, "[数字]"),
)


class SafeError(RuntimeError):
    """消息本身已经过脱敏，可以安全地发给用户或写进仓库。"""


def sanitize_for_qmsg(text: str) -> str:
    """替换 Qmsg 绝对禁止的 URL / IPv4 / 长数字串。"""
    cleaned = text or ""
    for pattern, replacement in RULES:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def safe_error(exc: BaseException) -> str:
    """返回不含 URL path/query、不含异常原文的简短错误描述。"""
    if isinstance(exc, SafeError):
        return str(exc)[:160]

    name = type(exc).__name__
    response = getattr(exc, "response", None)
    request = getattr(exc, "request", None)
    status = getattr(response, "status_code", None)
    url = getattr(request, "url", "") or getattr(response, "url", "")
    host = ""
    if url:
        try:
            host = urlparse(url).hostname or ""
        except ValueError:
            host = ""

    parts = [name]
    if status:
        parts.append(f"HTTP {status}")
    if host:
        parts.append(f"host={host}")
    if isinstance(exc, TimeoutError) or "Timeout" in name:
        parts.append("timeout")
    return " ".join(parts)[:160]

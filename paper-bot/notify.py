"""消息渲染与 Qmsg 推送。"""

from __future__ import annotations

import time
from typing import Any

import requests

from safety import SafeError, safe_error, sanitize_for_qmsg

LABELS = [
    ("problem", "问题"),
    ("core_idea", "核心"),
    ("evidence", "结果"),
    ("relevance", "与你"),
]


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(
    papers: list[dict[str, Any]],
    stats: dict[str, Any],
    weekly: str | None = None,
    include_url: bool = True,
) -> str:
    lines = [f"【沙箱内存·每日 {len(papers)} 篇】{stats['date_cn']}"]
    lines.append(
        f"今日候选 {stats['fetched']} → 相关 {stats['kept']} → 推送 {len(papers)}"
    )
    lines.append("")

    for index, paper in enumerate(papers, 1):
        summary = paper.get("summary", {})
        venue = f"[{paper['venue_label']}] " if paper.get("venue_label") else ""
        backfill = "（库存）" if paper.get("backfill") else ""
        lines.append(f"{index}) {venue}{_truncate(paper['title'], 110)}{backfill}")

        for key, label in LABELS:
            value = str(summary.get(key, "")).strip()
            if value:
                lines.append(f"   {label}：{_truncate(value, 150)}")

        terms = summary.get("terms") or []
        read_level = summary.get("read_level", "")
        tail = "｜".join(part for part in [read_level, ", ".join(terms)] if part)
        if tail:
            lines.append(f"   读法：{tail}")
        if summary.get("confidence") == "low":
            lines.append("   ⚠️ 摘要信息有限，建议看原文")
        if include_url:
            lines.append(f"   {paper['url']}")
        lines.append("")

    if weekly:
        lines.append(weekly)
        lines.append("")

    if not include_url and papers:
        lines.append("（Qmsg 不允许 URL；复制标题到 arXiv / Google Scholar 可找到原文）")

    lines.append(f"————\n库存 {stats['pool']} 篇｜累计 {stats['total']} 篇")
    return "\n".join(lines)


def split_message(text: str, max_chars: int) -> list[str]:
    """按空行切块，尽量在论文边界拆；单块超长再硬切。"""
    if len(text) <= max_chars:
        return [text]

    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    chunks: list[str] = []
    current = ""
    for block in blocks:
        candidate = f"{current}\n\n{block}".strip() if current else block
        if current and len(candidate) > max_chars:
            chunks.append(current)
            current = block
        else:
            current = candidate
    if current:
        chunks.append(current)

    result: list[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars:
            result.append(chunk)
        else:
            result.extend(
                chunk[i : i + max_chars] for i in range(0, len(chunk), max_chars)
            )
    return result


class QmsgError(SafeError):
    """Qmsg 发送失败；消息已脱敏，不含 key / URL。"""


def prepare_chunks(text: str, max_chars: int) -> list[str]:
    """先整体消毒再切分，避免 URL 被切到两个 chunk 里漏检。"""
    return split_message(sanitize_for_qmsg(text), max_chars)


def send_chunk(
    text: str, key: str, endpoint: str, index: int, total: int
) -> object | None:
    """发送一个 chunk，返回 Qmsg 的 msgId（可能为 None）。

    只认 ``success is True``；响应不是 JSON 也算失败，避免把 502/HTML
    当成“已发送”。异常文本一律经 ``safe_error``，不会带出 key。
    """
    body = text if total == 1 else f"({index}/{total})\n{text}"
    body = sanitize_for_qmsg(body)
    last_error: Exception | None = None

    for attempt in range(3):
        try:
            response = requests.post(
                endpoint.format(key=key), data={"msg": body}, timeout=25
            )
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                raise QmsgError("qmsg response is not JSON") from None

            if not isinstance(data, dict) or data.get("success") is not True:
                message = ""
                code = None
                if isinstance(data, dict):
                    code = data.get("code")
                    message = sanitize_for_qmsg(str(data.get("message", "")))[:80]
                    if key:
                        message = message.replace(key, "[key]")
                raise QmsgError(f"qmsg rejected: code={code} {message}".strip())
            return data.get("data")
        except QmsgError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络错误统一重试
            last_error = exc
            if attempt < 2:
                time.sleep(3.5 + attempt * 2)

    raise QmsgError(f"qmsg send failed: {safe_error(last_error)}")


def send_qmsg(text: str, key: str, endpoint: str, max_chars: int) -> None:
    """用于错误通知等一次性消息；正常日报走 main 的发送日志。"""
    chunks = prepare_chunks(text, max_chars)
    for index, chunk in enumerate(chunks, 1):
        if index > 1:
            # Qmsg 限流：同一 key 每 3 秒只能提交一次
            time.sleep(3.5)
        send_chunk(chunk, key, endpoint, index, len(chunks))

"""消息渲染与 Qmsg 推送。"""

from __future__ import annotations

import time
from typing import Any

import requests

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


def send_qmsg(text: str, key: str, endpoint: str, max_chars: int) -> None:
    chunks = split_message(text, max_chars)
    for index, chunk in enumerate(chunks, 1):
        if index > 1:
            # Qmsg 限流：同一 key 每 3 秒只能提交一次
            time.sleep(3.5)
        body = chunk if len(chunks) == 1 else f"({index}/{len(chunks)})\n{chunk}"
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
                    data = {}
                if isinstance(data, dict) and data.get("success") is False:
                    raise RuntimeError(f"qmsg rejected: {data}")
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                time.sleep(3.5 + attempt * 2)

        if last_error:
            raise RuntimeError(f"qmsg send failed: {last_error}")

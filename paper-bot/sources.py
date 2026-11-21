"""arXiv 抓取、会议识别、关键词过滤。"""

from __future__ import annotations

import datetime as dt
import re
import time
from typing import Any
from xml.etree import ElementTree as ET

import requests

ARXIV_API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

VENUE_CANON = {
    "OSDI": "OSDI",
    "SOSP": "SOSP",
    "ASPLOS": "ASPLOS",
    "EUROSYS": "EuroSys",
    "ATC": "ATC",
    "NSDI": "NSDI",
    "FAST": "FAST",
    "VEE": "VEE",
    "SOCC": "SoCC",
    "HOTOS": "HotOS",
    "MICRO": "MICRO",
    "ISCA": "ISCA",
    "HPCA": "HPCA",
    "PPOPP": "PPoPP",
    "ICS": "ICS",
    "MEMSYS": "MEMSYS",
    "SYSTOR": "SYSTOR",
}


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _parse_dt(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return dt.datetime.now(dt.timezone.utc)


def parse_feed(xml_bytes: bytes) -> list[dict[str, Any]]:
    """解析 arXiv Atom，返回统一结构的论文列表。"""
    root = ET.fromstring(xml_bytes)
    papers: list[dict[str, Any]] = []
    for entry in root.findall("a:entry", NS):
        raw_id = _clean(entry.findtext("a:id", default="", namespaces=NS))
        match = re.search(r"/abs/(.+?)(?:v\d+)?$", raw_id)
        arxiv_id = re.sub(r"v\d+$", "", match.group(1)) if match else raw_id

        authors = [
            _clean(author.findtext("a:name", default="", namespaces=NS))
            for author in entry.findall("a:author", NS)
        ]
        category_node = entry.find("arxiv:primary_category", NS)

        papers.append(
            {
                "id": arxiv_id,
                "title": _clean(entry.findtext("a:title", default="", namespaces=NS)),
                "abstract": _clean(entry.findtext("a:summary", default="", namespaces=NS)),
                "authors": authors[:3] + (["et al."] if len(authors) > 3 else []),
                "published": _clean(entry.findtext("a:published", default="", namespaces=NS)),
                "url": f"https://arxiv.org/abs/{arxiv_id}",
                "comment": _clean(entry.findtext("arxiv:comment", default="", namespaces=NS)),
                "journal_ref": _clean(entry.findtext("arxiv:journal_ref", default="", namespaces=NS)),
                "category": category_node.get("term", "") if category_node is not None else "",
            }
        )
    return papers


def fetch_arxiv(cfg: dict[str, Any], now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """抓取最近 lookback_hours 内的 arXiv 新论文，失败重试 3 次。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    search_query = " OR ".join(f"cat:{cat}" for cat in cfg["arxiv"]["categories"])
    params = {
        "search_query": search_query,
        "start": 0,
        "max_results": cfg["arxiv"]["max_results"],
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    headers = {"User-Agent": cfg["arxiv"]["user_agent"]}
    last_error: Exception | None = None

    for delay in (0, 5, 15, 45):
        if delay:
            time.sleep(delay)
        try:
            response = requests.get(ARXIV_API, params=params, headers=headers, timeout=40)
            response.raise_for_status()
            papers = parse_feed(response.content)
            cutoff = now - dt.timedelta(hours=cfg["general"]["lookback_hours"])
            return [paper for paper in papers if _parse_dt(paper["published"]) >= cutoff]
        except Exception as exc:  # noqa: BLE001 - 网络/解析都要重试
            last_error = exc

    raise RuntimeError(f"arXiv fetch failed after retries: {last_error}")


def _year_suffix(text: str) -> str | None:
    match = re.search(r"(?:19|20)(\d{2})", text)
    if match:
        return match.group(1)
    match = re.search(r"'(\d{2})", text)
    return match.group(1) if match else None


def detect_venue(paper: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, float]:
    """从 comment / journal_ref 识别会议或期刊，返回 (显示标签, 加分)。"""
    text = f"{paper.get('comment', '')} {paper.get('journal_ref', '')}".upper()

    for venue in cfg["venue"]["top"]:
        token = venue.upper()
        if re.search(rf"\b{re.escape(token)}\b", text):
            label = VENUE_CANON.get(token, venue)
            year = _year_suffix(text)
            if year:
                label = f"{label}'{year}"
            return label, float(cfg["venue"]["boost"])

    journal = paper.get("journal_ref", "")
    if journal:
        short = journal if len(journal) <= 24 else journal[:21] + "…"
        return short, float(cfg["venue"]["journal_boost"])

    return "", 0.0


def enrich(papers: list[dict[str, Any]], cfg: dict[str, Any]) -> list[dict[str, Any]]:
    for paper in papers:
        paper["title_key"] = re.sub(r"[^a-z0-9]+", "", paper["title"].lower())
        label, boost = detect_venue(paper, cfg)
        paper["venue_label"] = label
        paper["venue_boost"] = boost
    return papers


def select(
    papers: list[dict[str, Any]], cfg: dict[str, Any], limit: int | None = None
) -> list[dict[str, Any]]:
    top_n = limit or cfg["general"]["top_n"]
    threshold = float(cfg["general"]["min_final_score"])
    qualified = [paper for paper in papers if paper.get("final_score", 0) >= threshold]
    qualified.sort(key=lambda paper: paper.get("final_score", 0), reverse=True)
    return qualified[:top_n]

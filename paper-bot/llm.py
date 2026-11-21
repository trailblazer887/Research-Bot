"""LLM 调用：批量打分 + 结构化摘要。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

PROMPT_DIR = Path(__file__).parent / "prompts"


def _prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _loads(text: str) -> Any:
    text = _strip_fences(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _chat(cfg: dict[str, Any], messages: list[dict[str, str]], api_key: str) -> str:
    url = cfg["llm"]["base_url"].rstrip("/") + "/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict[str, Any] = {
        "model": cfg["llm"]["model"],
        "messages": messages,
        "temperature": float(cfg["llm"].get("temperature", 0.0)),
    }
    if cfg["llm"].get("json_mode", True):
        payload["response_format"] = {"type": "json_object"}

    last_error: Exception | None = None
    for attempt in range(2):
        try:
            response = requests.post(
                url, headers=headers, json=payload, timeout=cfg["llm"]["timeout"]
            )
            if response.status_code == 400 and "response_format" in payload and attempt == 0:
                payload.pop("response_format", None)
                continue
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - 统一重试
            last_error = exc
            time.sleep(2)

    raise RuntimeError(f"LLM request failed: {last_error}")


def score_papers(
    papers: list[dict[str, Any]], cfg: dict[str, Any], api_key: str
) -> dict[str, tuple[float, str]]:
    """批量打分，返回 {id: (score, reason)}。"""
    system = _prompt("score.md")
    results: dict[str, tuple[float, str]] = {}
    batch_size = 15

    for start in range(0, len(papers), batch_size):
        batch = papers[start : start + batch_size]
        payload = [
            {
                "id": paper["id"],
                "title": paper["title"],
                "venue": paper.get("venue_label", ""),
                "abstract": paper["abstract"][:1200],
            }
            for paper in batch
        ]
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        last_error: Exception | None = None

        for attempt in range(2):
            try:
                data = _loads(_chat(cfg, messages, api_key))
                items = data if isinstance(data, list) else data.get("items", [])
                for item in items:
                    paper_id = str(item.get("id", ""))
                    if not paper_id:
                        continue
                    score = max(0.0, min(5.0, float(item.get("score", 0))))
                    results[paper_id] = (score, str(item.get("reason", ""))[:60])
                break
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                messages = messages[:2] + [
                    {"role": "user", "content": "只输出 JSON，不要解释。"}
                ]
        else:
            raise RuntimeError(f"score parse failed: {last_error}")

    return results


def _number_tokens(text: str) -> list[str]:
    normalized = (
        text.replace("–", "-").replace("—", "-").replace("−", "-").replace(",", "")
    )
    return re.findall(r"\d+(?:\.\d+)?", normalized)


def _validate_evidence(summary: dict[str, Any], abstract: str) -> dict[str, Any]:
    evidence = str(summary.get("evidence", ""))
    if not evidence or "摘要未给出" in evidence:
        return summary
    normalized_abstract = (
        abstract.replace("–", "-").replace("—", "-").replace("−", "-").replace(",", "")
    )
    if any(token not in normalized_abstract for token in _number_tokens(evidence)):
        summary["evidence"] = "摘要未给出具体数字"
        summary["confidence"] = "low"
    return summary


def _normalize_summary(data: dict[str, Any], paper: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "problem": str(data.get("problem", "")).strip(),
        "core_idea": str(data.get("core_idea", "")).strip(),
        "mechanism": str(data.get("mechanism", "")).strip(),
        "evidence": str(data.get("evidence", "")).strip(),
        "relevance": str(data.get("relevance", "")).strip(),
        "limitation": str(data.get("limitation", "")).strip(),
        "read_level": str(data.get("read_level", "泛读")).strip(),
        "terms": data.get("terms") if isinstance(data.get("terms"), list) else [],
        "confidence": str(data.get("confidence", "low")).strip(),
    }
    if summary["read_level"] not in {"精读", "泛读", "跳过"}:
        summary["read_level"] = "泛读"
    if summary["confidence"] not in {"high", "medium", "low"}:
        summary["confidence"] = "low"
    summary["terms"] = [str(term)[:24] for term in summary["terms"][:2]]
    return _validate_evidence(summary, paper["abstract"])


def summarize_paper(
    paper: dict[str, Any], cfg: dict[str, Any], api_key: str
) -> dict[str, Any]:
    system = _prompt("summarize.md")
    user = {
        "title": paper["title"],
        "venue": paper.get("venue_label", ""),
        "comment": paper.get("comment", ""),
        "abstract": paper["abstract"][:3000],
    }
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            data = _loads(_chat(cfg, messages, api_key))
            if not isinstance(data, dict):
                raise ValueError("summary is not a JSON object")
            return _normalize_summary(data, paper)
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            messages = messages[:2] + [
                {
                    "role": "user",
                    "content": "上一次输出不是合法 JSON。只输出 JSON 对象，不要解释。",
                }
            ]
    raise RuntimeError(f"summary failed: {last_error}")


def fallback_summary(paper: dict[str, Any]) -> dict[str, Any]:
    abstract = paper.get("abstract", "")
    return {
        "problem": abstract[:140] + ("…" if len(abstract) > 140 else ""),
        "core_idea": "（未配置 LLM，显示摘要原文）",
        "mechanism": "",
        "evidence": "摘要未给出",
        "relevance": "未评估",
        "limitation": "未评估",
        "read_level": "泛读",
        "terms": [],
        "confidence": "low",
    }

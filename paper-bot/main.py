"""每日文献机器人：抓取 → 去重 → 打分 → 摘要 → 推送。"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import llm
import notify
import sources
import store
from safety import SafeError, safe_error

BASE_DIR = Path(__file__).parent
WEEKDAYS = "一二三四五六日"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _data_dir(cfg: dict[str, Any]) -> Path:
    return BASE_DIR / cfg["general"]["data_dir"]


def _date_cn(now: dt.datetime) -> str:
    return f"{now:%m-%d} 周{WEEKDAYS[now.weekday()]}"


SKIPPED_FIELDS = ("id", "title", "title_key", "score", "final_score", "status", "first_seen")


def _for_storage(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") != "skipped":
        return row
    return {key: row[key] for key in SKIPPED_FIELDS if key in row}


def compact_store(cfg: dict[str, Any]) -> int:
    path = _data_dir(cfg) / "papers.jsonl"
    rows = store.read_jsonl(path)
    before = path.stat().st_size if path.exists() else 0
    store.write_jsonl(path, [_for_storage(row) for row in rows])
    after = path.stat().st_size
    print(f"papers.jsonl: {before / 1024:.1f} KB -> {after / 1024:.1f} KB，{len(rows)} 行")
    return 0


def _last_run_path(cfg: dict[str, Any]) -> Path:
    return _data_dir(cfg) / "last_run.json"


def _digest_path(cfg: dict[str, Any], date: str) -> Path:
    return _data_dir(cfg) / "digests" / f"{date}.md"


def _merge_papers(cfg: dict[str, Any], papers: list[dict[str, Any]]) -> None:
    path = _data_dir(cfg) / "papers.jsonl"
    rows = store.read_jsonl(path)
    by_id = {row["id"]: row for row in rows}
    for paper in papers:
        by_id[paper["id"]] = paper
    store.write_jsonl(path, [_for_storage(row) for row in by_id.values()])


def _finalize_delivery(cfg: dict[str, Any], journal: dict[str, Any]) -> None:
    """把已确认发送的 journal 落成 papers.jsonl + 日报 + 精简 last_run。

    可重复执行：发送成功但写状态失败时，下一轮会从这里补写，不会再发一次。
    """
    date = str(journal.get("date", ""))
    papers = list(journal.get("papers", []))
    _merge_papers(cfg, list(journal.get("new_papers", [])) + papers)

    digest_text = str(journal.get("digest_text", ""))
    digest = _digest_path(cfg, date)
    if digest_text and date and not digest.exists():
        store.write_text(digest, f"# {date} 日报\n\n{digest_text}\n")

    stats = journal.get("stats") or {}
    sent_count = (
        len(papers) if papers else int(journal.get("sent_count", journal.get("sent", 0)))
    )
    msg_ids = [
        chunk.get("msg_id")
        for chunk in journal.get("chunks", [])
        if chunk.get("sent")
    ]
    if not msg_ids:
        msg_ids = list(journal.get("msg_ids", []))

    store.write_json(
        _last_run_path(cfg),
        {
            "at": journal.get("at", ""),
            "date": date,
            "mode": "daily",
            "status": "sent",
            "fetched": stats.get("fetched", journal.get("fetched", 0)),
            "kept": stats.get("kept", journal.get("kept", 0)),
            "sent": sent_count,
            "sent_count": sent_count,
            "msg_ids": [msg_id for msg_id in msg_ids if msg_id is not None],
            "errors": [],
        },
    )


def _send_journal(cfg: dict[str, Any], journal: dict[str, Any]) -> None:
    """发送 journal 里尚未发送的 chunk；每成功一条就落一次盘。"""
    key = os.getenv("QMSG_KEY", "").strip()
    if not key:
        raise SafeError("缺少 QMSG_KEY，无法发送或续发日报")

    endpoint = cfg["qmsg"]["endpoint"]
    chunks = journal.get("chunks", [])
    total = len(chunks)
    journal["status"] = "sending"
    journal["error"] = ""
    journal["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    store.write_json(_last_run_path(cfg), journal)

    already = any(chunk.get("sent") or chunk.get("msg_id") for chunk in chunks)
    try:
        for index, chunk in enumerate(chunks, 1):
            if chunk.get("sent") or chunk.get("msg_id"):
                continue
            if already:
                # Qmsg 限流：同一 key 每 3 秒只能提交一次
                time.sleep(3.5)
            msg_id = notify.send_chunk(chunk["text"], key, endpoint, index, total)
            chunk["sent"] = True
            chunk["msg_id"] = msg_id
            journal["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            store.write_json(_last_run_path(cfg), journal)
            already = True
    except Exception as exc:
        journal["status"] = "failed"
        journal["error"] = safe_error(exc)
        journal["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        store.write_json(_last_run_path(cfg), journal)
        raise

    journal["status"] = "sent"
    journal["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    store.write_json(_last_run_path(cfg), journal)
    _finalize_delivery(cfg, journal)


def _recover_stale(cfg: dict[str, Any], journal: dict[str, Any]) -> None:
    """处理隔夜残留的 journal：已发送的补写，未发送的放弃或回填。"""
    date = str(journal.get("date", ""))
    if journal.get("status") == "abandoned":
        return
    if journal.get("status") == "sent":
        _finalize_delivery(cfg, journal)
        return

    chunks = journal.get("chunks", [])
    if chunks and all(chunk.get("sent") for chunk in chunks):
        # 所有 chunk 都已提交，只是没来得及落 papers/digest
        _finalize_delivery(cfg, journal)
        return

    papers = list(journal.get("papers", []))
    delivered = any(
        chunk.get("sent") or chunk.get("msg_id") for chunk in chunks
    )
    for paper in papers:
        paper["status"] = "skipped" if delivered else "pool"
        paper["digest_date"] = ""
        paper["backfill"] = False
    _merge_papers(cfg, list(journal.get("new_papers", [])) + papers)
    print(
        f"上一轮日报（{date}）未完成："
        + ("部分已发送，剩余标记为已跳过" if delivered else "未发送，已放回库存")
    )
    stats = journal.get("stats") or {}
    store.write_json(
        _last_run_path(cfg),
        {
            "at": journal.get("at", ""),
            "date": date,
            "mode": "daily",
            "status": "abandoned",
            "fetched": stats.get("fetched", 0),
            "kept": stats.get("kept", 0),
            "sent": 0,
            "errors": [journal.get("error", "")] if journal.get("error") else [],
        },
    )


def _record_failure(cfg: dict[str, Any], exc: BaseException) -> None:
    """失败时保留发送日志（用于续发），只写入脱敏后的错误。"""
    path = _last_run_path(cfg)
    journal = store.read_json(path, {})
    now = dt.datetime.now(ZoneInfo(cfg["general"]["timezone"]))
    today = now.date().isoformat()

    if journal.get("date") == today and journal.get("status") in {"sending", "failed"}:
        journal["status"] = "failed"
        journal["error"] = safe_error(exc)
        journal["updated_at"] = now.isoformat()
        store.write_json(path, journal)
        return
    if journal.get("date") == today and journal.get("status") == "sent":
        return

    store.write_json(
        path,
        {
            "at": now.isoformat(),
            "date": today,
            "mode": "daily",
            "status": "failed",
            "fetched": 0,
            "kept": 0,
            "sent": 0,
            "errors": [safe_error(exc)],
        },
    )


def _weekly_footer(rows: list[dict[str, Any]], today: str) -> str:
    start = (dt.date.fromisoformat(today) - dt.timedelta(days=6)).isoformat()
    sent = [
        row
        for row in rows
        if row.get("status") == "sent" and row.get("digest_date", "") >= start
    ]
    if not sent:
        return "本周：暂无发送记录"
    top = max(sent, key=lambda row: row.get("final_score", 0))
    return (
        f"本周：发送 {len(sent)} 篇｜最高分 "
        f"[{top.get('venue_label', '')}] {top['title'][:46]}"
    )


def run_daily(cfg: dict[str, Any], dry_run: bool = False, limit: int | None = None) -> int:
    tz = ZoneInfo(cfg["general"]["timezone"])
    now = dt.datetime.now(tz)
    today = now.date().isoformat()
    data_dir = _data_dir(cfg)
    data_dir.mkdir(parents=True, exist_ok=True)

    last_run = store.read_json(_last_run_path(cfg), {})
    if not dry_run:
        if last_run.get("date") == today:
            if last_run.get("status") == "sent":
                _finalize_delivery(cfg, last_run)
                print("今日日报已发送，跳过重复推送。")
                return 0
            if last_run.get("status") in {"sending", "failed"} and last_run.get(
                "chunks"
            ):
                print("发现今日未完成的发送，续发剩余消息。")
                _send_journal(cfg, last_run)
                return 0
        elif last_run.get("date"):
            _recover_stale(cfg, last_run)
            last_run = {}

        if _digest_path(cfg, today).exists():
            print("今日日报文件已存在，跳过（避免覆盖和重复推送）。")
            return 0

    rows = store.read_jsonl(data_dir / "papers.jsonl")
    known_ids = {row["id"] for row in rows}
    known_keys = {row.get("title_key", "") for row in rows}

    fetched = sources.enrich(
        sources.fetch_arxiv(cfg, now=now.astimezone(dt.timezone.utc)), cfg
    )
    fresh = [
        paper
        for paper in fetched
        if paper["id"] not in known_ids and paper["title_key"] not in known_keys
    ]
    unique: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for paper in fresh:
        if paper["title_key"] in seen_keys:
            continue
        seen_keys.add(paper["title_key"])
        unique.append(paper)
    fresh = unique

    for paper in fresh:
        paper["first_seen"] = today
        paper["digest_date"] = ""
        paper["status"] = "skipped"

    api_key = os.getenv("LLM_API_KEY", "").strip()
    new_papers = fresh

    if new_papers and not api_key:
        if dry_run:
            print(f"抓到 {len(new_papers)} 篇新论文（未配置 LLM，无法排序）。前 20 篇：")
            for paper in new_papers[:20]:
                print(f"- [{paper['category']}] {paper['title']}")
                print(f"  {paper['url']}")
            print("\n配置 LLM_API_KEY 后重跑，才能看到打分和摘要。")
            return 0
        raise SafeError("缺少 LLM_API_KEY（本地调试可用 --dry-run）")

    scores = llm.score_papers(new_papers, cfg, api_key) if new_papers else {}

    threshold = float(cfg["general"]["min_final_score"])
    qualified: list[dict[str, Any]] = []
    for paper in new_papers:
        score, reason = scores.get(paper["id"], (0.0, ""))
        paper["score"] = round(score, 2)
        paper["reason"] = reason
        paper["final_score"] = round(score + float(paper.get("venue_boost", 0.0)), 2)
        if paper["final_score"] >= threshold:
            paper["status"] = "pool"
            qualified.append(paper)
        else:
            paper["status"] = "skipped"

    rows.extend(new_papers)
    top_n = limit or int(cfg["general"]["top_n"])
    selected = sources.select(qualified, cfg, top_n)
    selected_ids = {paper["id"] for paper in selected}

    cutoff = (
        now.date() - dt.timedelta(days=int(cfg["general"]["backfill_days"]))
    ).isoformat()
    candidates = [
        row
        for row in rows
        if row.get("status") == "pool"
        and row.get("first_seen", "") >= cutoff
        and row.get("id") not in selected_ids
    ]
    candidates.sort(key=lambda row: row.get("final_score", 0), reverse=True)
    for row in candidates:
        if len(selected) >= top_n:
            break
        row["backfill"] = True
        selected.append(row)
        selected_ids.add(row["id"])

    if selected:
        for paper in selected:
            if not paper.get("summary"):
                paper["summary"] = (
                    llm.summarize_paper(paper, cfg, api_key)
                    if api_key
                    else llm.fallback_summary(paper)
                )
            paper["status"] = "sent"
            paper["digest_date"] = today

    stats = {
        "date_cn": _date_cn(now),
        "fetched": len(fetched),
        "kept": len(qualified),
        "pool": sum(1 for row in rows if row.get("status") == "pool"),
        "total": len(rows),
    }

    weekly = _weekly_footer(rows, today) if now.weekday() == 5 else None
    include_url = bool(cfg["qmsg"].get("include_url", False))
    digest_text = notify.render(selected, stats, weekly, include_url=True)

    if selected:
        message = notify.render(selected, stats, weekly, include_url=include_url)
    else:
        message = notify.render([], stats, include_url=include_url)
        message += "\n\n今天没有新论文（或都已推送过）。明天见。"

    if dry_run:
        print(message)
        print("\n[dry-run] 未发送，也未写入状态。")
        return 0

    key = os.getenv("QMSG_KEY", "").strip()
    if not key:
        raise SafeError("缺少 QMSG_KEY")
    if not api_key:
        raise SafeError("缺少 LLM_API_KEY（本地调试可用 --dry-run）")

    chunks = notify.prepare_chunks(message, int(cfg["qmsg"]["max_chars"]))
    journal = {
        "at": now.isoformat(),
        "date": today,
        "mode": "daily",
        "status": "sending",
        "new_papers": new_papers,
        "papers": selected,
        "chunks": [
            {"index": index, "text": chunk, "sent": False}
            for index, chunk in enumerate(chunks, 1)
        ],
        "digest_text": digest_text,
        "stats": stats,
        "errors": [],
    }
    store.write_json(_last_run_path(cfg), journal)
    _send_journal(cfg, journal)
    print(f"已发送 {len(selected)} 篇。")
    return 0


def self_test(cfg: dict[str, Any]) -> int:
    fixture = BASE_DIR / "tests" / "fixtures" / "arxiv_sample.xml"
    papers = sources.enrich(sources.parse_feed(fixture.read_bytes()), cfg)
    if len(papers) != 4:
        raise SystemExit(f"self-test failed: expected 4 papers, got {len(papers)}")

    fake = {
        "2411.00001": (
            4.6,
            {
                "problem": "microVM 的 guest 内存对 host 不可见，host 侧回收容易打错页。",
                "core_idea": "把 host PSI 与 guest free page reporting 合成一个双层水位控制器。",
                "evidence": "p99 分配延迟降低 23%，超售比提升 1.4x（原文）。",
                "relevance": "直接对应“两层账本”：水位设在 host，但冷热知识在 guest。",
                "limitation": "只在 64 核单机评估。",
                "read_level": "精读",
                "terms": ["PSI", "free page reporting"],
                "confidence": "high",
            },
        ),
        "2411.00002": (
            3.8,
            {
                "problem": "用内存使用率决定卸载量会被 page cache 误导。",
                "core_idea": "用 PSI lost-work 替代使用率，决定匿名页向 zswap 卸载多少。",
                "evidence": "200 节点集群总内存下降 27%，p99 无回退（原文）。",
                "relevance": "把 PSI 从观测指标变成控制信号，对应第二个问题。",
                "limitation": "依赖 zswap，未覆盖 CXL。",
                "read_level": "泛读",
                "terms": ["PSI", "zswap"],
                "confidence": "medium",
            },
        ),
        "2411.00003": (
            1.0,
            {
                "problem": "联邦学习综述，与内存水位线无关。",
                "core_idea": "不相关。",
                "evidence": "摘要未给出",
                "relevance": "无关",
                "limitation": "无关",
                "read_level": "跳过",
                "terms": [],
                "confidence": "low",
            },
        ),
        "2411.00004": (
            4.2,
            {
                "problem": "分层内存缺少低开销的热度追踪，冷页占着 DRAM。",
                "core_idea": "采样 access bit 识别热页，主动 demote 冷页并为新分配预留 headroom。",
                "evidence": "吞吐比 NUMA balancing 高 18%（原文）。",
                "relevance": "headroom 思路可以直接借到沙箱水位线的高/低水位设计。",
                "limitation": "4 socket 机器上的原型，未评估多租户。",
                "read_level": "精读",
                "terms": ["CXL", "demotion"],
                "confidence": "high",
            },
        ),
    }

    for paper in papers:
        score, summary = fake[paper["id"]]
        paper["score"] = score
        paper["final_score"] = round(score + paper["venue_boost"], 2)
        paper["summary"] = summary
        paper["first_seen"] = "2026-11-21"

    selected = sources.select(papers, cfg)
    stats = {
        "date_cn": "11-21 周五",
        "fetched": 4,
        "kept": 3,
        "pool": 0,
        "total": 4,
    }
    message = notify.render(selected, stats, include_url=False)
    digest = notify.render(selected, stats, include_url=True)
    print(message)

    if "ASPLOS'26" not in message:
        raise SystemExit("self-test failed: venue label missing")
    if "Federated" in message:
        raise SystemExit("self-test failed: irrelevant paper leaked")
    if "http" in message:
        raise SystemExit("self-test failed: QQ message must not contain URLs")
    if "2411.00001" not in digest:
        raise SystemExit("self-test failed: digest url missing")

    print("\n[self-test] OK：解析 / 过滤 / 会议识别 / 渲染 都正常。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="每日文献机器人")
    parser.add_argument("--mode", choices=["daily", "venue"], default="daily")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发送、不写状态")
    parser.add_argument("--limit", type=int, default=None, help="临时覆盖 top_n")
    parser.add_argument("--self-test", action="store_true", help="用本地 fixture 跑通链路")
    parser.add_argument("--compact", action="store_true", help="压缩 papers.jsonl 中 skipped 行")
    parser.add_argument("--config", default=str(BASE_DIR / "config.toml"))
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.self_test:
        return self_test(cfg)
    if args.compact:
        return compact_store(cfg)
    if args.mode == "venue":
        print("venue 模式是 v1.5（USENIX proceedings），当前版本未实现。")
        return 0

    try:
        return run_daily(cfg, dry_run=args.dry_run, limit=args.limit)
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，必须通知
        message = f"【沙箱内存·每日】运行失败\n{safe_error(exc)}"
        print(message, file=sys.stderr)

        if not args.dry_run:
            try:
                _record_failure(cfg, exc)
            except Exception:  # noqa: BLE001
                pass
            key = os.getenv("QMSG_KEY", "").strip()
            if key:
                try:
                    notify.send_qmsg(
                        message,
                        key,
                        cfg["qmsg"]["endpoint"],
                        int(cfg["qmsg"]["max_chars"]),
                    )
                except Exception:  # noqa: BLE001
                    pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

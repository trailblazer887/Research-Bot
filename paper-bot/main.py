"""每日文献机器人：抓取 → 去重 → 打分 → 摘要 → 推送。"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import tomllib
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import llm
import notify
import sources
import store

BASE_DIR = Path(__file__).parent
WEEKDAYS = "一二三四五六日"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def _data_dir(cfg: dict[str, Any]) -> Path:
    return BASE_DIR / cfg["general"]["data_dir"]


def _date_cn(now: dt.datetime) -> str:
    return f"{now:%m-%d} 周{WEEKDAYS[now.weekday()]}"


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
        raise RuntimeError("缺少 LLM_API_KEY（本地调试可用 --dry-run）")

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
        raise RuntimeError("缺少 QMSG_KEY")
    if not api_key:
        raise RuntimeError("缺少 LLM_API_KEY（本地调试可用 --dry-run）")

    notify.send_qmsg(
        message, key, cfg["qmsg"]["endpoint"], int(cfg["qmsg"]["max_chars"])
    )

    store.write_jsonl(data_dir / "papers.jsonl", rows)
    digest_dir = data_dir / "digests"
    digest_dir.mkdir(parents=True, exist_ok=True)
    (digest_dir / f"{today}.md").write_text(
        f"# {today} 日报\n\n{digest_text}\n", encoding="utf-8"
    )
    store.write_json(
        data_dir / "last_run.json",
        {
            "at": now.isoformat(),
            "mode": "daily",
            "fetched": len(fetched),
            "kept": len(qualified),
            "sent": len(selected),
            "errors": [],
        },
    )
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
    parser.add_argument("--config", default=str(BASE_DIR / "config.toml"))
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    if args.self_test:
        return self_test(cfg)
    if args.mode == "venue":
        print("venue 模式是 v1.5（USENIX proceedings），当前版本未实现。")
        return 0

    try:
        return run_daily(cfg, dry_run=args.dry_run, limit=args.limit)
    except Exception as exc:  # noqa: BLE001 - 顶层兜底，必须通知
        message = f"【沙箱内存·每日】运行失败\n{type(exc).__name__}: {exc}"
        print(message, file=sys.stderr)

        if not args.dry_run:
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
            try:
                store.write_json(
                    _data_dir(cfg) / "last_run.json",
                    {
                        "at": dt.datetime.now(
                            ZoneInfo(cfg["general"]["timezone"])
                        ).isoformat(),
                        "mode": args.mode,
                        "fetched": 0,
                        "kept": 0,
                        "sent": 0,
                        "errors": [f"{type(exc).__name__}: {exc}"],
                    },
                )
            except Exception:  # noqa: BLE001
                pass
        return 1


if __name__ == "__main__":
    sys.exit(main())

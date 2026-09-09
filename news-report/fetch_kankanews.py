#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup

PROGRAM_ID = "7LMwVLeyzn3"
BASE_URL = "https://www.kankanews.com/program/{program_id}/{date}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
ITEM_RE = re.compile(r"^(\d+)\s+(\d{2}:\d{2})\s+(.+)$")


def shanghai_yesterday() -> str:
    now_cn = datetime.utcnow() + timedelta(hours=8)
    return (now_cn.date() - timedelta(days=1)).isoformat()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def extract_items(html: str):
    soup = BeautifulSoup(html, "html.parser")
    title = clean_text(soup.title.get_text(" ", strip=True)) if soup.title else ""
    marker = soup.find(string=re.compile("本期看点"))

    candidates = []
    if marker:
        cur = marker.parent
        for _ in range(5):
            if cur is None:
                break
            for tag in cur.find_all(["a", "li", "h1", "h2", "h3", "h4", "p", "span"]):
                candidates.append(clean_text(tag.get_text(" ", strip=True)))
            cur = cur.parent

    # Fallback across the visible page if the nearby scope changes.
    if not candidates:
        candidates = [clean_text(tag.get_text(" ", strip=True)) for tag in soup.find_all(["a", "li", "p", "span"])]

    parsed = {}
    for text in candidates:
        m = ITEM_RE.match(text)
        if not m:
            continue
        source_order = int(m.group(1))
        duration = m.group(2)
        headline = clean_text(m.group(3))
        # First occurrence wins; repeated ancestors often duplicate the same list.
        parsed.setdefault(source_order, {"order": source_order, "duration": duration, "title": headline})

    items = [parsed[k] for k in sorted(parsed)]
    meta = {
        "html_title": title,
        "marker_found": bool(marker),
        "numbered_item_count": len(items),
    }
    return items, meta


def fetch(date: str, out_dir: Path) -> Path:
    url = BASE_URL.format(program_id=PROGRAM_ID, date=date)
    headers = {
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5",
        "Referer": "https://www.kankanews.com/",
    }
    r = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
    html = r.text
    items, meta = extract_items(html) if html else ([], {})

    status, reasons = "ok", []
    if r.status_code != 200:
        status = "incomplete"; reasons.append(f"http_status={r.status_code}")
    if len(html) < 1000:
        status = "incomplete"; reasons.append("html_too_short")
    if "新闻报道" not in html:
        status = "incomplete"; reasons.append("program_name_not_found")
    if date.replace("-", "") not in html and date not in html:
        status = "incomplete"; reasons.append("date_not_confirmed_in_page")
    if not meta.get("marker_found"):
        status = "incomplete"; reasons.append("highlights_marker_not_found")
    if not items:
        status = "incomplete"; reasons.append("no_numbered_program_items_extracted")
    elif items[0]["order"] != 1:
        status = "incomplete"; reasons.append("program_item_1_missing")

    data = {
        "date": date,
        "program": "新闻报道",
        "channel": "上海电视台新闻综合频道",
        "scheduled_time": "18:30",
        "program_id": PROGRAM_ID,
        "url": url,
        "fetched_at_cn": (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds") + "+08:00",
        "http_status": r.status_code,
        "final_url": r.url,
        "status": status,
        "validation_notes": reasons,
        "page_meta": meta,
        "item_count": len(items),
        "items": items,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if status != "ok":
        diag_dir = out_dir / "diagnostics"
        diag_dir.mkdir(parents=True, exist_ok=True)
        (diag_dir / f"{date}.html").write_text(html[:500_000], encoding="utf-8", errors="ignore")
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return out_path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD; defaults to yesterday in Asia/Shanghai")
    p.add_argument("--out", default="news-report/data")
    args = p.parse_args()
    date = args.date or shanghai_yesterday()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise SystemExit("--date must be YYYY-MM-DD")
    fetch(date, Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())

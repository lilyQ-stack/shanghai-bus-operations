#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

PROGRAM_ID = "7LMwVLeyzn3"
BASE_URL = "https://www.kankanews.com/program/{program_id}/{date}"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


def shanghai_yesterday() -> str:
    now_cn = datetime.utcnow() + timedelta(hours=8)
    return (now_cn.date() - timedelta(days=1)).isoformat()


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def looks_like_story(text: str) -> bool:
    text = clean_text(text)
    if not (6 <= len(text) <= 100):
        return False
    blacklist = {"本期看点", "新闻报道", "查看更多", "展开", "收起", "分享", "往期", "电视新闻", "看看新闻", "首页", "登录", "下载APP", "相关推荐"}
    if text in blacklist or re.fullmatch(r"\d{4,8}", text):
        return False
    return len(re.findall(r"[\u4e00-\u9fff]", text)) >= 4


def walk_json(obj: Any) -> Iterable[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            lk = str(key).lower()
            if isinstance(value, str) and any(token in lk for token in ("title", "name", "headline", "subject")):
                yield value
            yield from walk_json(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk_json(item)


def dedupe(items: Iterable[str]) -> list[str]:
    out, seen = [], set()
    for item in items:
        text = clean_text(item)
        key = re.sub(r"[\s｜|·:：—_-]+", "", text)
        if not looks_like_story(text) or key in seen:
            continue
        seen.add(key)
        out.append(text)
    return out


def extract_items(html: str) -> tuple[list[str], dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    meta: dict[str, Any] = {}
    if soup.title:
        meta["html_title"] = clean_text(soup.title.get_text(" ", strip=True))

    dom_candidates = []
    marker = soup.find(string=re.compile("本期看点"))
    if marker:
        cur = marker.parent
        for _ in range(5):
            if cur is None:
                break
            for tag in cur.find_all(["a", "li", "h1", "h2", "h3", "h4", "p", "span"]):
                dom_candidates.append(tag.get_text(" ", strip=True))
            cur = cur.parent

    broad_candidates = [tag.get_text(" ", strip=True) for tag in soup.find_all(["a", "h1", "h2", "h3", "h4"])]

    json_candidates = []
    parsed_json_blocks = 0
    for script in soup.find_all("script"):
        raw = (script.string or script.get_text() or "").strip()
        if not raw:
            continue
        payloads = []
        if script.get("type") == "application/json":
            payloads.append(raw)
        m = re.search(r"=\s*({.*})\s*;?\s*$", raw, flags=re.S)
        if m:
            payloads.append(m.group(1))
        for payload in payloads:
            try:
                data = json.loads(payload)
            except Exception:
                continue
            parsed_json_blocks += 1
            json_candidates.extend(walk_json(data))

    primary = dedupe(dom_candidates)
    embedded = dedupe(json_candidates)
    broad = dedupe(broad_candidates)
    items = dedupe(primary + embedded + broad)
    meta.update({"marker_found": bool(marker), "primary_candidate_count": len(primary), "embedded_candidate_count": len(embedded), "broad_candidate_count": len(broad), "parsed_json_blocks": parsed_json_blocks})
    return items, meta


def fetch(date: str, out_dir: Path) -> Path:
    url = BASE_URL.format(program_id=PROGRAM_ID, date=date)
    headers = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.5", "Referer": "https://www.kankanews.com/"}
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
        reasons.append("date_not_confirmed_in_page")
    if not items:
        status = "incomplete"; reasons.append("no_story_items_extracted")

    data = {"date": date, "program": "新闻报道", "channel": "上海电视台新闻综合频道", "scheduled_time": "18:30", "program_id": PROGRAM_ID, "url": url, "fetched_at_cn": (datetime.utcnow() + timedelta(hours=8)).isoformat(timespec="seconds") + "+08:00", "http_status": r.status_code, "final_url": r.url, "status": status, "validation_notes": reasons, "page_meta": meta, "item_count": len(items), "items": [{"order": i + 1, "title": t} for i, t in enumerate(items)]}

    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date}.json"
    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    if status != "ok":
        diag_dir = out_dir / "diagnostics"; diag_dir.mkdir(parents=True, exist_ok=True)
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

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import reverse_watch_sampler as watch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args()
    root = Path(args.root)
    date = args.date or datetime.now(watch.CST).strftime("%Y-%m-%d")
    source = root / "data" / "shmaas" / f"{date}-{watch.ROUTE}.jsonl"
    snapshots = watch.load_jsonl(source)
    active = watch.find_active_watches(snapshots)
    if not active:
        print("no active reverse watches; no tracking record written")
        return
    watch.run(date, root)


if __name__ == "__main__":
    main()

"""Independent, manual-only, read-only Baidu traffic diagnostics.

No SHMAAS imports, no repository data writes, and no AK logging.
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AK = os.environ.get("BAIDU_MAP_AK", "").strip()
if not AK:
    raise SystemExit("Missing BAIDU_MAP_AK GitHub Actions secret")

def get_road(road):
    params = urllib.parse.urlencode({"road_name": road, "city": "上海市", "ak": AK})
    url = "https://api.map.baidu.com/traffic/v1/road?" + params
    req = urllib.request.Request(url, headers={"User-Agent": "bus-commute-traffic-test/1.1"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.load(resp)
    # Print only selected traffic data. Never print request URL, AK, or raw payload.
    traffic = data.get("road_traffic") or []
    result = {
        "road": road,
        "status": data.get("status"),
        "message": data.get("message"),
        "description": data.get("description"),
        "evaluation": data.get("evaluation"),
        "response_keys": sorted(data.keys()),
        "road_traffic_count": len(traffic),
        "road_traffic": [],
    }
    for item in traffic[:12]:
        sections = item.get("congestion_sections") or []
        result["road_traffic"].append({
            "road_name": item.get("road_name"),
            "status": item.get("status"),
            "congestion_sections_count": len(sections),
            "available_fields": sorted(item.keys()),
            "sections": [
                {key: section.get(key) for key in (
                    "section_desc", "status", "speed", "congestion_distance",
                    "congestion_trend", "direction", "start_point", "end_point"
                ) if key in section}
                for section in sections[:25]
            ],
        })
    return result

def main():
    print("Sample time CST:", datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"))
    failed = False
    for road in ("长岛路", "金杨路"):
        try:
            result = get_road(road)
            print(json.dumps(result, ensure_ascii=False))
            if result["status"] != 0:
                failed = True
        except Exception as exc:
            # No exception text: some HTTP clients embed URLs containing credentials.
            print(json.dumps({"road": road, "error_type": type(exc).__name__}, ensure_ascii=False))
            failed = True
    if failed:
        raise SystemExit("One or more Baidu traffic queries failed; see safe summaries above")

if __name__ == "__main__":
    main()

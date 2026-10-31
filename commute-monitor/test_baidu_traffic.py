"""Read-only Baidu traffic API smoke test. Never prints AK."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

AK = os.environ.get("BAIDU_MAP_AK", "").strip()
if not AK:
    raise SystemExit("Missing BAIDU_MAP_AK GitHub Actions secret")

def query_road(road):
    params = urllib.parse.urlencode({"road_name": road, "city": "上海市", "ak": AK})
    req = urllib.request.Request("https://api.map.baidu.com/traffic/v1/road?" + params, headers={"User-Agent": "bus-commute-traffic-test/1.0"})
    with urllib.request.urlopen(req, timeout=20) as response:
        result = json.load(response)
    output = {
        "road": road,
        "status": result.get("status"),
        "message": result.get("message"),
        "description": result.get("description"),
        "evaluation": result.get("evaluation"),
        "sections": []
    }
    for item in result.get("road_traffic") or []:
        for section in item.get("congestion_sections") or []:
            output["sections"].append({
                "road_name": item.get("road_name"),
                "section_desc": section.get("section_desc"),
                "status": section.get("status"),
                "speed": section.get("speed"),
                "congestion_distance": section.get("congestion_distance"),
                "congestion_trend": section.get("congestion_trend")
            })
    return output

if __name__ == "__main__":
    print("Sample time CST:", datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"))
    for road in ("长岛路", "金杨路"):
        try:
            print(json.dumps(query_road(road), ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({"road": road, "error_type": type(exc).__name__, "error": str(exc)[:200]}, ensure_ascii=False))

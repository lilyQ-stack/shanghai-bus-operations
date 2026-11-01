"""Independent scheduled Baidu traffic snapshots; never touches SHMAAS."""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from test_baidu_traffic import around

BJT = timezone(timedelta(hours=8))
now = datetime.now(BJT)
out = Path("commute-monitor/output")
out.mkdir(parents=True, exist_ok=True)
east = (31.26261655179258, 121.59441085013457)
west = (31.26134638455894, 121.59175860889343)
targets = [
    ("school_intersection_candidate", 31.26743581069969, 121.58741596105723, 250),
    ("bridge_east_candidate", east[0]*0.65+west[0]*0.35, east[1]*0.65+west[1]*0.35, 120),
    ("bridge_middle_candidate", (east[0]+west[0])/2, (east[1]+west[1])/2, 120),
    ("bridge_west_candidate", east[0]*0.35+west[0]*0.65, east[1]*0.35+west[1]*0.65, 120),
]
records = []
for name, lat, lng, radius in targets:
    item = {"name": name, "candidate_bd09": {"lat": round(lat, 8), "lng": round(lng, 8)}, "radius_m": radius}
    try:
        result = around(lat, lng, radius)
        item["api_status"] = result.get("status")
        item["evaluation"] = result.get("evaluation")
        item["description"] = result.get("description")
        item["roads"] = [
            {"road_name": r.get("road_name"), "congestion_sections": r.get("congestion_sections"),
             "traffic_status": r.get("traffic_status")}
            for r in (result.get("road_traffic") or [])
            if any(x in str(r.get("road_name") or "") for x in ("金杨路", "长岛路", "柳埠路"))
        ]
    except Exception as exc:
        item["error_type"] = type(exc).__name__  # Never log URLs, secrets, exception details
    records.append(item)
payload = {"timestamp_beijing": now.isoformat(), "school_day_verified": False,
           "candidate_coordinates_not_surveyed": True, "snapshots": records}
path = out / (now.strftime("%Y-%m-%d_%H-%M-%S") + ".json")
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
print("Saved snapshot:", path.name, "targets:", len(records))
print("API status:", [(x["name"], x.get("api_status", "error")) for x in records])

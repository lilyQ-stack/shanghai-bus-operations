"""Manual-only Baidu road traffic diagnostics; isolated from SHMAAS collector."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AK = os.environ.get("BAIDU_MAP_AK", "").strip()
if not AK:
    raise SystemExit("Missing BAIDU_MAP_AK GitHub Actions secret")

def query(road):
    params = urllib.parse.urlencode({"road_name": road, "city": "上海市", "ak": AK})
    request = urllib.request.Request(
        "https://api.map.baidu.com/traffic/v1/road?" + params,
        headers={"User-Agent": "bus-commute-traffic-test/1.2"},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)

def display(road, data):
    print("\n=== " + road + " ===")
    print("接口状态:", data.get("status"), data.get("message"))
    print("整体描述:", data.get("description", "未提供"))
    traffic = data.get("road_traffic") or []
    print("道路记录数:", len(traffic))
    if not traffic:
        print("具体路段: 未返回；不能据此判定局部堵点畅通")
    for i, item in enumerate(traffic[:10], 1):
        print("道路记录", i, "名称:", item.get("road_name", "未提供"),
              "状态:", item.get("status", "未提供"))
        sections = item.get("congestion_sections") or []
        print("拥堵分段数:", len(sections))
        if not sections:
            print("分段详情: 无（可能因当前畅通）")
        for j, section in enumerate(sections[:15], 1):
            print("分段", j, "位置:", section.get("section_desc", "未提供"),
                  "等级:", section.get("status", "未提供"),
                  "车速:", section.get("speed", "未提供"),
                  "趋势:", section.get("congestion_trend", "未提供"))
    print("局部定位结论: 尚未核验学校/小桥；道路名称查询不等于局部路况")

def main():
    now = datetime.now(timezone(timedelta(hours=8)))
    print("北京时间:", now.strftime("%Y-%m-%d %H:%M:%S"))
    failed = False
    for road in ("长岛路", "金杨路"):
        try:
            data = query(road)
            display(road, data)
            if data.get("status") != 0:
                failed = True
        except Exception as exc:
            # Avoid exception text: URLs may include the credential.
            print("道路:", road, "查询异常类型:", type(exc).__name__)
            failed = True
    if failed:
        raise SystemExit("Traffic query failed; inspect safe status lines")

if __name__ == "__main__":
    main()

"""Manual-only Baidu around-traffic probe. No SHMAAS reads or writes."""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

AK = os.environ.get("BAIDU_MAP_AK", "").strip()
if not AK:
    raise SystemExit("Missing BAIDU_MAP_AK secret")

TARGETS = (
    ("学校堵点", "上海市浦东新区长岛路柳埠路交叉口", 350, ("长岛路", "柳埠路")),
    ("小桥堵点", "上海市浦东新区金杨路金业路交叉口", 300, ("金杨路", "金业路", "金桥路")),
)

def request(path, params):
    params = dict(params, ak=AK)
    url = "https://api.map.baidu.com" + path + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "bus-commute-around-probe/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.load(resp)

def geocode(address):
    return request("/geocoding/v3/", {"address": address, "city": "上海市", "output": "json"})

def around(lat, lng, radius):
    return request("/traffic/v1/around", {
        "center": f"{lat:.7f},{lng:.7f}",
        "radius": radius,
        "coord_type_input": "bd09ll",
        "coord_type_output": "bd09ll",
    })

def probe(label, address, radius, roads):
    print("\n===", label, "===")
    geo = geocode(address)
    print("地理编码状态:", geo.get("status"), geo.get("message", ""))
    result = geo.get("result") or {}
    location = result.get("location") or {}
    confidence = result.get("confidence")
    precise = result.get("precise")
    level = result.get("level")
    print("定位精度:", "precise=" + str(precise), "confidence=" + str(confidence), "level=" + str(level))
    if geo.get("status") != 0 or not {"lat", "lng"} <= location.keys():
        print("周边路况: 未执行（无法定位）")
        return False
    lat, lng = float(location["lat"]), float(location["lng"])
    print("中心坐标(BD09):", round(lat, 6), round(lng, 6), "半径(米):", radius)
    # Reject obvious out-of-Shanghai geocoding results.
    if not (30.9 < lat < 31.6 and 120.8 < lng < 122.1):
        print("周边路况: 未执行（中心坐标超出上海范围）")
        return False
    data = around(lat, lng, radius)
    print("路况接口状态:", data.get("status"), data.get("message", ""))
    print("区域描述:", str(data.get("description", "未提供"))[:240])
    ev = data.get("evaluation") or {}
    print("区域拥堵等级:", ev.get("status", "未提供"), ev.get("status_desc", ""))
    traffic = data.get("road_traffic") or []
    print("返回道路数量:", len(traffic))
    matched = 0
    for item in traffic:
        road = str(item.get("road_name") or "")
        if not any(name in road for name in roads):
            continue
        matched += 1
        sections = item.get("congestion_sections") or []
        print("相关道路:", road, "拥堵分段:", len(sections))
        for section in sections[:8]:
            print("  位置:", str(section.get("section_desc", "未提供"))[:100])
            print("  等级:", section.get("status", "未提供"),
                  "速度(km/h):", section.get("speed", "未提供"),
                  "趋势:", section.get("congestion_trend", "未提供"))
    print("相关道路匹配数:", matched)
    print("定位结论: 仅为地理编码候选点，尚未核实是否落在35路实际行驶路段")
    return data.get("status") == 0

def main():
    print("北京时间:", datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"))
    failed = False
    for target in TARGETS:
        try:
            if not probe(*target):
                failed = True
        except Exception as exc:
            # Never print exception details: request URL may include AK.
            print("查询异常:", target[0], type(exc).__name__)
            failed = True
    if failed:
        raise SystemExit("Some around-traffic probes failed; inspect safe log lines")

if __name__ == "__main__":
    main()

"""One-off Pudong35 full-stop reference export. Does not touch the live sampler."""
from __future__ import annotations
import argparse
import json
from datetime import datetime,timezone,timedelta
from pathlib import Path
from shmaas_key_stop_sampler import post

ROOT=Path(__file__).resolve().parents[1]
ROUTE="浦东35路"
OUT=ROOT/"data/reference/pudong35-stop-map-full.json"

def build():
    search,_=post("/traffic/v2/querytrafficline",{"keywords":ROUTE,"type":0,"pageNo":1,"pageSize":20})
    candidates=[]
    for item in search.get("trafficStop") or []:
        candidates.extend(x for x in item.get("stopLineInfo") or [] if x.get("lineName")==ROUTE)
        info=item.get("lineInfo") or {}
        if info.get("lineName")==ROUTE:candidates.append(info)
    ids={str(x.get("lineId")) for x in candidates if x.get("lineId")}
    if len(ids)!=1:raise RuntimeError(f"Expected unique line ID, got {sorted(ids)}")
    line_id=ids.pop()
    directions=[]
    for d in (0,1):
        payload,_=post("/traffic/v1/querybusline",{"lineId":line_id,"lineName":ROUTE,"direction":d})
        line=payload.get("busLine") or {}
        raw=line.get("stop") or []
        if not raw or any(not s.get("stopId") or not s.get("stopName") for s in raw):
            raise RuntimeError(f"Invalid stop list direction={d}")
        stops=[{"seq":i,"stop_id":str(s["stopId"]),"stop_name":str(s["stopName"]).strip()} for i,s in enumerate(raw,1)]
        if len({x["seq"] for x in stops})!=len(stops):raise RuntimeError("Duplicate sequences")
        directions.append({"direction":d,"start_stop":line.get("upStartStop"),"end_stop":line.get("upEndStop"),"stop_count":len(stops),"verified_stops":stops})
    if [x["stop_count"] for x in directions]!=[64,65]:
        raise RuntimeError(f"Stop count changed: {[x['stop_count'] for x in directions]}; review before saving")
    return {"route":ROUTE,"source":"SHMAAS querybusline (one-off independent reference read)","line_id":line_id,
            "captured_at_cst":datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "status":"full_api_stop_list_unreviewed","directions":directions}

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--write",action="store_true",help="Write reference file after validation")
    args=parser.parse_args()
    data=build()
    print(json.dumps({"route":data["route"],"line_id":data["line_id"],"counts":[d["stop_count"] for d in data["directions"]],"captured_at_cst":data["captured_at_cst"]},ensure_ascii=False))
    if args.write:
        OUT.parent.mkdir(parents=True,exist_ok=True)
        OUT.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(f"Written: {OUT}")

if __name__=="__main__":main()

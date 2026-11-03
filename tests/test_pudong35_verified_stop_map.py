"""Regression for safe physical-sequence stop labels (no collector changes)."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import postprocess_operations as p

def test_verified_partial_map_preserves_exact_stops():
    maps=p.load_verified_stop_maps()
    assert maps[("浦东35路",0)][29]=="丁香路锦绣路"
    assert maps[("浦东35路",1)][21]=="沪南路华夏西路"

def test_unverified_midpoint_stays_unmapped():
    maps=p.load_verified_stop_maps()
    for direction,seq in [(0,31),(1,22),(0,27),(1,24),(0,30)]:
        assert p.mapped_stop("浦东35路",direction,seq,maps)==f"物理seq{seq}（站名待映射）"

def test_known_other_route_unaffected():
    maps=p.load_verified_stop_maps()
    assert p.mapped_stop("浦东78路",1,15,maps)=="物理seq15（站名待映射）"

"""Regression for safe physical-sequence stop labels (no collector changes)."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
import postprocess_operations as p

def test_full_map_exact_stops():
    maps=p.load_verified_stop_maps()
    assert len(maps[("浦东35路",0)])==64
    assert len(maps[("浦东35路",1)])==65
    expected={(0,31):"迎春路芳甸路",(1,22):"沪南路高青路",
              (0,27):"黑松路锦绣东路",(1,24):"沪南路北中路",
              (0,30):"迎春路紫槐路"}
    for (direction,seq),name in expected.items():
        assert p.mapped_stop("浦东35路",direction,seq,maps)==name

def test_sampled_stops_still_match():
    maps=p.load_verified_stop_maps()
    assert maps[("浦东35路",0)][29]=="丁香路锦绣路"
    assert maps[("浦东35路",1)][21]=="沪南路华夏西路"

def test_out_of_range_unmapped():
    maps=p.load_verified_stop_maps()
    assert p.mapped_stop("浦东35路",0,65,maps)=="物理seq65（站名待映射）"
    assert p.mapped_stop("浦东35路",1,66,maps)=="物理seq66（站名待映射）"

def test_other_route_unaffected():
    maps=p.load_verified_stop_maps()
    assert p.mapped_stop("浦东78路",1,15,maps)=="物理seq15（站名待映射）"

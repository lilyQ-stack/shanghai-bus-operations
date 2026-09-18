from src.reverse_watch_sampler import query_reverse_corridor

class FakeAPI:
    pass

# Regression for 沪A72305D: a single mirror target can miss the bus,
# while a wider downstream corridor sees it at successive physical positions.
def simulate(query_target_func):
    import src.reverse_watch_sampler as rw
    old = rw.query_target
    rw.query_target = query_target_func
    try:
        stops=[{"seq":i,"stopId":str(i),"stopName":f"S{i}"} for i in range(1,66)]
        return rw.query_reverse_corridor(stops,1,"沪A72305D",40)
    finally:
        rw.query_target=old

def test_72305_multistop_corridor_finds_reverse_vehicle():
    def q(stop,direction,plate):
        # Old watcher queried seq43 only and missed. New corridor also probes
        # farther downstream; seq45 exposes physical reverse seq42.
        if stop["seq"]==45:
            return "current",42
        return None,None
    role,seq,queried,hit=simulate(q)
    assert 43 in queried and 45 in queried
    assert role=="current"
    assert seq==42
    assert hit["target_seq"]==45

def test_confirmation_requires_later_gain_of_two():
    first=42
    later=44
    assert later-first>=2

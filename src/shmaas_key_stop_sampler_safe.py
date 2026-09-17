from __future__ import annotations

import shmaas_key_stop_sampler as core

MAIN_SAMPLE_MINUTES = 8


def iter_vehicles(snapshot: dict):
    vehicles = snapshot.get("vehicles") or []
    if isinstance(vehicles, dict):
        return list(vehicles.values())
    return list(vehicles)


def observation_max_seq(snapshot: dict, plate: str, direction: int):
    for vehicle in iter_vehicles(snapshot):
        if not isinstance(vehicle, dict) or vehicle.get("plate") != plate:
            continue
        seqs = [
            int(obs["stop_seq"])
            for obs in vehicle.get("observations") or []
            if obs.get("direction") == direction
            and obs.get("role") in ("current", "next")
            and obs.get("stop_seq") is not None
        ]
        return max(seqs) if seqs else None
    return None


def plates_by_direction(snapshot: dict) -> dict[int, set[str]]:
    result = {0: set(), 1: set()}
    for vehicle in iter_vehicles(snapshot):
        if not isinstance(vehicle, dict):
            continue
        plate = vehicle.get("plate")
        if not plate:
            continue
        for obs in vehicle.get("observations") or []:
            if obs.get("role") not in ("current", "next"):
                continue
            direction = obs.get("direction")
            if direction in result:
                result[direction].add(plate)
    return result


def sampling_phase_8min(now):
    # Use elapsed minutes since midnight, not minute-of-hour, so the rotating
    # key-stop phase advances consistently across hour boundaries on an 8-minute cadence.
    return ((now.hour * 60 + now.minute) // MAIN_SAMPLE_MINUTES) % core.SAMPLE_STRIDE


def no_legacy_immediate_watch(history, current, stop_counts):
    """Keep reverse tracking in the dedicated watcher; main classifier is authoritative."""
    return []


# Preserve the complete stop list returned by SHMAAS querybusline.  The core
# sampler already downloads it to choose backbone stops, but historically only
# sampled_stops was written to the snapshot.  Keeping the same-source full list
# lets postprocess map reconstructed physical_seq values without a third-party
# table or a guessed +/-1 sequence offset.
_original_post = core.post
_full_stops_by_direction: dict[int, list[dict]] = {}


def post_with_full_stop_capture(path: str, payload: dict, timeout: int = 12, retries: int = 2):
    data, elapsed = _original_post(path, payload, timeout=timeout, retries=retries)
    if path == "/traffic/v1/querybusline":
        try:
            direction = int(payload.get("direction"))
            bus_line = data.get("busLine") or {}
            stops = []
            for seq, stop in enumerate(bus_line.get("stop") or [], 1):
                if not stop.get("stopId") or not stop.get("stopName"):
                    continue
                stops.append({
                    "seq": seq,
                    "stop_id": str(stop["stopId"]),
                    "stop_name": str(stop["stopName"]),
                })
            _full_stops_by_direction[direction] = stops
        except (TypeError, ValueError):
            pass
    return data, elapsed


_original_collect = core.collect


def collect_with_full_stop_metadata(route: str, history=None):
    _full_stops_by_direction.clear()
    snapshot = _original_collect(route, history=history)
    for dmeta in snapshot.get("directions") or []:
        try:
            direction = int(dmeta.get("direction"))
        except (TypeError, ValueError):
            continue
        stops = _full_stops_by_direction.get(direction)
        if stops:
            dmeta["stops"] = stops
    return snapshot


core.observation_max_seq = observation_max_seq
core.plates_by_direction = plates_by_direction
core.sampling_phase = sampling_phase_8min
core.build_watch_candidates = no_legacy_immediate_watch
core.post = post_with_full_stop_capture
core.collect = collect_with_full_stop_metadata

if __name__ == "__main__":
    raise SystemExit(core.main())

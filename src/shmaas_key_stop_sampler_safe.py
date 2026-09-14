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
    """Keep all reverse tracking in the dedicated 5-minute watcher.

    A vehicle becomes a reverse-watch target after the first main sample that no
    longer contains its same-direction movement. The dedicated watcher starts at
    the mirrored reverse position +3 stops and confirms a short turn after >=5
    reverse stops of progress.
    """
    return []


core.observation_max_seq = observation_max_seq
core.plates_by_direction = plates_by_direction
core.sampling_phase = sampling_phase_8min
core.build_watch_candidates = no_legacy_immediate_watch

if __name__ == "__main__":
    raise SystemExit(core.main())

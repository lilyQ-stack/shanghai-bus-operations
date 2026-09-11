from __future__ import annotations

import shmaas_key_stop_sampler as core


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


core.observation_max_seq = observation_max_seq
core.plates_by_direction = plates_by_direction

if __name__ == "__main__":
    raise SystemExit(core.main())

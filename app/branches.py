import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Branch:
    id: str
    name: str
    address: str
    lat: float | None
    lon: float | None
    radius_m: int
    # True when lat/lon were borrowed from the configured restaurant pin because this record had
    # none. The delivery origin then comes from the environment, not from branches.json, and the
    # startup log must say so rather than crediting the file that supplied only the address.
    from_fallback: bool = False


def load_branches(path: str, fallback_lat: float | None = None,
                  fallback_lon: float | None = None) -> list[Branch]:
    """Read branches.json. A record with no coordinates borrows the configured restaurant pin."""
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    branches = []
    for r in records:
        lat = float(r["lat"]) if r.get("lat") is not None else None
        lon = float(r["lon"]) if r.get("lon") is not None else None
        from_fallback = False
        if (lat is None or lon is None) and fallback_lat is not None and fallback_lon is not None:
            lat, lon, from_fallback = float(fallback_lat), float(fallback_lon), True
        if lat is None or lon is None:
            lat = lon = None
        branches.append(Branch(r["id"], r["name"], r["address"], lat, lon, int(r["radius_m"]),
                               from_fallback))
    return branches

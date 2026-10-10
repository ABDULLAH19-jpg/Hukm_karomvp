"""Geoapify client: forward geocoding and road distance. Everything else talks to the GeoClient shape,
so another provider (Google) can replace this file without touching the delivery rules."""
import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.errors import Unavailable
from app.logging_setup import logger

# How exactly a match pins down a doorstep. Only exact and street matches may be offered to the customer.
PRECISION_BY_TYPE = {"building": "exact", "amenity": "exact", "street": "street", "suburb": "area",
                     "district": "area", "postcode": "area", "unknown": "area",
                     "city": "too_coarse", "county": "too_coarse", "state": "too_coarse", "country": "too_coarse"}

MIN_HOUSE_CONFIDENCE = 0.3
CACHE_TTL_S = 3600  # the same address asked twice (a retry, a correction) costs nothing
CACHE_MAX = 512


@dataclass(frozen=True)
class GeoCandidate:
    label: str
    lat: float
    lon: float
    precision: str  # exact | street | area | too_coarse
    confidence: float
    place_id: str | None = None


class GeoClient(Protocol):
    def geocode(self, text: str, near: tuple[float, float], radius_m: int, limit: int = 5) -> list[GeoCandidate]: ...
    def reverse_geocode(self, lat: float, lon: float) -> GeoCandidate | None: ...

    def route_distance_m(self, origin: tuple[float, float], dest: tuple[float, float]) -> int: ...


class GeoapifyClient:
    BASE = "https://api.geoapify.com/v1"

    def __init__(self, api_key: str, timeout: float = 6.0, clock=time.monotonic):
        # httpx logs every request URL at INFO, and the key travels in the URL. Silence it here so no
        # caller can forget to.
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        self._key = api_key
        # A fresh connection costs DNS + TLS (up to ~3 s measured). httpx drops idle connections after 5 s by
        # default, which made nearly every customer pay for it, so keep them for two minutes.
        self._http = httpx.Client(timeout=httpx.Timeout(timeout, connect=3.0),
                                  limits=httpx.Limits(max_keepalive_connections=8, keepalive_expiry=120))
        self._clock = clock
        self._cache: dict[tuple, tuple[float, list[GeoCandidate]]] = {}
        self._lock = threading.Lock()

    def warm_up(self) -> threading.Thread:
        """Open the connection in the background so the first customer does not pay for it."""
        def run():
            try:
                self.geocode("Lahore", (31.5, 74.3), 50_000, limit=1)
            except Unavailable:
                pass  # already logged, and the first real lookup will try again
        thread = threading.Thread(target=run, daemon=True, name="geo-warmup")
        thread.start()
        return thread

    def _get(self, path: str, params: dict, retry: bool = True) -> dict:
        """One retry on a timeout or a 5xx. Both calls are read-only lookups, so repeating them is safe.
        Geocoding passes retry=False: the caller already hedges with a second query, and retrying a slow
        call would double the wait."""
        try:
            return self._get_once(path, params)
        except Unavailable as first:
            if not retry or first.code not in ("geo_timeout", "geo_unavailable") or getattr(first, "no_retry", False):
                raise
            logger.info("geo_retry", extra={"path": path, "after": first.code})
            return self._get_once(path, params)

    def _get_once(self, path: str, params: dict) -> dict:
        try:
            resp = self._http.get(f"{self.BASE}/{path}", params={**params, "apiKey": self._key})
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            logger.warning("geo_timeout", extra={"path": path})
            raise Unavailable("geo_timeout", "the map service is too slow right now")
        except httpx.HTTPStatusError as exc:  # never log the exception text: it contains the URL and key
            logger.warning("geo_http_error", extra={"path": path, "status": exc.response.status_code})
            err = Unavailable("geo_unavailable", "the map service is unavailable")
            err.no_retry = 400 <= exc.response.status_code < 500 and exc.response.status_code != 429
            raise err
        except (httpx.HTTPError, ValueError):
            logger.warning("geo_error", extra={"path": path})
            raise Unavailable("geo_unavailable", "the map service is unavailable")

    def geocode(self, text: str, near: tuple[float, float], radius_m: int, limit: int = 5) -> list[GeoCandidate]:
        lat, lon = near
        key = (" ".join(text.lower().split()), round(lat, 5), round(lon, 5), radius_m, limit)
        with self._lock:
            hit = self._cache.get(key)
        if hit and self._clock() - hit[0] < CACHE_TTL_S:
            return list(hit[1])
        data = self._get("geocode/search", {
            "text": text, "format": "json", "limit": limit, "lang": "en",
            "filter": f"circle:{lon},{lat},{radius_m}", "bias": f"proximity:{lon},{lat}"}, retry=False)
        found = []
        for r in data.get("results", []):
            confidence = float(r.get("rank", {}).get("confidence", 0) or 0)
            if r.get("housenumber"):
                # Seen in real Lahore data: "5 Street 1" comes back as a house match at confidence 0 for the query
                # "Phase 5". Real house matches score 0.45 and up. Streets and landmarks legitimately score near 0.
                kind = "exact" if confidence >= MIN_HOUSE_CONFIDENCE else "area"
            else:
                kind = PRECISION_BY_TYPE.get(r.get("result_type", "unknown"), "area")
            found.append(GeoCandidate(r.get("formatted") or r.get("address_line1") or "unnamed place",
                                      float(r["lat"]), float(r["lon"]), kind, confidence,
                                      r.get("place_id")))
        with self._lock:  # only successful answers are cached, a failure raised above and is never stored
            if len(self._cache) >= CACHE_MAX:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (self._clock(), found)
        return list(found)

    def route_distance_m(self, origin: tuple[float, float], dest: tuple[float, float]) -> int:
        data = self._get("routing", {"waypoints": f"{origin[0]},{origin[1]}|{dest[0]},{dest[1]}",
                                     "mode": "drive", "units": "metric"})
        try:
            return round(data["features"][0]["properties"]["distance"])
        except (KeyError, IndexError, TypeError):
            raise Unavailable("route_not_found", "no driving route could be found to that address")

    def reverse_geocode(self, lat: float, lon: float) -> GeoCandidate | None:
        data = self._get("geocode/reverse", {"lat": lat, "lon": lon, "format": "json", "lang": "en"})
        result = (data.get("results") or [None])[0]
        if not result or "lat" not in result or "lon" not in result:
            return None
        return GeoCandidate(result.get("formatted") or result.get("address_line1") or "detected location",
                            float(result["lat"]), float(result["lon"]), "exact", 1.0,
                            result.get("place_id"))

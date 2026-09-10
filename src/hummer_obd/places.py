"""Named places, and which one the vehicle is sitting in.

A trip between two coordinates is a fact nobody remembers. A trip from home to
work is one they do. This turns the first into the second, and it is
deliberately a small amount of machinery: a list of circles with names, a
point-in-circle test, and a file.

Nothing here calls a geocoder. The vehicle has been telling us where home is
every night for weeks -- it is the place the truck sits -- and a rooftop pin
from a postal address is a worse fence than the driveway the truck actually
parks in. Coordinates come from the recorder's own fixes, or from the owner
typing them, and neither route sends an address anywhere.

The file is JSON, on the node, outside the repository. Home addresses are not
telemetry and this project does not commit them.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Optional

#: Default fence radius. Big enough to cover a house and its driveway with the
#: GPS error this vehicle actually sees -- fixes wander a measured 46 m while
#: parked, and gps_epx_m has been observed at 72.9 m -- and small enough not to
#: swallow the neighbours.
DEFAULT_RADIUS_M = 120.0

#: A fence below this is smaller than the receiver's own error and would flap
#: between inside and outside while the vehicle sat still.
MIN_RADIUS_M = 40.0

#: Beyond this a "place" is a district, not a place.
MAX_RADIUS_M = 5000.0

_EARTH_RADIUS_M = 6371000.0


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


@dataclass(frozen=True)
class Place:
    """One named circle on the map."""

    name: str
    lat: float
    lon: float
    radius_m: float = DEFAULT_RADIUS_M
    #: Free text the owner attached. Never interpreted, only shown.
    note: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("a place needs a name")
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"latitude out of range: {self.lat}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"longitude out of range: {self.lon}")
        if not MIN_RADIUS_M <= self.radius_m <= MAX_RADIUS_M:
            raise ValueError(
                f"radius must be {MIN_RADIUS_M:.0f}-{MAX_RADIUS_M:.0f} m, "
                f"not {self.radius_m}")

    def contains(self, lat: float, lon: float) -> bool:
        return _distance_m(self.lat, self.lon, lat, lon) <= self.radius_m

    def distance_m(self, lat: float, lon: float) -> float:
        return _distance_m(self.lat, self.lon, lat, lon)


class Places:
    """The list, and the file it lives in."""

    def __init__(self, places: Optional[Iterable[Place]] = None) -> None:
        self._places: list[Place] = list(places or ())

    def __len__(self) -> int:
        return len(self._places)

    def __iter__(self):
        return iter(self._places)

    def add(self, place: Place) -> None:
        """Add or replace by name. Names are the identity, so re-adding one
        edits it rather than producing two fences with the same label."""
        self._places = [p for p in self._places if p.name != place.name]
        self._places.append(place)

    def remove(self, name: str) -> bool:
        before = len(self._places)
        self._places = [p for p in self._places if p.name != name]
        return len(self._places) != before

    def at(self, lat: Optional[float], lon: Optional[float]) -> Optional[Place]:
        """The place this fix is in, or None.

        The SMALLEST containing fence wins, with distance from its centre as
        the tie-break. Fences overlap in life -- a home inside a
        neighbourhood, a suite inside an office park -- and the tighter one is
        what a person means.

        Smallest rather than nearest, which was the first rule here and is
        wrong for the case that matters: concentric fences share a centre, so
        every distance is equal and the answer falls to whichever happened to
        be defined first. A trip would get labelled by the coarsest fence
        around it.
        """
        if lat is None or lon is None:
            return None
        inside = [p for p in self._places if p.contains(lat, lon)]
        if not inside:
            return None
        return min(inside, key=lambda p: (p.radius_m, p.distance_m(lat, lon)))

    def label(self, lat: Optional[float], lon: Optional[float]) -> Optional[str]:
        found = self.at(lat, lon)
        return found.name if found else None

    # --- persistence ---

    def to_json(self) -> str:
        return json.dumps({"places": [asdict(p) for p in self._places]},
                          indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Places":
        """Parse, skipping entries that do not describe a place.

        One bad row must not cost the whole file. A place list is edited by
        hand, and the failure mode of refusing everything is that a typo in
        one fence silently unlabels every trip.
        """
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            return cls()
        rows = data.get("places") if isinstance(data, dict) else data
        # A bare number or string parses as valid JSON and is not iterable.
        if not isinstance(rows, list):
            return cls()
        out = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                out.append(Place(
                    name=str(row["name"]),
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                    radius_m=float(row.get("radius_m", DEFAULT_RADIUS_M)),
                    note=str(row.get("note", "")),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        return cls(out)

    @classmethod
    def load(cls, path: Path) -> "Places":
        try:
            return cls.from_json(path.read_text(encoding="utf-8"))
        except OSError:
            return cls()

    def save(self, path: Path) -> None:
        """Write atomically. A half-written place file read by the next poll
        is an unlabelled map, and the rename is one syscall."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(path.parent), delete=False)
        try:
            handle.write(self.to_json())
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            os.replace(handle.name, path)
        except BaseException:
            handle.close()
            try:
                os.unlink(handle.name)
            except OSError:
                pass
            raise

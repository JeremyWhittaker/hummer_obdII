"""Map tiles, fetched one at a time and then kept forever.

The map was drawn on black. The page's CSP allows images from its own origin
only, and that was the right default for a reason worth restating: asking a
tile server for the squares around a coordinate tells that server, fairly
precisely, where the vehicle is. This project went to some trouble to keep
that local.

Serving tiles through this process is the compromise. The browser never talks
to the tile server -- this node does, one tile at a time, only for ground the
vehicle actually covered, and every tile is written to disk and never asked
for twice. What the tile server learns is bounded by where the truck has
already been, and it learns it once.

The OSM Foundation's tile usage policy is binding on this and is followed to
the letter rather than the gist:

  * A stable, identifying User-Agent. The policy names generic library
    defaults -- ``python-urllib``, ``curl``, ``requests`` -- and says traffic
    using them is blocked because the operators cannot identify or contact
    whoever is responsible.
  * No bulk downloading. "Bulk downloading is any pre-emptive fetching of
    tiles other than those a user is actively viewing", and pre-seeding areas
    or zoom levels in advance is called out by name. Nothing here fetches a
    tile the page has not asked to draw.
  * A local cache. The policy's "at least 7 days" is a floor, not a ceiling,
    and re-visits served from cache are the behaviour it asks for.
  * Attribution on the map, which the page renders.

  https://operations.osmfoundation.org/policies/tiles/

Two providers that would otherwise fit were ruled out on their own terms
rather than on taste: CARTO forbids "proxying or caching the content on the
server side", and Thunderforest forbids caching proxies outright. This design
is exactly what both prohibit, so neither is used.
"""

from __future__ import annotations

import errno
import math
import os
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from pathlib import Path
from typing import Optional

#: Where tiles come from. The standard OSM raster layer, whose policy is
#: quoted above and implemented below.
TILE_HOST = "tile.openstreetmap.org"

#: The policy requires an application-identifying User-Agent with a contact.
#: A generic one is blocked by name, so this is not decoration.
USER_AGENT = (
    "hummer-obd-node/0.1 (personal vehicle telemetry dashboard; "
    "+https://github.com/JeremyWhittaker/hummer_obdII)"
)

#: Below 3 the whole world is four tiles and there is nothing to see; above 17
#: the standard style is drawing building outlines, which is more detail than a
#: driving dashboard needs and more requests than it should make.
MIN_ZOOM = 3
MAX_ZOOM = 17

#: A standard 256px PNG tile is a few kilobytes. Anything approaching this is
#: not a tile, and is refused rather than cached.
MAX_TILE_BYTES = 512 * 1024

#: Upstream is a donation-funded service on the other side of the internet
#: from a Pi Zero. Fail fast rather than holding a request thread open.
FETCH_TIMEOUT_S = 8.0

#: A browser bug or a wild zoom-out must not turn this node into a scraper.
#: The policy's prohibition is on pre-emptive fetching, and this is the
#: mechanical backstop for it: a token bucket, refilled slowly, that bounds
#: outbound requests no matter what the page asks for. Cache hits are free and
#: never touch it, so ordinary use never notices.
BUCKET_CAPACITY = 120
BUCKET_REFILL_PER_S = 0.5

#: Stop growing the cache at some point. 20k tiles is far more ground than
#: this vehicle will cover, and about 200 MiB at typical tile sizes.
MAX_CACHE_FILES = 20000

#: How many tiles to hold in memory when the disk cache is unavailable.
#: The dashboard's systemd unit runs ProtectSystem=strict and
#: ProtectHome=read-only -- it is deliberately forbidden from writing
#: anything, which is a good property for a read-only telemetry service and
#: not one to quietly drill a hole through. When that is the case there is
#: still an obligation not to ask the tile server for the same square twice,
#: so the cache moves into memory for the life of the process. At roughly
#: 35 KiB a tile this is about 9 MiB, on a node with 415.
MEMORY_TILES = 256


class TileError(RuntimeError):
    """A tile could not be produced, with a reason fit to show a user."""


class _Bucket:
    """A token bucket, so a runaway client cannot become a bulk download."""

    def __init__(self, capacity: int = BUCKET_CAPACITY,
                 refill_per_s: float = BUCKET_REFILL_PER_S):
        self.capacity = capacity
        self.refill = refill_per_s
        self._tokens = float(capacity)
        # Monotonic: a clock step -- and this node sets its clock from GPS at
        # boot -- must not hand out a burst of free tokens.
        self._at = time.monotonic()
        self._lock = threading.Lock()

    def take(self, now: Optional[float] = None) -> bool:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            elapsed = max(0.0, stamp - self._at)
            self._at = stamp
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill)
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


def valid(z: int, x: int, y: int) -> bool:
    """Whether (z, x, y) names a tile that can exist.

    This is the only thing standing between a URL path and the filesystem, so
    it rejects rather than clamps: a request outside the pyramid is a bug or
    an attack, and answering it with a neighbouring tile would hide both.
    """
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (z, x, y)):
        return False
    if not MIN_ZOOM <= z <= MAX_ZOOM:
        return False
    span = 1 << z
    return 0 <= x < span and 0 <= y < span


def deg_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """The tile containing a coordinate, in the usual Web Mercator scheme."""
    span = 1 << zoom
    x = int((lon + 180.0) / 360.0 * span)
    radians = math.radians(max(-85.05112878, min(85.05112878, lat)))
    y = int((1.0 - math.asinh(math.tan(radians)) / math.pi) / 2.0 * span)
    return max(0, min(span - 1, x)), max(0, min(span - 1, y))


class TileStore:
    """Tiles on local disk, fetched on a miss and then kept."""

    def __init__(self, directory: str | Path, *, session: Optional[object] = None):
        self.directory = Path(directory).resolve()
        self._bucket = _Bucket()
        self._lock = threading.Lock()
        self._count: Optional[int] = None
        self._memory: OrderedDict[tuple[int, int, int], bytes] = OrderedDict()
        self.writable = self._probe()
        # Injectable so the tests never reach the network. Nothing in the
        # suite is allowed to depend on a donation-funded server being up.
        self._fetch = session or self._fetch_upstream

    def _probe(self) -> bool:
        """Whether tiles can be kept on disk, decided once by trying it.

        Asked rather than assumed, because the answer is a property of how
        the service was launched and not of this code. Under a hardened unit
        the honest answer is no, and the caller needs to know that before it
        starts telling anyone tiles are cached.
        """
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            probe = self.directory / ".writable"
            probe.write_bytes(b"")
            probe.unlink()
            return True
        except OSError:
            return False

    def path(self, z: int, x: int, y: int) -> Path:
        """Where a tile lives. Only ever called with a validated triple."""
        return self.directory / str(z) / str(x) / f"{y}.png"

    def cached(self, z: int, x: int, y: int) -> Optional[bytes]:
        # Disk first even when it cannot be written: a cache seeded by some
        # other run is still a cache, and reading it is still allowed.
        try:
            return self.path(z, x, y).read_bytes()
        except OSError:
            pass
        with self._lock:
            body = self._memory.get((z, x, y))
            if body is not None:
                self._memory.move_to_end((z, x, y))
            return body

    def _fetch_upstream(self, z: int, x: int, y: int) -> bytes:
        request = urllib.request.Request(
            f"https://{TILE_HOST}/{z}/{x}/{y}.png",
            headers={"User-Agent": USER_AGENT, "Accept": "image/png"},
        )
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_S) as response:
            if response.status != 200:
                raise TileError(f"tile server answered {response.status}")
            body = response.read(MAX_TILE_BYTES + 1)
        if len(body) > MAX_TILE_BYTES:
            raise TileError("tile larger than a tile has any business being")
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise TileError("tile server did not answer with a PNG")
        return body

    def _files(self) -> int:
        """How many tiles are held. Counted once, then tracked."""
        with self._lock:
            if self._count is None:
                try:
                    self._count = sum(1 for _ in self.directory.rglob("*.png"))
                except OSError:
                    self._count = 0
            return self._count

    def _remember(self, z: int, x: int, y: int, body: bytes) -> None:
        with self._lock:
            self._memory[(z, x, y)] = body
            self._memory.move_to_end((z, x, y))
            while len(self._memory) > MEMORY_TILES:
                self._memory.popitem(last=False)

    def _store(self, z: int, x: int, y: int, body: bytes) -> None:
        # Guarded here rather than only at the fetch, because this is the step
        # that makes a body permanent. A cache hit is never revalidated, so
        # anything written wrong -- a captive portal's login page, an error
        # document answered with 200 -- would be served as that tile for good.
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):
            raise TileError("refusing to cache something that is not a PNG")
        if len(body) > MAX_TILE_BYTES:
            raise TileError("refusing to cache an oversized tile")
        if not self.writable:
            self._remember(z, x, y, body)
            return
        target = self.path(z, x, y)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Written beside and renamed, so a crash mid-write cannot leave a
        # truncated PNG that would then be served from cache forever.
        temporary = target.with_suffix(".part")
        try:
            temporary.write_bytes(body)
            os.replace(temporary, target)
        except OSError as error:
            # The disk went away under us mid-run. Keep serving from memory
            # rather than failing the request: the tile is already fetched,
            # and refusing to hand it over would only make the page ask
            # again, which is the one thing not to do.
            self.writable = False
            self._remember(z, x, y, body)
            if error.errno == errno.ENOSPC:
                return
            return
        with self._lock:
            if self._count is not None:
                self._count += 1

    def get(self, z: int, x: int, y: int) -> bytes:
        """A tile, from disk if it is there and from upstream once if not."""
        if not valid(z, x, y):
            raise TileError("no such tile")
        hit = self.cached(z, x, y)
        if hit is not None:
            return hit
        if self.writable and self._files() >= MAX_CACHE_FILES:
            raise TileError("tile cache is full")
        if not self._bucket.take():
            # Deliberately not a retry loop. The policy's concern is volume,
            # and the honest answer to "you are asking too fast" is to stop.
            raise TileError("asking the tile server too quickly; slow down")
        try:
            body = self._fetch(z, x, y)
        except TileError:
            raise
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise TileError(f"tile unavailable: {error}") from error
        self._store(z, x, y, body)
        return body

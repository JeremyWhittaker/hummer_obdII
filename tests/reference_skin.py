"""The body the page draws, decoded with the standard library.

dashboard.html carries the exterior of "Hummer EV - Low Poly" by Ajay Gawde
(CC-BY-4.0) as REF_BODY: per colour group, int16 positions over the group's
bounding box, int8 normals and uint16 indices, placed by buildScene() with
that box. Tests measure parts against the skin the page actually draws, so
this decodes those bytes rather than trusting numbers copied out of them.

numpy is not a dependency of this project, so the skin is rasterised into
5 cm extent maps once: for every cell of a plane, the extreme coordinate of
the skin along the axis normal to it. That turns "is this corner inside the
body" into dictionary lookups.
"""

import base64
import hashlib
import json
import math
import re
import struct

CELL = 0.05


def ref_groups(page: str) -> list[dict]:
    match = re.search(r"  var REF_BODY = (\[.*?\]);\n", page)
    assert match, "REF_BODY not found in dashboard.html"
    return json.loads(match.group(1))


def placements(page: str) -> dict:
    """group name -> (t, s), read from buildScene's REF_PLACEMENTS table."""
    match = re.search(r"var REF_PLACEMENTS = (\[[\s\S]*?\n    \]);", page)
    assert match, "REF_PLACEMENTS not found in dashboard.html"
    return {row[0]: (row[1], row[2]) for row in json.loads(match.group(1))}


def triangles(page: str) -> list[tuple]:
    """(group, a, b, c) in metres."""
    placed = placements(page)
    out = []
    for group in ref_groups(page):
        t, s = placed[group["name"]]
        raw = base64.b64decode(group["v"])
        v = struct.unpack("<%dh" % (len(raw) // 2), raw)
        raw = base64.b64decode(group["i"])
        idx = struct.unpack("<%dH" % (len(raw) // 2), raw)
        pts = [tuple(t[a] + v[k + a] / 65534 * s[a] for a in range(3))
               for k in range(0, len(v), 3)]
        out.extend((group["name"], pts[idx[k]], pts[idx[k + 1]], pts[idx[k + 2]])
                   for k in range(0, len(idx), 3))
    return out


class Extents:
    """Extreme skin coordinate along one axis, per 5 cm cell of the other two.

    axis 0 (x): cells over (y, z); axis 1 (y): cells over (x, z); axis 2 (z):
    cells over (x, y). `hi` and `lo` hold the largest and smallest value hit
    in each cell.
    """

    def __init__(self, tris, axis, groups=None):
        self.axis = axis
        self.i, self.j = [a for a in range(3) if a != axis]
        self.hi, self.lo = {}, {}
        for name, a, b, c in tris:
            if groups is None or name in groups:
                self._raster(a, b, c)

    def _raster(self, a, b, c):
        i, j, k = self.i, self.j, self.axis
        d = (b[j] - c[j]) * (a[i] - c[i]) + (c[i] - b[i]) * (a[j] - c[j])
        if abs(d) < 1e-12:
            return
        for ci in range(math.ceil(min(a[i], b[i], c[i]) / CELL), math.floor(max(a[i], b[i], c[i]) / CELL) + 1):
            for cj in range(math.ceil(min(a[j], b[j], c[j]) / CELL), math.floor(max(a[j], b[j], c[j]) / CELL) + 1):
                pi, pj = ci * CELL, cj * CELL
                l1 = ((b[j] - c[j]) * (pi - c[i]) + (c[i] - b[i]) * (pj - c[j])) / d
                l2 = ((c[j] - a[j]) * (pi - c[i]) + (a[i] - c[i]) * (pj - c[j])) / d
                l3 = 1 - l1 - l2
                if min(l1, l2, l3) < -1e-9:
                    continue
                h = l1 * a[k] + l2 * b[k] + l3 * c[k]
                key = (ci, cj)
                if h > self.hi.get(key, -1e9):
                    self.hi[key] = h
                if h < self.lo.get(key, 1e9):
                    self.lo[key] = h

    def at(self, u, v, which="hi"):
        """Extreme at the cell nearest (u, v), or None where the skin is absent."""
        table = self.hi if which == "hi" else self.lo
        return table.get((round(u / CELL), round(v / CELL)))

    def near(self, u, v, which="hi", reach=1):
        """The most extreme value within `reach` cells -- for a corner that falls
        between two cells, the generous answer."""
        table = self.hi if which == "hi" else self.lo
        cu, cv = round(u / CELL), round(v / CELL)
        vals = [table[(cu + du, cv + dv)] for du in range(-reach, reach + 1)
                for dv in range(-reach, reach + 1) if (cu + du, cv + dv) in table]
        if not vals:
            return None
        return max(vals) if which == "hi" else min(vals)


def extreme(tris, axis, which, span_u, span_v, samples=6, where=None):
    """The skin's largest ('hi') or smallest ('lo') coordinate along `axis` over a
    rectangular footprint of the other two axes, sampled exactly on every
    triangle crossing it -- not read off the 5 cm maps, which misread a sloped
    lens by up to 2 cm. `where(a, b, c)` may restrict which triangles count, for
    inner surfaces an outer extent cannot see. None where nothing is there."""
    i, j = [a for a in range(3) if a != axis]
    us = [span_u[0] + (span_u[1] - span_u[0]) * k / (samples - 1) for k in range(samples)]
    vs = [span_v[0] + (span_v[1] - span_v[0]) * k / (samples - 1) for k in range(samples)]
    best = None
    for _, a, b, c in tris:
        if max(a[i], b[i], c[i]) < span_u[0] or min(a[i], b[i], c[i]) > span_u[1]:
            continue
        if max(a[j], b[j], c[j]) < span_v[0] or min(a[j], b[j], c[j]) > span_v[1]:
            continue
        if where is not None and not where(a, b, c):
            continue
        d = (b[j] - c[j]) * (a[i] - c[i]) + (c[i] - b[i]) * (a[j] - c[j])
        if abs(d) < 1e-12:
            continue
        for u in us:
            for v in vs:
                l1 = ((b[j] - c[j]) * (u - c[i]) + (c[i] - b[i]) * (v - c[j])) / d
                l2 = ((c[j] - a[j]) * (u - c[i]) + (a[i] - c[i]) * (v - c[j])) / d
                l3 = 1 - l1 - l2
                if min(l1, l2, l3) < -1e-9:
                    continue
                h = l1 * a[axis] + l2 * b[axis] + l3 * c[axis]
                if best is None or (h > best if which == "hi" else h < best):
                    best = h
    return best


NAME = re.compile(r"^(body|mirror-[lr])-[a-z]+$|^glass$")


def digest_parts(page: str):
    """(stored digest, REF_BODY data text, REF_PLACEMENTS rows text), or None."""
    body = re.search(r"  var REF_BODY = (\[.*?\]);\n  /\* REF_DIGEST sha256:([0-9a-f]{64}) \*/\n", page)
    rows = re.search(r"    var REF_PLACEMENTS = \[\n([\s\S]*?)\n    \];", page)
    if not body or not rows:
        return None
    return body.group(2), body.group(1), rows.group(1)


def digest_ok(page: str) -> bool:
    """True only if REF_BODY and REF_PLACEMENTS are byte for byte what
    scripts/build_reference_body.py wrote, and every group has a body-group name."""
    found = digest_parts(page)
    if found is None:
        return False
    stored, data, rows = found
    if hashlib.sha256((data + "\n" + rows).encode("utf-8")).hexdigest() != stored:
        return False
    names = [g.get("name", "") for g in json.loads(data)]
    return all(NAME.match(n) for n in names) and set(names) == set(placements(page))

#!/usr/bin/env python3
"""Rebuild the reference body embedded in src/hummer_obd/dashboard.html.

The page's Body layer is the exterior of "Hummer EV - Low Poly" by Ajay Gawde
(https://sketchfab.com/3d-models/hummer-ev-low-poly-12622086af0449eda09f9d2ce5596090),
licensed CC-BY-4.0. This script is how that mesh gets from the downloaded GLB
into the page, so the numbers in the page can be regenerated rather than
trusted:

1. Frame. The GLB is Z-up with the nose on -Y. It is turned into the page's
   frame (+X forward, +Y up, +Z to the passenger side) and shifted so the
   origin is on the ground at the wheelbase midpoint: the axle stations come
   from the four tyre meshes, the ground from the tyre bottoms. Laterally the
   model is already symmetric about its own centre plane.
2. Height. In plan the model agrees with GMC's published length, wheelbase,
   rear overhang, track and both widths to 0.6 % (the front overhang to 1.1 %,
   10 mm), but it is 3.4 % short in height
   and its tyre is 2.3 % small. Heights are therefore mapped y' = a*y + b so
   that its wheel centres land on the published tyre radius (LT305/55R22,
   0.4471 m) and its roof on the published overall height (2.009 m). Plan
   dimensions are untouched.
3. Trim. The model carries tyres, rims, an interior (seats, wheel, console, a
   cabin tub) and wheel hardware (discs, calipers, arms, coil-overs). The page
   draws its own of all of those, sourced separately, so the tyre and rim
   materials are not read at all, and any connected piece of the Details mesh
   that sits inside the wheel stations or inside the cabin is dropped.
   The roof marker lamps, which also sit over the cabin, are kept.
4. Colour. Faces take the page's palette from the model's own base-colour
   texture: white panels are body colour, dark ones trim, grey ones metal, and
   the red and amber lenses keep their colour.
5. Encoding. Each colour group is stored like the page's own unit meshes:
   positions as int16 over the group's bounding box, normals as int8 divided
   by the box size, indices as uint16. The page's vertex shader multiplies a
   normal by mat3(uModel), which is that same size on the diagonal, so a normal
   stored as n/s is drawn as n. (The first version divided by the size squared
   and lit every sloped panel as if it faced a short axis of its box: the
   windshield came out about 30 degrees wrong.) buildScene() places
   each group with that box, so a part's t/s is its honest extent.
6. Digest. A sha256 of the REF_BODY line's data and the REF_PLACEMENTS rows is
   written beside them, so a hand edit to either -- which no geometry test
   could otherwise see, since both are read from the same table -- fails the
   tests until the page is rebuilt from the model.

Needs numpy, trimesh, scipy and Pillow, none of which the node itself uses:
    python3 -m venv /tmp/refbody && /tmp/refbody/bin/pip install numpy trimesh scipy pillow
    /tmp/refbody/bin/python scripts/build_reference_body.py 3d/hummer_ev_-_low_poly.glb
"""

import argparse
import base64
import io
import json
import re
import struct
import sys
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

PAGE = Path(__file__).resolve().parent.parent / "src" / "hummer_obd" / "dashboard.html"
TYRE_RADIUS = 0.4471      # LT305/55R22: (558.8 + 2 x 167.75) / 2 mm
OVERALL_HEIGHT = 2.009    # GMC, 79.1 in
KEEP_MATERIALS = ("Main_Body", "Details", "Glass")


def read_glb(path):
    blob = Path(path).read_bytes()
    json_len = struct.unpack("<I", blob[12:16])[0]
    gltf = json.loads(blob[20:20 + json_len])
    bin_at = 20 + json_len
    bin_len = struct.unpack("<I", blob[bin_at:bin_at + 4])[0]
    return gltf, blob[bin_at + 8:bin_at + 8 + bin_len]


def accessor(gltf, binary, index):
    dtypes = {5126: np.float32, 5125: np.uint32, 5123: np.uint16, 5121: np.uint8}
    widths = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
    acc = gltf["accessors"][index]
    view = gltf["bufferViews"][acc["bufferView"]]
    offset = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    n = widths[acc["type"]]
    data = np.frombuffer(binary, dtype=dtypes[acc["componentType"]],
                         count=acc["count"] * n, offset=offset)
    return data.reshape(-1, n) if n > 1 else data


def texture(gltf, binary, material):
    tex = material["pbrMetallicRoughness"]["baseColorTexture"]["index"]
    view = gltf["bufferViews"][gltf["images"][gltf["textures"][tex]["source"]]["bufferView"]]
    start = view.get("byteOffset", 0)
    image = Image.open(io.BytesIO(binary[start:start + view["byteLength"]])).convert("RGB")
    return np.asarray(image).astype(np.float32) / 255


def enc(arr):
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode()


def to_page_frame(p):
    """GLB mesh space (Z up, nose -Y, driver +X) to the page's (+X fwd, +Y up, +Z passenger)."""
    return np.stack([-p[:, 1], p[:, 2], -p[:, 0]], axis=1)


def colour_class(rgb):
    lum = rgb.mean(1)
    sat = rgb.max(1) - rgb.min(1)
    r, g, b = rgb.T
    out = np.full(len(rgb), "trim", dtype=object)
    out[lum >= 0.22] = "metal"
    out[lum >= 0.55] = "paint"
    out[(sat > 0.25) & (r > 0.4) & (g < 0.2)] = "red"
    out[(sat > 0.25) & (r > 0.5) & (g >= 0.2) & (b < 0.25)] = "amber"
    return out


def role(material, lo, hi):
    """What a connected piece of the model is, or None for pieces the page draws itself."""
    if material == "Glass":
        return "glass"
    if material == "Main_Body":
        return "body"
    zmin, zmax = sorted((abs(lo[2]), abs(hi[2])))
    if 1.5 <= abs((lo[0] + hi[0]) / 2) <= 1.95 and hi[1] <= 1.10 and zmin >= 0.40 and zmax <= 0.99:
        return None   # hubs, discs, calipers, arms, coil-overs
    if lo[0] >= -1.30 and hi[0] <= 1.30 and 0.70 <= lo[1] < 1.80 and zmax <= 0.90:
        return None   # cabin tub, seats, headrests, wheel, console, grab handles
    if 0.70 <= lo[0] and hi[0] <= 0.95 and 1.20 <= lo[1] and hi[1] <= 1.60 and zmax >= 1.05:
        return "mirror-l" if lo[2] < 0 else "mirror-r"
    return "body"


def build(glb_path):
    gltf, binary = read_glb(glb_path)
    materials = gltf["materials"]
    prims = [(materials[p["material"]]["name"], p) for m in gltf["meshes"] for p in m["primitives"]]

    # Axle stations and ground from the four tyres (material "Material.001").
    tyre = [to_page_frame(accessor(gltf, binary, p["attributes"]["POSITION"]).astype(np.float64))
            for name, p in prims if name == "Material.001"]
    tyre = np.concatenate(tyre)
    front, rear = tyre[tyre[:, 0] > tyre[:, 0].mean()], tyre[tyre[:, 0] <= tyre[:, 0].mean()]
    front_x = (front[:, 0].min() + front[:, 0].max()) / 2
    rear_x = (rear[:, 0].min() + rear[:, 0].max()) / 2
    ground = tyre[:, 1].min()
    shift = np.array([-(front_x + rear_x) / 2, -ground, 0.0])
    centre_y = (tyre[:, 1].min() + tyre[:, 1].max()) / 2 - ground

    raw = []
    for name, p in prims:
        if name not in KEEP_MATERIALS:
            continue
        pos = to_page_frame(accessor(gltf, binary, p["attributes"]["POSITION"]).astype(np.float64)) + shift
        nrm = to_page_frame(accessor(gltf, binary, p["attributes"]["NORMAL"]).astype(np.float64))
        uv = accessor(gltf, binary, p["attributes"]["TEXCOORD_0"]).astype(np.float64)
        idx = accessor(gltf, binary, p["indices"]).astype(np.int64).reshape(-1, 3)
        raw.append((name, p, pos, nrm, uv, idx))
    roof = max(pos[:, 1].max() for name, _, pos, *_ in raw if name == "Main_Body")

    a = (OVERALL_HEIGHT - TYRE_RADIUS) / (roof - centre_y)
    b = TYRE_RADIUS - a * centre_y

    groups, dropped = {}, 0
    for name, prim, pos, nrm, uv, idx in raw:
        if name == "Glass":
            cls = np.full(len(idx), "glass", dtype=object)
        else:
            img = texture(gltf, binary, materials[prim["material"]])
            h, w = img.shape[:2]
            c = uv[idx].mean(1)
            cls = colour_class(img[np.clip((c[:, 1] % 1) * h, 0, h - 1).astype(int),
                                   np.clip((c[:, 0] % 1) * w, 0, w - 1).astype(int)])
        mesh = trimesh.Trimesh(pos, idx, process=True)
        assert len(mesh.faces) == len(idx), "merging vertices must not drop faces"
        labels = trimesh.graph.connected_component_labels(mesh.face_adjacency, node_count=len(mesh.faces))
        for label in range(labels.max() + 1):
            faces = np.where(labels == label)[0]
            verts = mesh.vertices[np.unique(mesh.faces[faces])]
            r = role(name, verts.min(0), verts.max(0))
            if r is None:
                dropped += len(faces)
                continue
            for k in np.unique(cls[faces]):
                sel = faces[cls[faces] == k]
                groups.setdefault("glass" if r == "glass" else f"{r}-{k}", []).append((pos, nrm, idx[sel]))

    out = []
    for gname in sorted(groups):
        tri = np.concatenate([p[i] for p, n, i in groups[gname]]).reshape(-1, 3)
        nor = np.concatenate([n[i] for p, n, i in groups[gname]]).reshape(-1, 3)
        tri = tri * [1, a, 1] + [0, b, 0]
        nor = nor / [1, a, 1]
        lo, hi = tri.min(0), tri.max(0)
        t, s = (lo + hi) / 2, np.maximum(hi - lo, 1e-4)
        q = np.round((tri - t) / s * 65534).clip(-32767, 32767).astype(np.int16)
        nd = nor / s
        nq = np.round(nd / (np.linalg.norm(nd, axis=1, keepdims=True) + 1e-12) * 127).clip(-127, 127).astype(np.int8)
        key = np.concatenate([q.view(np.uint8).reshape(-1, 6), nq.view(np.uint8).reshape(-1, 3)], axis=1)
        uniq, inverse = np.unique(key, axis=0, return_inverse=True)
        assert len(uniq) < 65536, f"{gname}: too many vertices for uint16 indices"
        out.append({"name": gname, "t": [round(float(v), 4) for v in t], "s": [round(float(v), 4) for v in s],
                    "v": enc(uniq[:, :6].copy().view(np.int16)), "n": enc(uniq[:, 6:].copy().view(np.int8)),
                    "i": enc(inverse.reshape(-1).astype(np.uint16)), "tris": len(inverse) // 3})
    info = {"shift": shift.round(5).tolist(), "height_map": [round(a, 6), round(b, 6)],
            "dropped_tris": dropped, "kept_tris": sum(g["tris"] for g in out)}
    return out, info


def digest(data, rows):
    """What the page's REF_DIGEST must be for this REF_BODY data and these placement rows."""
    import hashlib
    return hashlib.sha256((data + "\n" + rows).encode("utf-8")).hexdigest()


def splice(page, groups):
    data = json.dumps([{k: g[k] for k in ("name", "v", "n", "i")} for g in groups], separators=(",", ":"))
    rows = ",\n".join(f'      ["{g["name"]}", {json.dumps(g["t"])}, {json.dumps(g["s"])}]' for g in groups)
    body = f"  var REF_BODY = {data};\n  /* REF_DIGEST sha256:{digest(data, rows)} */\n"
    page, n = re.subn(r"  var REF_BODY = \[.*?\];\n(  /\* REF_DIGEST sha256:[0-9a-f]{64} \*/\n)?",
                      lambda m: body, page, flags=re.S)
    assert n == 1, "REF_BODY line not found exactly once"
    page, n = re.subn(r"(    /\* REF_PLACEMENTS:begin \*/\n).*?(\n    /\* REF_PLACEMENTS:end \*/)",
                      lambda m: m.group(1) + "    var REF_PLACEMENTS = [\n" + rows + "\n    ];" + m.group(2),
                      page, flags=re.S)
    assert n == 1, "REF_PLACEMENTS markers not found exactly once"
    return page


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("glb", help="the downloaded hummer_ev_-_low_poly.glb")
    ap.add_argument("--page", default=str(PAGE))
    ap.add_argument("--check", action="store_true", help="exit 1 if the page is not what this would write")
    args = ap.parse_args(argv)
    groups, info = build(args.glb)
    page = Path(args.page).read_text(encoding="utf-8")
    new = splice(page, groups)
    print(json.dumps(info), file=sys.stderr)
    for g in groups:
        print(f'  {g["name"]:15s} {g["tris"]:5d} tris  t {g["t"]}  s {g["s"]}', file=sys.stderr)
    if args.check:
        return 0 if new == page else 1
    Path(args.page).write_text(new, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())

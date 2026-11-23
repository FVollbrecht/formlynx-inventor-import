# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Bezugsgeometrie einer .ipt ohne Inventor und ohne bpy: Skizzen (2D/3D), Arbeitsebenen, Ursprungsebenen,
-achsen und Mittelpunkt, je mit der Sichtbarkeit aus Inventor.

Skizzen stehen analytisch im PmGraphicsSegment vor den Netzen: Satz = Kopf (Klasse u16, 6 Byte 0, ID u16, …) und
Verweis `<gruppe> 00 00 80` auf die Skizze, danach die doubles (cm). Klassennummern vergibt jede Datei selbst ->
der Typ folgt aus dem Inhalt (Längenfeld = Nutzdaten + 26 Byte). 2D-Skizzen in Skizzenkoordinaten, ihre Lage als
komprimierte 4×4-Matrix im PmDCSegment; Ebenen als Grafikknoten (Umriss + dieselbe Matrix). Namen und Sichtbarkeit
aus dem PmBrowserSegment.
"""
import math
import re
import struct

import numpy as np

CM_TO_MM = 10.0
ARC_STEP = math.radians(5.0)          # Bögen: höchstens 5° je Strecke
SPLINE_SAMPLES = 16                   # Punkte je Spline-Spanne
UNIT_TOL = 1e-6

# Verweis auf die Skizze + 4 Byte 0; davor 24 Byte Kopf (00 00 | Klasse u16 | 6 Byte 0 | …)
_REF = re.compile(rb"[\x01-\xff][\x00-\xff]\x00\x80\x00\x00\x00\x00", re.S)
# DC-Objekt mit Matrix: Elternverweis 3, eigene ID, ffffffff, 200c0000, Matrixcode
_DC_FRAME = re.compile(rb"\x03\x00\x00\x80(....)\xff\xff\xff\xff\x20\x0c\x00\x00", re.S)


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0] if o + 4 <= len(b) else None


def _doubles(b, p, n):
    if p + n * 8 > len(b):
        return None
    d = np.frombuffer(b, "<f8", n, p).astype(np.float64)
    if not np.all(np.isfinite(d)) or np.any(np.abs(d) > 1e6):
        return None
    d[np.abs(d) < 1e-12] = 0.0
    return d


def _unit(v):
    return abs(float(np.linalg.norm(v)) - 1.0) < UNIT_TOL


# ---------- Matrixcode (PmDCSegment) ----------

def decode_matrix(b, o):
    """Komprimierte 4×4-Matrix ab o: u32 Code, je Eintrag i (zeilenweise) Bit i und Bit 16+i:
    (0,1) = 0, (1,0) = +1, (1,1) = −1, (0,0) = double folgt (in Eintragsreihenfolge). -> (M, Ende) oder None"""
    code = _u32(b, o)
    if code is None:
        return None
    m = np.zeros(16)
    p = o + 4
    for i in range(16):
        lo, hi = (code >> i) & 1, (code >> (16 + i)) & 1
        if lo and hi:
            m[i] = -1.0
        elif lo:
            m[i] = 1.0
        elif not hi:
            d = _doubles(b, p, 1)
            if d is None:
                return None
            m[i] = d[0]
            p += 8
    m = m.reshape(4, 4)
    r = m[:3, :3]
    if not np.allclose(m[3], (0, 0, 0, 1)) or not np.allclose(r @ r.T, np.eye(3), atol=1e-6):
        return None
    return m, p


def sketch_frames(dc):
    """Lage der 2D-Skizzen (Skizze -> Modell, cm) in Dateireihenfolge"""
    out = []
    for mm in _DC_FRAME.finditer(dc):
        got = decode_matrix(dc, mm.end())
        if got is not None:
            out.append(got[0])
    return out


def visible_planes(gb, arrays, end=None):
    """Angezeigte Ebenen: Knoten mit Umriss (u0, v0, 0, u1, v1, 0) in Ebenenkoordinaten, 0–8 Byte danach der
    Matrixcode (Ebene -> Modell). Gültig nur, wenn ein Viereck-Netz (4 Punkte, z = 0) genau diesen Umriss hat.
    -> [dict(m 4×4 cm, box (u0, v0, u1, v1) cm)] in Dateireihenfolge"""
    quads, first_quad = [], len(gb)
    for o, t, n in arrays:
        if t > 250 and n == 4 and o + 16 + 48 <= len(gb) and all(
                gb[o + 24 + 12 * k:o + 28 + 12 * k] == b"\x00\x00\x00\x00" for k in range(4)):
            q = np.frombuffer(gb, "<f4", 12, o + 16).reshape(4, 3)
            if np.all(np.isfinite(q)) and np.all(q[:, 2] == 0):
                quads.append((q[:, 0].min(), q[:, 1].min(), q[:, 0].max(), q[:, 1].max()))
                first_quad = min(first_quad, o)
    if not quads:
        return []
    quads = np.array(quads, dtype=np.float64)
    out, seen = [], set()
    if end is None:
        end = first_mesh(gb, arrays)
    end = min(end, first_quad)                         # Ebenenknoten stehen vor ihren Vierecken und vor den Netzen
    # Umriss enthält zweimal z = 0 (8 Byte 0 an +16 und +40)
    for mm in re.finditer(rb"(?=[\s\S]{16}\x00{8}[\s\S]{16}\x00{8})", gb[:end]):
        o = mm.start()
        d = _doubles(gb, o, 6)
        if d is None or not (d[3] > d[0] and d[4] > d[1]):
            continue
        box = np.array([d[0], d[1], d[3], d[4]])
        if not np.any(np.all(np.abs(quads - box) < 1e-3, axis=1)):
            continue
        for k in range(9):
            got = decode_matrix(gb, o + 48 + k)
            if got:
                key = tuple(np.round(got[0][:3].ravel(), 6))
                if key not in seen:
                    seen.add(key)
                    out.append(dict(m=got[0], box=box))
                break
    return out


# ---------- Skizzensätze (PmGraphicsSegment) ----------

def _sized(b, p, n):
    """n doubles, wenn danach das Längenfeld (8n + 26, bei 3D-Bögen ein Byte später 8n + 27) steht"""
    q = p + 8 * n
    if _u32(b, q) == 8 * n + 26 or _u32(b, q + 1) == 8 * n + 27:
        return _doubles(b, p, n)
    return None


def _bspline(b, p):
    """u32 1, 0, Grad, double Toleranz, Knoten (Anzahl, Kapazität, 8, doubles), Gewichte (dito, meist leer),
    Kontrollpunkte (Anzahl, Kapazität, 8, xyz doubles)"""
    if _u32(b, p) != 1 or _u32(b, p + 4) != 0:
        return None
    deg = _u32(b, p + 8)
    if not deg or deg > 9:
        return None
    q = p + 20
    arrays = []
    for size in (1, 1, 3):
        n, cap, es = _u32(b, q), _u32(b, q + 4), _u32(b, q + 8)
        if n is None or es != 8 or n > cap or n > 100000:
            return None
        d = _doubles(b, q + 12, n * size)
        if d is None:
            return None
        arrays.append(d)
        q += 12 + n * size * 8
    knots, weights, ctrl = arrays
    ctrl = ctrl.reshape(-1, 3)
    if len(knots) != len(ctrl) + deg + 1 or (len(weights) and len(weights) != len(ctrl)):
        return None
    return dict(kind="spline", degree=int(deg), knots=knots, weights=weights if len(weights) else None, ctrl=ctrl)


def _entity(b, p):
    if _u32(b, p + 34) == 60:                            # Punkt: xyz, Punktgröße f32, 6 Byte, Längenfeld 60
        d = _doubles(b, p, 3)
        return dict(kind="point", p=d) if d is not None else None
    sp = _bspline(b, p)
    if sp:
        return sp
    for n in (12, 7, 6):
        d = _sized(b, p, n)
        if d is None:
            continue
        if n == 12 and _unit(d[3:6]) and _unit(d[6:9]) and abs(float(d[3:6] @ d[6:9])) < 1e-4 and d[9] > 0:
            return dict(kind="arc3", c=d[:3], n=d[3:6], x=d[6:9], r=d[9], a=(d[10], d[11]))
        if n == 7 and _unit(d[2:4]) and d[4] > 0 and d[6] > d[5]:
            return dict(kind="arc2", c=np.array([d[0], d[1], 0.0]), x=np.array([d[2], d[3], 0.0]), r=d[4],
                        a=(d[5], d[6]))
        if n == 6:
            return dict(kind="line", p0=d[:3], p1=d[3:6])
    return None


def first_mesh(gb, arrays):
    """Beginn des ersten Flächen-/Ebenennetzes (Positionen, Indizes, Normalen) – Skizzen stehen davor"""
    for i, (o, t, n) in enumerate(arrays):
        if t > 250 and n >= 3 and i + 1 < len(arrays) and arrays[i + 1][1] < 16:
            return o
    return len(gb)


def sketch_groups(gb, end):
    """{gruppe: [Element]} in Dateireihenfolge der Gruppen, nur Sätze vor `end`"""
    groups = {}
    for mm in _REF.finditer(gb, 24, end):
        o = mm.start()
        if gb[o - 24:o - 22] != b"\x00\x00" or gb[o - 20:o - 14] != b"\x00" * 6:
            continue
        e = _entity(gb, o + 8)
        if e:
            groups.setdefault(_u32(gb, o) & 0x7FFFFFFF, []).append(e)
    return groups


def is_3d(ents):
    for e in ents:
        if e["kind"] in ("arc3", "spline"):
            return True
        pts = [e[k] for k in ("p", "p0", "p1", "c") if k in e]
        if any(abs(q[2]) > 1e-9 for q in pts):
            return True
    return False


# ---------- Abtasten ----------

def _arc_points(c, x, y, r, a0, a1):
    n = max(2, int(math.ceil(abs(a1 - a0) / ARC_STEP)) + 1)
    t = np.linspace(a0, a1, n)
    return c + r * (np.cos(t)[:, None] * x + np.sin(t)[:, None] * y)


def _deboor(deg, knots, ctrl, weights, u):
    k = int(np.searchsorted(knots, u, side="right")) - 1
    k = min(max(k, deg), len(ctrl) - 1)
    w = np.ones(len(ctrl)) if weights is None else weights
    d = [np.append(ctrl[j] * w[j], w[j]) for j in range(k - deg, k + 1)]
    for r in range(1, deg + 1):
        for j in range(deg, r - 1, -1):
            i = j + k - deg
            den = knots[i + deg - r + 1] - knots[i]
            a = 0.0 if den == 0 else (u - knots[i]) / den
            d[j] = (1 - a) * d[j - 1] + a * d[j]
    return d[deg][:3] / d[deg][3]


def polyline(e):
    """Element -> Punkte (N, 3) in cm, Koordinaten der Skizze"""
    k = e["kind"]
    if k == "line":
        return np.array([e["p0"], e["p1"]])
    if k == "arc2":
        x = e["x"]
        return _arc_points(e["c"], x, np.array([-x[1], x[0], 0.0]), e["r"], *e["a"])
    if k == "arc3":
        return _arc_points(e["c"], e["x"], np.cross(e["n"], e["x"]), e["r"], *e["a"])
    if k == "spline":
        deg, kn = e["degree"], e["knots"]
        spans = len(np.unique(kn[deg:len(kn) - deg]))
        u = np.linspace(kn[deg], kn[len(kn) - deg - 1], max(2, (spans - 1) * SPLINE_SAMPLES + 1))
        return np.array([_deboor(deg, kn, e["ctrl"], e["weights"], x) for x in u])
    return None


# ---------- Namen und Sichtbarkeit (PmBrowserSegment) ----------

HIDDEN = 0x04                         # Byte direkt nach dem Namen: Bit 2 = in Inventor ausgeblendet


def browser_entries(br):
    """[(Name, Typcode, ausgeblendet)] in Baumreihenfolge: UTF-16-Text, Zeichenanzahl u32 davor, Typcode u32 davor,
    Merkerbyte danach. Ursprungselemente heißen intern englisch („YZ Plane“, „X Axis“, „Center Point“)."""
    out = []
    for mm in re.finditer(rb"(?:[\x20-\x7e\xa0-\xff]\x00){2,}", br):
        o = mm.start()
        n = _u32(br, o - 4)
        if n is None or n * 2 > len(mm.group()) or n < 2:
            continue
        end = o + n * 2
        hidden = end < len(br) and bool(br[end] & HIDDEN)
        out.append((br[o:end].decode("utf-16-le", "replace"), _u32(br, o - 8), hidden))
    return out


def _assign_names(entries, kinds):
    """kinds: Folge '2'/'3' der Skizzengruppen. Sucht Typcodes c2, c3, deren Einträge in Reihenfolge genau diese
    Folge ergeben -> (c2, c3, [Eintrag]) oder None"""
    codes = {}
    for e in entries:
        codes.setdefault(e[1], []).append(e)
    want2, want3 = kinds.count("2"), kinds.count("3")
    c2s = [c for c, v in codes.items() if len(v) == want2] if want2 else [None]
    c3s = [c for c, v in codes.items() if len(v) == want3] if want3 else [None]
    hits = []
    for c2 in c2s:
        for c3 in c3s:
            if c2 == c3:
                continue
            seq = [e for e in entries if e[1] in (c2, c3)]
            if "".join("2" if e[1] == c2 else "3" for e in seq) == kinds:
                hits.append((c2, c3, seq))
    return hits[0] if len(hits) == 1 else None


# ---------- Gesamt ----------

ORIGIN_NAMES = ("Origin", "YZ Plane", "XZ Plane", "XY Plane", "X Axis", "Y Axis", "Z Axis", "Center Point")


def read(seg, arrays=None):
    """-> dict(sketches=[dict(name, is3d, hidden, curves=[(N,3) mm], points=[(3,) mm])],
    planes=[dict(name, hidden, corners (4,3) mm, origin_plane)], axes=[dict(name, hidden, p0, p1 mm)],
    center=dict(name, hidden, p) | None). Alle Elemente; `hidden` = in Inventor ausgeblendet."""
    from . import inventor_part                         # erst hier: inventor_part importiert dieses Modul nicht
    gb = seg.get("PmGraphicsSegment", b"")
    dc = seg.get("PmDCSegment", b"")
    br = seg.get("PmBrowserSegment", b"")
    out = dict(sketches=[], planes=[], axes=[], center=None)
    if not gb:
        return out
    if arrays is None:
        arrays = inventor_part._arrays(gb)
    start = first_mesh(gb, arrays)
    groups = sketch_groups(gb, start)
    frames = sketch_frames(dc)
    kinds = "".join("3" if is_3d(v) else "2" for v in groups.values())
    entries = browser_entries(br)
    named = _assign_names(entries, kinds)
    fi = n2 = n3 = 0
    for gi, ents in enumerate(groups.values()):
        three = kinds[gi] == "3"
        m = np.eye(4)
        if three:
            n3 += 1
        else:
            if fi < len(frames):
                m = frames[fi]
            fi += 1
            n2 += 1
        if named:
            name, hidden = named[2][gi][0], named[2][gi][2]
        else:
            name, hidden = (f"3D Sketch {n3}" if three else f"Sketch {n2}"), True

        def tf(p, m=m):
            return (np.atleast_2d(p) @ m[:3, :3].T + m[:3, 3]) * CM_TO_MM

        curves = [tf(pl) for pl in (polyline(e) for e in ents) if pl is not None]
        points = [tf(e["p"])[0] for e in ents if e["kind"] == "point"]
        if curves or points:
            out["sketches"].append(dict(name=name, is3d=three, hidden=hidden, curves=curves, points=points))

    # Ursprungselemente und Arbeitselemente teilen sich einen Typcode; Ursprung über die internen Namen
    code = next((e[1] for e in entries if e[0] == "YZ Plane"), None)
    origin = {e[0]: e[2] for e in entries if e[1] == code and e[0] in ORIGIN_NAMES}
    user = [e for e in entries if e[1] == code and e[0] not in ORIGIN_NAMES] if code is not None else []
    planes = visible_planes(gb, arrays, start)
    origin_left = {0: "YZ Plane", 1: "XZ Plane", 2: "XY Plane"}
    user_planes = []
    for p in planes:
        m, (u0, v0, u1, v1) = p["m"], p["box"]
        normal = m[:3, 2]
        ax = int(np.argmax(np.abs(normal)))
        corners = np.array([m[:3, 0] * a + m[:3, 1] * b + m[:3, 3] for a, b in ((u0, v0), (u1, v0), (u1, v1), (u0, v1))])
        plane = dict(name=None, hidden=False, corners=corners * CM_TO_MM, origin_plane=False)
        if not np.any(m[:3, 3]) and np.isclose(abs(normal[ax]), 1.0) and ax in origin_left:
            name = origin_left.pop(ax)
            plane.update(name=name, hidden=origin.get(name, True), origin_plane=True)
        else:
            user_planes.append(plane)
        out["planes"].append(plane)
    # Namen der Arbeitsebenen nur, wenn es genau so viele Arbeitselemente wie Ebenen gibt (Achsen/Punkte sonst unklar)
    for k, plane in enumerate(user_planes):
        if len(user) == len(user_planes):
            plane.update(name=user[k][0], hidden=user[k][2])
        else:
            plane.update(name=f"Work Plane {k + 1}", hidden=True)

    # Achsen: Lage der Achsgrafik nicht entschlüsselt -> Länge aus Ebenen und Skizzen mit 10 % Überstand
    if planes or out["sketches"]:
        pts = [p["corners"] for p in out["planes"]] + [c for s in out["sketches"] for c in s["curves"]]
        pts = np.concatenate(pts + [np.zeros((1, 3))])
        lo, hi = pts.min(0), pts.max(0)
        pad = 0.1 * float((hi - lo).max())
        for i in range(3):
            p0, p1 = np.zeros(3), np.zeros(3)
            p0[i], p1[i] = min(lo[i], 0.0) - pad, max(hi[i], 0.0) + pad
            name = "XYZ"[i] + " Axis"
            out["axes"].append(dict(name=name, hidden=origin.get(name, True), p0=p0, p1=p1))
        out["center"] = dict(name="Center Point", hidden=origin.get("Center Point", True), p=np.zeros(3))
    return out

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Inventor-Bauteil (.ipt) -> Körper mit Netz und Farben, ohne Inventor und ohne bpy.

Netz aus PmGraphicsSegment (Anzeigenetz je Fläche), Körper und Flächenfarben aus PmBRepSegment (ASM/ACIS SAB),
Farbwerte aus PmAppSegment (Darstellungen). Zuordnung B-Rep-Fläche <-> Grafikfläche über die Eckpunkte
(liegen exakt im Netz), Feature-ID und Flächenart. Format und Regeln: atlas/ATLAS.md („.ipt-Netz“).
Prototyp mit Diagnose-Ausgabe: tools/ipt_probe.py.
"""
import bisect
import heapq
import math
import re
import struct
from collections import defaultdict

import numpy as np

from . import inventor_props
from .inventor_data import Cfb, InventorError, PartFileError, unpack_segment

HEAD = b"\x02\x00\x00\x30"
# Kopf eines Flächen-Eintrags: Feld (k, k, 0x10000000) mit k Paaren (Feature-ID u32, ffffffff), meist k = 1.
# Arbeitsebenen/Skizzen haben andere Köpfe und bleiben so außen vor.
FACE_MARK = re.compile(rb"\x02\x00\x00\x30([\x01-\x40])\x00\x00\x00\1\x00\x00\x00\x00\x00\x00\x10....\xff\xff\xff\xff", re.S)
POINT_TOL = 1e-3             # cm: B-Rep-Eckpunkt liegt im Grafiknetz
SURF_TOL = 2e-3              # cm: Netzpunkte liegen auf der B-Rep-Fläche
CM_TO_MM = 10.0
NORMAL_TOL = 1e-3            # Normalen-Array: alle Vektoren Länge 1


# ---------- Datei ----------

def segments(data):
    """{Segmentname: entpackte Bytes} aus RSeStorage (Meta-Stream M…: Name UTF-16, Daten-Stream B…: 18 Byte Kopf + zlib/Zstandard)."""
    cfb = Cfb(data)
    out = {}
    for p in cfb.paths():
        if p.startswith("RSeStorage/M"):
            words = re.findall(rb"(?:[\x20-\x7e]\x00){4,}", cfb.get(p) or b"")
            raw = cfb.get("RSeStorage/B" + p[12:])
            if not words or raw is None:
                continue
            raw = unpack_segment(raw)
            if raw is None:
                continue
            out.setdefault(words[0].decode("utf-16le"), raw)
    return out


# ---------- Grafiksegment ----------

def _arrays(b):
    out = []
    for mm in re.finditer(re.escape(HEAD), b):
        o = mm.start()
        if o + 16 > len(b):
            continue
        a, c, t = struct.unpack_from("<3I", b, o + 4)
        if 0 < a <= c < 10_000_000 and (t < 16 or 250 < t < 400):
            out.append((o, t, a))
    return out


def graphic_faces(b):
    """Flächen des Anzeigenetzes: dict(mark, feature, pts[N,3] cm, idx[M,3], nor[N,3]).
    Satz = Positionen (n Punkte), Indizes u32 (kleiner Typ), Normalen (n Vektoren Länge 1), Zusatz-Array (n).
    Die Typnummern vergibt jede Datei selbst (2017–2025 gesehen: Normalen t+1 oder t+6) -> nur der Aufbau zählt.
    Je Flächen-Eintrag erst die feine, dann die grobe Stufe -> nur der erste Satz zählt."""
    marks = [mm.start() for mm in FACE_MARK.finditer(b)]
    arr = _arrays(b)
    out, seen = [], set()
    for i, (o, t, n) in enumerate(arr):
        if not (t > 250 and i + 2 < len(arr) and arr[i + 1][1] < 16 and arr[i + 2][1] > 250 and arr[i + 2][2] == n):
            continue
        k = bisect.bisect_left(marks, o) - 1
        if k < 0 or marks[k] in seen:
            continue
        io, _, ni = arr[i + 1]
        no = arr[i + 2][0]
        if ni % 3 or io + 16 + ni * 4 > len(b) or o + 16 + n * 12 > len(b) or no + 16 + n * 12 > len(b):
            continue
        nor = np.frombuffer(b, "<f4", n * 3, no + 16).reshape(-1, 3).astype(np.float64)
        if not np.all(np.abs(np.linalg.norm(nor, axis=1) - 1.0) < NORMAL_TOL):
            continue
        idx = np.frombuffer(b, "<u4", ni, io + 16)
        if ni and idx.max() >= n:
            continue
        seen.add(marks[k])
        out.append(dict(mark=marks[k], feature=struct.unpack_from("<I", b, marks[k] + 16)[0],
                        pts=np.frombuffer(b, "<f4", n * 3, o + 16).reshape(-1, 3).astype(np.float64),
                        idx=idx.reshape(-1, 3).astype(np.int64),
                        nor=nor))
    return out


def _rgba_pair_at(b, o):
    """16 Null-Byte, dann RGBA ambient und RGBA diffus (gleich) -> diffus oder None."""
    if b[o:o + 16] != bytes(16) or o + 48 > len(b):
        return None
    amb = struct.unpack_from("<4f", b, o + 16)
    dif = struct.unpack_from("<4f", b, o + 32)
    if dif[3] == 1.0 and all(0.0 <= x <= 1.0 for x in dif[:3]) and max(dif[:3]) > 0.01 and amb == dif:
        return dif
    return None


def material_states(b):
    """Materialsätze im Grafiksegment als [(offset, rgba)], aufsteigend. Kopf variiert, darum über die Form gesucht:
    16 Null-Byte + zweimal dieselbe RGBA. Ein Satz gilt für die folgenden Flächen (Teilfarbe vor der ersten Fläche,
    Feature-Darstellung vor den Flächen des Features) – an Testdateien gesehen, nicht bewiesen."""
    out = []
    for mm in re.finditer(rb"\x00{16,}", b):
        rgba = _rgba_pair_at(b, mm.end() - 16)
        if rgba:
            out.append((mm.end() - 16, rgba))
    return out


def _material_before(states, offsets, mark):
    k = bisect.bisect_left(offsets, mark) - 1
    return states[k][1] if k >= 0 else None


# ---------- B-Rep (ASM/ACIS SAB) ----------

def sab_entities(b):
    """Entities -> (kinds, ptrs, toks), Index = Zeigerwert in SAB (Kopf ist Entity 0). Historie wird nicht gelesen.
    kinds[i] = (Art, Unterart): letzter und erster Bezeichner vor dem ersten Wert ('face', 'attrib' … / 'plane' …).
    ptrs[i] = Zeiger in Reihenfolge, toks[i] = übrige Werte als (Art, Wert) – Zeiger und Kopf getrennt, das spart
    bei großen Teilen (Millionen Werte) die meiste Zeit."""
    i = b.find(b"ASM BinaryFile")
    if i < 0:
        return [], [], []
    o = i + len(b"ASM BinaryFile4") + 16
    kinds, ptrs, toks, n = [], [], [], len(b)
    head, ptr, cur = [], [], []
    padd, add = ptr.append, cur.append
    i32, f64, v3, v2 = (struct.Struct(f).unpack_from for f in ("<i", "<d", "<3d", "<2d"))
    sizes = {0x07: 1, 0x08: 2, 0x09: 4, 0x12: 4}
    try:
        while o < n:
            t = b[o]
            o += 1
            if t == 0x0C:
                padd(i32(b, o)[0]); o += 4
            elif t == 0x04:
                add(("i", i32(b, o)[0])); o += 4
            elif t == 0x06:
                add(("d", f64(b, o)[0])); o += 8
            elif t in (0x0D, 0x0E):
                ln = b[o]; o += 1
                word = b[o:o + ln].decode("latin-1"); o += ln
                if ptr or cur:                              # Bezeichner nach dem ersten Wert: kein Kopf
                    add(("id", word))
                else:
                    head.append(word)
            elif t == 0x11:
                if head and head[0].startswith(("End", "Begin")):
                    name = "-".join(head + [v for k, v in cur if k == "id"])   # Bezeichner kommen als Einzelteile
                    if name.startswith(("End-of-ASM-data", "Begin-of-ASM-History")):
                        break
                kinds.append((head[-1], head[0]) if head else ("", ""))
                ptrs.append(ptr)
                toks.append(cur)
                head, ptr, cur = [], [], []
                padd, add = ptr.append, cur.append
            elif t in (0x13, 0x14):
                add(("v", v3(b, o))); o += 24
            elif t in (0x0A, 0x0B):
                add(("b", t == 0x0B))
            elif t in sizes:
                size = sizes[t]
                ln = int.from_bytes(b[o:o + size], "little"); o += size
                add(("s", b[o:o + ln].decode("latin-1"))); o += ln
            elif t in (0x0F, 0x10):
                pass
            elif t == 0x15:
                add(("e", i32(b, o)[0])); o += 4
            elif t == 0x16:
                add(("v2", v2(b, o))); o += 16
            else:
                break                       # unbekanntes Tag: Rest nicht lesbar
    except struct.error:
        pass                                # abgeschnittene Daten: bis hierher verwenden
    return kinds, ptrs, toks


def brep_faces(b):
    """-> Liste dict(body, points[N,3] cm, colors[(stufe, name)], feature, surface=(art, werte))."""
    kind, ptrs, ents = sab_entities(b)
    if not kind:
        return []
    count = len(kind)
    kname = [k for k, _ in kind]

    def ptr_of(i, wanted):
        return [p for p in ptrs[i] if 0 <= p < count and kname[p] == wanted]

    def body_of(face):
        shell = (ptr_of(face, "shell") or [None])[0]
        lump = (ptr_of(shell, "lump") or [None])[-1] if shell is not None else None
        return (ptr_of(lump, "body") or [None])[0] if lump is not None else None

    loops_of, coedges_of = defaultdict(list), defaultdict(list)
    for i, k in enumerate(kname):
        if k == "loop":
            f = ptr_of(i, "face")
            if f:
                loops_of[f[-1]].append(i)
        elif k == "coedge":
            lp = ptr_of(i, "loop")
            if lp:
                coedges_of[lp[0]].append(i)

    edge_pts = {}

    def points_of_edge(e):
        """[(Punkt-Entity, Koordinaten)] der Eckpunkte einer Kante (jede Kante gehört zu zwei Flächen)"""
        hit = edge_pts.get(e)
        if hit is None:
            hit = []
            for v in ptr_of(e, "vertex"):
                for p in ptr_of(v, "point"):
                    vec = next((val for kk, val in ents[p] if kk == "v"), None)
                    if vec:
                        hit.append((p, vec))
            edge_pts[e] = hit
        return hit

    out = []
    for f, k in enumerate(kname):
        if k != "face":
            continue
        pts = {}
        for lp in loops_of[f]:
            for ce in coedges_of[lp]:
                for e in ptr_of(ce, "edge"):
                    pts.update(points_of_edge(e))
        # Attribut-Kette: ATTRIB_CUSTOM mit Flächenfarbe (Stufe 1 Körper/Feature, 2 Fläche) und Feature-ID
        colors, feature = [], None
        a = ptrs[f][0] if ptrs[f] else -1
        guard = 0
        while 0 <= a < count and kname[a] == "attrib" and guard < 100:
            strs = [v for kk, v in ents[a] if kk == "s"]
            ints = [v for kk, v in ents[a] if kk == "i"]
            if "INV_NMX_FACE_COLOR_ATTRIB" in strs and len(strs) > 1 and len(ints) > 3:
                colors.append((ints[3], strs[1]))
            elif "INV_NMX_SWEEPGENERATED_TAG" in strs and len(ints) > 4:
                feature = ints[4]
            a = ptrs[a][1] if len(ptrs[a]) > 1 else -1
            guard += 1
        surf = None
        sp = ptr_of(f, "surface")
        if sp:
            surf = (kind[sp[0]][1], [(kk, v) for kk, v in ents[sp[0]] if kk in ("v", "d")])
        out.append(dict(body=body_of(f), points=np.array(list(pts.values()), dtype=np.float64).reshape(-1, 3),
                        colors=colors, feature=feature, surface=surf))
    return out


def surface_distance(surf, pts):
    """Größter Abstand der Punkte zur Fläche (cm) oder None, wenn die Art nicht geprüft wird."""
    if not surf:
        return None
    art, vals = surf
    vs = [v for k, v in vals if k == "v"]
    ds = [v for k, v in vals if k == "d"]
    try:
        if art == "plane":
            root, n = np.array(vs[0]), np.array(vs[1])
            return float(np.abs((pts - root) @ n).max())
        if art == "sphere":
            c, r = np.array(vs[0]), abs(ds[0])
            return float(np.abs(np.linalg.norm(pts - c, axis=1) - r).max())
        if art == "torus":
            c, ax, big, r = np.array(vs[0]), np.array(vs[1]), ds[0], abs(ds[1])
            q = pts - c
            h = q @ ax
            rho = np.linalg.norm(q - np.outer(h, ax), axis=1)
            return float(np.abs(np.hypot(rho - big, h) - r).max())
        if art == "cone" and len(ds) >= 3 and abs(ds[1]) < 1e-12:      # Zylinder (sin = 0)
            root, ax, major = np.array(vs[0]), np.array(vs[1]), np.array(vs[2])
            q = pts - root
            rho = np.linalg.norm(q - np.outer(q @ ax, ax), axis=1)
            return float(np.abs(rho - np.linalg.norm(major)).max())
    except (IndexError, ValueError):
        return None
    return None


# ---------- Darstellungen ----------

_RGBA = struct.Struct("<4f").unpack_from


def appearance_rgba(app, name, memo=None):
    """Darstellungsname (Sprache der Inventor-Installation, wie im B-Rep) -> RGBA aus PmAppSegment oder None.
    Nach dem Namen folgt das Asset ('1:<Name>') und darin die erste RGBA mit a = 1."""
    if not app or not name:
        return None
    if memo is not None:
        if name not in memo:
            memo[name] = appearance_rgba(app, name)
        return memo[name]
    key = name.encode("utf-16le")
    marker = "1:".encode("utf-16le")
    start = 0
    while True:
        k = app.find(key, start)
        if k < 0:
            return None
        asset = app.find(marker, k, k + 800)
        if asset >= 0:
            for o in range(asset, min(asset + 900, len(app) - 16)):
                r, g, bl, a = _RGBA(app, o)
                if a == 1.0 and all(0.0 <= x <= 1.0 for x in (r, g, bl)) and max(r, g, bl) > 0.01:
                    return (r, g, bl, a)
        start = k + 2


# ---------- Zuordnung ----------

def match_faces(gfaces, bfaces):
    """{Index Grafikfläche: Index B-Rep-Fläche}. Kandidaten: alle Eckpunkte im Netz, Feature-ID gleich, Netz auf der Fläche.
    Eindeutige zuerst: immer die B-Rep-Fläche mit den wenigsten freien Kandidaten zuordnen."""
    grid = defaultdict(set)
    for gi, g in enumerate(gfaces):
        for p in np.floor(g["pts"] / POINT_TOL).astype(np.int64):
            grid[tuple(p)].add(gi)

    empty = frozenset()

    def faces_at(p):
        cx, cy, cz = (math.floor(v / POINT_TOL) for v in p)
        hit = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    hit |= grid.get((cx + dx, cy + dy, cz + dz), empty)
        return hit

    pairs = []
    for bi, f in enumerate(bfaces):
        cand = None
        for p in f["points"]:
            hit = faces_at(p)
            cand = hit if cand is None else cand & hit
            if not cand:
                break
        if cand is None:                      # Fläche ohne Eckpunkte (Kugel, Torus): alle prüfen
            cand = range(len(gfaces))
        for gi in cand:
            if f["feature"] is not None and gfaces[gi]["feature"] != f["feature"]:
                continue
            d = surface_distance(f["surface"], gfaces[gi]["pts"])
            if d is not None and d > SURF_TOL:
                continue
            pairs.append((0 if d is not None else 1, bi, gi))
    cands = defaultdict(list)
    for _, bi, gi in sorted(pairs):
        cands[bi].append(gi)
    # Halde nach (freie Kandidaten, Index); Zähler sinken nur, veraltete Einträge werden übersprungen
    free_count = {bi: len(gs) for bi, gs in cands.items()}
    owners = defaultdict(list)
    for bi, gs in cands.items():
        for g in gs:
            owners[g].append(bi)
    heap = [(n, bi) for bi, n in free_count.items()]
    heapq.heapify(heap)
    used, match = set(), {}
    while heap:
        n, bi = heapq.heappop(heap)
        if bi not in cands or n != free_count[bi]:
            continue
        free = [g for g in cands.pop(bi) if g not in used]
        if free:
            g = free[0]
            used.add(g)
            match[g] = bi
            for other in owners[g]:
                if other in cands:
                    free_count[other] -= 1
                    heapq.heappush(heap, (free_count[other], other))
    return match


# ---------- Ergebnis ----------

def color_key(rgb):
    """Farbe -> Schlüssel für den Namensvergleich (Grafik-Cache und Teil speichern float32)"""
    return tuple(round(float(c), 3) for c in rgb[:3])


def _part_appearance(app, props):
    """Teildarstellung (iProperty Appearance) -> (Name, rgba) oder (Name, None)"""
    name = props.get("Appearance")
    return name, appearance_rgba(app, name) if name else None


def color_names(path):
    """Darstellungsnamen eines Teils nach Farbe -> {color_key: Name} (Teil-, Körper- und Flächendarstellungen).
    Für Baugruppen: der Grafik-Cache kennt nur Farben. Schlüssel None: (Name, rgb) der Teildarstellung – Ersatz,
    wenn der Cache der Baugruppe für das Teil keine Farbe hat (iam/rottrans: Messing, Stahl)."""
    with open(path, "rb") as f:
        data = f.read()
    seg = segments(data)
    app = seg.get("PmAppSegment")
    out = {}
    names = {name for bf in brep_faces(seg.get("PmBRepSegment", b"")) for _, name in bf["colors"]}
    for name in sorted(names):
        rgba = appearance_rgba(app, name)
        if rgba:
            out.setdefault(color_key(rgba), name)
    name, rgba = _part_appearance(app, inventor_props.read_cfb(Cfb(data)))
    if rgba:
        out.setdefault(color_key(rgba), name)
        out[None] = (name, tuple(float(c) for c in rgba[:3]))
    return out


def read(path):
    """Liest eine .ipt -> dict(bodies=[dict(verts[N,3] mm, tris[M,3], normals[N,3], colors=[rgba], names=[str|None],
    color_idx[M])], faces, matched, props). Wirft PartFileError (kein Anzeigenetz) bzw. InventorError."""
    with open(path, "rb") as f:
        data = f.read()
    seg = segments(data)
    props = inventor_props.read_cfb(Cfb(data))
    gb = seg.get("PmGraphicsSegment")
    if gb is None:
        raise PartFileError("The Inventor part (.ipt) contains no display mesh. Open and save it in Inventor, "
                            "or export it as STEP")
    gfaces = graphic_faces(gb)
    if not gfaces:
        raise PartFileError("The Inventor part (.ipt) contains no display mesh. Open and save it in Inventor, "
                            "or export it as STEP")
    bfaces = brep_faces(seg.get("PmBRepSegment", b""))
    match = match_faces(gfaces, bfaces)

    app = seg.get("PmAppSegment")
    states = material_states(gb)
    offsets = [o for o, _ in states]
    part_name, part_rgba = _part_appearance(app, props)
    groups = defaultdict(list)
    memo = {}
    for gi, g in enumerate(gfaces):
        bf = bfaces[match[gi]] if gi in match else None
        rgba = name = None
        if bf and bf["colors"]:
            name = max(bf["colors"])[1]                              # Stufe 2 (Fläche) vor 1 (Körper)
            rgba = appearance_rgba(app, name, memo)
            if rgba is None:
                name = None
        if rgba is None:
            rgba = _material_before(states, offsets, g["mark"])
        if rgba is None and states:     # neuere Dateien: Teilfarbe steht erst hinter den Flächen
            rgba = states[0][1]
        g["rgba"] = tuple(float(x) for x in rgba) if rgba else None
        # Farbe aus dem Grafiksegment trägt keinen Namen: gleich der Teildarstellung -> deren Name
        if name is None and rgba and part_rgba and color_key(rgba) == color_key(part_rgba):
            name = part_name
        g["name"] = name
        groups[bf["body"] if bf else None].append(g)

    bodies = []
    for key in sorted(groups, key=lambda k: (k is None, k or 0)):
        gs = groups[key]
        colors, names, verts, tris, normals, color_idx = [], [], [], [], [], []
        base = 0
        for g in gs:
            if g["rgba"] not in colors:
                colors.append(g["rgba"])
                names.append(g["name"])
            elif g["name"] and not names[colors.index(g["rgba"])]:
                names[colors.index(g["rgba"])] = g["name"]
            verts.append(g["pts"] * CM_TO_MM)
            normals.append(g["nor"])
            tris.append(g["idx"] + base)
            color_idx.append(np.full(len(g["idx"]), colors.index(g["rgba"]), dtype=np.int32))
            base += len(g["pts"])
        bodies.append(dict(verts=np.concatenate(verts), tris=np.concatenate(tris), normals=np.concatenate(normals),
                           colors=colors, names=names, color_idx=np.concatenate(color_idx)))
    if not any(len(b["tris"]) for b in bodies):
        raise InventorError("The Inventor part (.ipt) contains no triangles")
    return dict(bodies=bodies, faces=len(gfaces), matched=len(match), props=props)

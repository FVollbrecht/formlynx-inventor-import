# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Inventor-Bauteil (.ipt) -> Körper mit Netz und Farben, ohne Inventor und ohne bpy.

Netz aus PmGraphicsSegment (Anzeigenetz je Fläche), Körper und Flächenfarben aus PmBRepSegment (ASM/ACIS SAB),
Farbwerte aus PmAppSegment (Darstellungen). Zuordnung B-Rep-Fläche <-> Grafikfläche über die Eckpunkte
(liegen exakt im Netz), Feature-ID und Flächenart.
Prototyp mit Diagnose-Ausgabe: tools/ipt_probe.py.
"""
import bisect
import heapq
import math
import re
import struct
from collections import defaultdict

import numpy as np

from . import inventor_props, inventor_sketch
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


def graphic_faces(b, arr=None):
    """Flächen des Anzeigenetzes: dict(mark, feature, pts[N,3] cm, idx[M,3], nor[N,3]).
    Satz = Positionen (n Punkte), Indizes u32 (kleiner Typ), Normalen (n Vektoren Länge 1), Zusatz-Array (n).
    Die Typnummern vergibt jede Datei selbst (2017–2025 gesehen: Normalen t+1 oder t+6) -> nur der Aufbau zählt.
    Je Flächen-Eintrag erst die feine, dann die grobe Stufe -> nur der erste Satz zählt."""
    marks = [mm.start() for mm in FACE_MARK.finditer(b)]
    if arr is None:
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
        colors, feature, decals = [], None, []
        a = ptrs[f][0] if ptrs[f] else -1
        guard = 0
        while 0 <= a < count and kname[a] == "attrib" and guard < 100:
            strs = [v for kk, v in ents[a] if kk == "s"]
            ints = [v for kk, v in ents[a] if kk == "i"]
            if "INV_NMX_FACE_COLOR_ATTRIB" in strs and len(strs) > 1 and len(ints) > 3:
                colors.append((ints[3], strs[1]))
            elif "INV_NMX_SWEEPGENERATED_TAG" in strs and len(ints) > 4:
                feature = ints[4]
            elif "INV_MIX_DECAL" in strs and ints:
                decals.append((ints[-1], [v for kk, v in ents[a] if kk == "d"]))     # (Aufkleber-ID, Werte)
            a = ptrs[a][1] if len(ptrs[a]) > 1 else -1
            guard += 1
        surf = None
        sp = ptr_of(f, "surface")
        if sp:
            surf = (kind[sp[0]][1], [(kk, v) for kk, v in ents[sp[0]] if kk in ("v", "d")])
        out.append(dict(body=body_of(f), points=np.array(list(pts.values()), dtype=np.float64).reshape(-1, 3),
                        colors=colors, feature=feature, surface=surf, entity=f, decals=decals))
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


# ---------- Aufkleber ----------

# Skizzenbild im PmDCSegment (Satztyp je Datei verschieden: 0x141, 0x136 gesehen): fester Kopf, dann Breite, Höhe,
# Position x, y (double, cm). Linien haben an derselben Stelle „00 02 02“ statt „80 02 00“.
IMAGE_REC = re.compile(rb"\x80\x02\x00\x00\x00\x00\x00\x00\x03\x00\x00\x80.\x00\x00\x00\xff\xff\xff\xff"
                       rb"\x20\x0c\x00\x00.\x00\x00\x80", re.S)
DECAL_GRID = 48              # Gitterteilung je Bildseite (Umwickeln über mehrere Flächen)
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", "png"), (b"\xff\xd8\xff", "jpg"), (b"BM", "bmp"), (b"GIF8", "gif"))


def gif_rgba(raw):
    """Erstes Bild einer GIF -> (breite, höhe, RGBA float32 [h, w, 4], Zeile 0 = oben). Blender liest kein GIF."""
    w, h, flags = struct.unpack_from("<HHB", raw, 6)
    o = 13
    table = None
    if flags & 0x80:
        n = 3 << ((flags & 7) + 1)
        table = np.frombuffer(raw, np.uint8, n, o).reshape(-1, 3)
        o += n
    transparent = None
    while o < len(raw):
        tag = raw[o]
        if tag == 0x21:                                     # Erweiterung
            label = raw[o + 1]
            o += 2
            while raw[o]:
                if label == 0xF9 and raw[o] >= 4 and raw[o + 1] & 1:
                    transparent = raw[o + 4]
                o += raw[o] + 1
            o += 1
        elif tag == 0x2C:                                   # Bild
            x, y, iw, ih, f2 = struct.unpack_from("<HHHHB", raw, o + 1)
            o += 10
            if f2 & 0x80:
                n = 3 << ((f2 & 7) + 1)
                table = np.frombuffer(raw, np.uint8, n, o).reshape(-1, 3)
                o += n
            min_code = raw[o]
            o += 1
            data = bytearray()
            while raw[o]:
                data += raw[o + 1:o + 1 + raw[o]]
                o += raw[o] + 1
            idx = _lzw(bytes(data), min_code, iw * ih)
            if f2 & 0x40:                                   # Zeilensprung
                rows = list(range(0, ih, 8)) + list(range(4, ih, 8)) + list(range(2, ih, 4)) + list(range(1, ih, 2))
                img = np.empty((ih, iw), np.uint8)
                img[rows] = idx.reshape(ih, iw)
                idx = img.ravel()
            rgba = np.zeros((h, w, 4), np.float32)
            pal = np.zeros((256, 4), np.float32)
            pal[:len(table), :3] = table / 255.0
            pal[:, 3] = 1.0
            if transparent is not None:
                pal[transparent, 3] = 0.0
            rgba[y:y + ih, x:x + iw] = pal[idx.reshape(ih, iw)]
            return w, h, rgba
        else:
            break
    raise InventorError("GIF without image")


def _lzw(data, min_code, count):
    """GIF-LZW entpacken -> Indizes (uint8)"""
    clear, end = 1 << min_code, (1 << min_code) + 1
    out = bytearray()
    table = [bytes([i]) for i in range(clear)] + [b"", b""]
    size, bits, prev = min_code + 1, 0, None
    acc = nacc = 0
    for byte in data:
        acc |= byte << nacc
        nacc += 8
        while nacc >= size:
            code = acc & ((1 << size) - 1)
            acc >>= size
            nacc -= size
            if code == clear:
                table = table[:clear + 2]
                size, prev = min_code + 1, None
                continue
            if code == end:
                return np.frombuffer(bytes(out[:count]).ljust(count, b"\0"), np.uint8)
            if code < len(table):
                entry = table[code]
                if prev is not None:
                    table.append(prev + entry[:1])
            elif prev is not None:
                entry = prev + prev[:1]
                table.append(entry)
            else:
                continue
            out += entry
            prev = entry
            if len(table) == (1 << size) and size < 12:
                size += 1
    return np.frombuffer(bytes(out[:count]).ljust(count, b"\0"), np.uint8)


def embedded_images(cfb):
    """Eingebettete Bilder (RSeStorage/RefdFiles/RefdFile_N) in Dateireihenfolge -> [(bytes, endung)]"""
    out = []
    for p in sorted((p for p in cfb.paths() if p.startswith("RSeStorage/RefdFiles/RefdFile_")),
                    key=lambda p: int(re.sub(r"\D", "", p.rsplit("_", 1)[1]) or 0)):
        raw = cfb.get(p) or b""
        ext = next((e for magic, e in IMAGE_MAGIC if raw.startswith(magic)), None)
        if ext:
            out.append((raw, ext))
    return out


def sketch_images(dc):
    """Skizzenbilder aus dem PmDCSegment -> [(breite, höhe, x, y)] in cm, Skizzenkoordinaten"""
    out = []
    for mm in IMAGE_REC.finditer(dc):
        if mm.end() + 32 > len(dc):
            continue
        w, h, x, y = struct.unpack_from("<4d", dc, mm.end())
        if 0 < w < 1e5 and 0 < h < 1e5:
            out.append((w, h, x, y))
    return out


def _inside(pts, tris):
    """Bool je Punkt (K,2): liegt in einem der Dreiecke (T,3,2)"""
    out = np.zeros(len(pts), dtype=bool)
    a, b, c = tris[:, 0], tris[:, 1], tris[:, 2]
    v0, v1 = b - a, c - a
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    ok = np.abs(den) > 1e-14
    a, v0, v1, den = a[ok], v0[ok], v1[ok], den[ok]
    for s in range(0, len(pts), 512):
        p = pts[s:s + 512, None, :] - a[None]
        l1 = (p[..., 0] * v1[:, 1] - v1[:, 0] * p[..., 1]) / den
        l2 = (v0[:, 0] * p[..., 1] - p[..., 0] * v0[:, 1]) / den
        tol = -1e-6
        out[s:s + 512] = np.any((l1 >= tol) & (l2 >= tol) & (l1 + l2 <= 1 - tol), axis=1)
    return out


class _DecalFace:
    """Abbildung Skizze -> Fläche für einen Aufkleber (Ebene oder Kreiszylinder). Werte in cm.
    Attribut: Anker Skizze (a0, a1) <-> Anker Flächen-(u, v) (a2, a3), Skizzen-x-Richtung in (u, v) (a4, a5).
    Ebene (ACIS Ursprung, Normale, u): (u, v) in cm, v = n × u. Zylinder (Mitte, Achse, Hauptradius-Vektor):
    u = Höhe / Radius, v = Winkel (von Hauptradius zu Achse × Hauptradius) -> abgewickelt, Bogenlänge = Radius · v."""

    def __init__(self, surf, d, out_sign, gface):
        kind, vals = surf
        vecs = [np.array(v, dtype=np.float64) for k, v in vals if k == "v"]
        nums = [v for k, v in vals if k == "d"]
        self.anchor_s = np.array(d[0:2])
        self.anchor_uv = np.array(d[2:4])
        xuv = np.array(d[4:6]) / (np.hypot(*d[4:6]) or 1.0)
        if kind == "plane":
            self.root, n, u = vecs[:3]
            n, u = n / np.linalg.norm(n), u / np.linalg.norm(u)
            self.cyl, self.U, self.V, self.n = False, u, np.cross(n, u), n
            self.scale = 1.0
        elif kind == "cone" and len(vecs) >= 3 and len(nums) >= 2 and abs(nums[1]) < 1e-9 and abs(nums[0] - 1) < 1e-9:
            self.root, axis, major = vecs[:3]
            self.r = float(np.linalg.norm(major))
            self.axis = axis / np.linalg.norm(axis)
            self.e1 = major / self.r
            self.e2 = np.cross(self.axis, self.e1)
            self.h = nums[3] if len(nums) > 3 and nums[3] > 0 else self.r     # Höhe je u
            self.cyl, self.scale = True, 1.0 / self.r
        else:
            raise ValueError(kind)
        self.out_sign = out_sign
        # Skizzen-y in (u, v): senkrecht zu x, Vorzeichen so, dass x × y nach außen zeigt (Skizze von außen gesehen)
        yuv = np.array([-xuv[1], xuv[0]])
        tu, tv = self._tangents(self.anchor_uv)
        x3, y3 = xuv[0] * tu + xuv[1] * tv, yuv[0] * tu + yuv[1] * tv
        if np.dot(np.cross(x3, y3), self.normal(self.anchor_uv[None])[0]) < 0:
            yuv = -yuv
        self.M = np.column_stack([xuv, yuv]) * self.scale            # Skizzen-Versatz -> (u, v)-Versatz
        self.tris = None
        if gface is not None:
            uv = self.to_uv(gface["pts"])
            self.tris = uv[gface["idx"]]

    def _tangents(self, uv):
        if not self.cyl:
            return self.U, self.V
        v = uv[1]
        return self.axis, -math.sin(v) * self.e1 + math.cos(v) * self.e2

    def uv_of(self, s):
        return self.anchor_uv + (s - self.anchor_s) @ self.M.T

    def point(self, uv):
        if not self.cyl:
            return self.root + uv[:, :1] * self.U + uv[:, 1:2] * self.V
        return (self.root + (uv[:, :1] * self.h) * self.axis
                + self.r * (np.cos(uv[:, 1:2]) * self.e1 + np.sin(uv[:, 1:2]) * self.e2))

    def normal(self, uv):
        if not self.cyl:
            return np.tile(self.n * self.out_sign, (len(uv), 1))
        return self.out_sign * (np.cos(uv[:, 1:2]) * self.e1 + np.sin(uv[:, 1:2]) * self.e2)

    def to_uv(self, pts):
        q = pts - self.root
        if not self.cyl:
            return np.column_stack([q @ self.U, q @ self.V])
        ang = np.arctan2(q @ self.e2, q @ self.e1)
        a0 = self.anchor_uv[1]
        ang = a0 + (ang - a0 + math.pi) % (2 * math.pi) - math.pi      # nahe am Anker abwickeln
        return np.column_stack([(q @ self.axis) / self.h, ang])


def _decal_rect(rec, anchors):
    """Bildlage in der Skizze: Position = Ecke oben links des Bildes; das Bild liegt in dem Quadranten (vom Eckpunkt
    aus), der alle Anker enthält. -> (Ursprung, Bild-x-Vorzeichen, Bild-unten-Vorzeichen) oder None"""
    w, h, x, y = rec
    for sx in (1, -1):
        for sy in (-1, 1):
            ok = all(-1e-6 <= (ax - x) * sx <= w + 1e-6 and -1e-6 <= (ay - y) * sy <= h + 1e-6 for ax, ay in anchors)
            if ok:
                return sx, sy
    return None


def decals(bfaces, gfaces, match, dc, images):
    """Aufkleber -> [dict(verts[K,3] mm, normals[K,3], uv[K,2], quads[Q,4], body, image=(bytes, endung)|None)].
    B-Rep-Attribut INV_MIX_DECAL (ints …, Aufkleber-ID) an jeder Fläche, die der Aufkleber berührt (umwickelt).
    Größe/Lage in der Skizze aus dem Skizzenbild-Satz; Bild <-> Aufkleber und Satz <-> Aufkleber nach Reihenfolge
    (IDs aufsteigend), geprüft über die Anker. Bild als Gitter: jeder Punkt geht auf die Fläche, in deren
    Umriss (Anzeigenetz in (u, v)) er fällt; Punkte außerhalb aller Flächen fallen weg.
    Belegt an 3 Aufklebern (2 Dateien, Inventor-COM): Ebene, Ebene -> Zylinder, Zylinder."""
    recs = sketch_images(dc)
    by_b = {bi: gi for gi, bi in match.items()}
    groups = defaultdict(list)
    for bi, bf in enumerate(bfaces):
        for did, d in bf.get("decals", ()):
            if len(d) >= 6 and bf.get("surface"):
                groups[did].append((bi, d))
    out = []
    for k, did in enumerate(sorted(groups)):
        entries = groups[did]
        anchors = [tuple(d[0:2]) for _, d in entries]
        cand = ([recs[k]] if k < len(recs) else []) + recs
        rec = rect = None
        for r in cand:
            rect = _decal_rect(r, anchors)
            if rect:
                rec = r
                break
        if rec is None:
            continue
        faces = []
        for bi, d in entries:
            gi = by_b.get(bi)
            sign = 1.0
            try:
                f = _DecalFace(bfaces[bi]["surface"], d, 1.0, gfaces[gi] if gi is not None else None)
            except (ValueError, IndexError, ZeroDivisionError):
                continue
            if gi is not None:                                  # nach außen: wie die Normalen im Anzeigenetz
                g = gfaces[gi]
                sign = 1.0 if np.sum(np.einsum("ij,ij->i", f.normal(f.to_uv(g["pts"])), g["nor"])) >= 0 else -1.0
            if sign < 0:
                f = _DecalFace(bfaces[bi]["surface"], d, -1.0, gfaces[gi])
            faces.append((bfaces[bi]["body"], f))
        if not faces:
            continue
        w, h, x0, y0 = rec
        sx, sy = rect
        n = DECAL_GRID
        ix, iy = np.meshgrid(np.linspace(0, 1, n + 1), np.linspace(0, 1, n + 1))
        ix, iy = ix.ravel(), iy.ravel()
        s = np.column_stack([x0 + sx * ix * w, y0 + sy * (1 - iy) * h])     # iy = 1: obere Bildkante
        verts = np.full((len(s), 3), np.nan)
        normals = np.zeros((len(s), 3))
        best = np.full(len(s), np.inf)
        body = faces[0][0]
        for b, f in faces:
            uv = f.uv_of(s)
            ok = _inside(uv, f.tris) if f.tris is not None else np.ones(len(s), dtype=bool)
            dist = np.linalg.norm(s - f.anchor_s, axis=1)
            take = ok & (dist < best)                          # mehrdeutig: Fläche mit nächstem Anker
            if take.any():
                verts[take] = f.point(uv[take])
                normals[take] = f.normal(uv[take])
                best[take] = dist[take]
        valid = ~np.isnan(verts[:, 0])
        idx = np.arange(len(s)).reshape(n + 1, n + 1)
        quads = np.stack([idx[:-1, :-1], idx[:-1, 1:], idx[1:, 1:], idx[1:, :-1]], axis=-1).reshape(-1, 4)
        quads = quads[valid[quads].all(axis=1)]
        if not len(quads):
            continue
        used = np.unique(quads)
        remap = np.full(len(s), -1)
        remap[used] = np.arange(len(used))
        out.append(dict(verts=verts[used] * CM_TO_MM, normals=normals[used], uv=np.column_stack([ix, iy])[used],
                        quads=remap[quads], body=body, id=did,
                        image=images[min(k, len(images) - 1)] if images else None))
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
    arr = _arrays(gb)
    gfaces = graphic_faces(gb, arr)
    try:
        reference = inventor_sketch.read(seg, arr)
    except Exception:                                   # Bezugsgeometrie ist Beigabe, nie Grund zum Abbruch
        reference = dict(sketches=[], planes=[], axes=[], center=None)
    has_ref = bool(reference["sketches"] or reference["planes"])
    if not gfaces and not has_ref:
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
    if not any(len(b["tris"]) for b in bodies) and not has_ref:
        raise InventorError("The Inventor part (.ipt) contains no triangles")
    keys = sorted(groups, key=lambda k: (k is None, k or 0))
    found = decals(bfaces, gfaces, match, seg.get("PmDCSegment", b""), embedded_images(Cfb(data)))
    for dcl in found:
        dcl["body"] = keys.index(dcl["body"]) if dcl["body"] in keys else 0     # Index in bodies
    found = [dcl for dcl in found if len(bodies[dcl["body"]]["tris"])]
    return dict(bodies=bodies, faces=len(gfaces), matched=len(match), props=props, decals=found,
                reference=reference)

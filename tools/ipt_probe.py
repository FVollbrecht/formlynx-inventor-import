"""Prototyp (2026-11-21): Inventor-Bauteil (.ipt) -> Körper mit Netz und Farbe, ohne Inventor.

Netz aus PmGraphicsSegment (Anzeigenetz je Fläche), Körper und Farben aus PmBRepSegment (ASM/ACIS SAB),
Farbwerte aus PmAppSegment (Darstellungen). Zuordnung B-Rep-Fläche <-> Grafikfläche über die Eckpunkte
(liegen exakt im Netz), Feature-ID und Flächenart. Erkenntnisse: atlas/ATLAS.md („.ipt-Netz“).

Aufruf: python -X utf8 tools/ipt_probe.py <datei.ipt> [...]
"""
import bisect
import importlib.util
import re
import struct
import sys
import zlib
from collections import defaultdict

import numpy as np

spec = importlib.util.spec_from_file_location("inventor_data", r"I:\GIT\FORMLYNX\modulynx\inventor_data.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

HEAD = b"\x02\x00\x00\x30"
# Kopf eines Flächen-Eintrags: Feld (k, k, 0x10000000) mit k Paaren (Feature-ID u32, ffffffff), meist k = 1.
# Arbeitsebenen/Skizzen haben andere Köpfe und bleiben so außen vor.
FACE_MARK = re.compile(rb"\x02\x00\x00\x30([\x01-\x40])\x00\x00\x00\1\x00\x00\x00\x00\x00\x00\x10....\xff\xff\xff\xff", re.S)
# Materialsatz im Grafiksegment: Kopf, 16 Byte Null, RGBA ambient, RGBA diffus, ...
POINT_TOL = 1e-3             # cm: B-Rep-Eckpunkt liegt im Grafiknetz
SURF_TOL = 2e-3              # cm: Netzpunkte liegen auf der B-Rep-Fläche
DEFAULT_RGBA = (0.8, 0.8, 0.8, 1.0)


# ---------- Datei ----------

def segments(path):
    """{Segmentname: entpackte Bytes} aus RSeStorage."""
    cfb = m.Cfb(open(path, "rb").read())
    out = {}
    for p in cfb.paths():
        if p.startswith("RSeStorage/M"):
            words = re.findall(rb"(?:[\x20-\x7e]\x00){4,}", cfb.get(p) or b"")
            raw = cfb.get("RSeStorage/B" + p[12:])
            if not words or raw is None:
                continue
            try:
                raw = zlib.decompressobj().decompress(raw[18:])
            except zlib.error:
                continue
            out.setdefault(words[0].decode("utf-16le"), raw)
    return out


def graphics(path):
    return segments(path).get("PmGraphicsSegment")


# ---------- Grafiksegment ----------

def arrays(b):
    out = []
    for mm in re.finditer(re.escape(HEAD), b):
        o = mm.start()
        if o + 16 > len(b):
            continue
        a, c, t = struct.unpack_from("<3I", b, o + 4)
        if 0 < a <= c < 10_000_000 and (t < 16 or 250 < t < 400):
            out.append((o, t, a))
    return out


def faces(b):
    """Satz = Positionen (Typ t), Indizes u32 (kleiner Typ, 8 oder 0), Normalen (t+1) gleicher Anzahl.
    Typnummern hängen von der Version ab (t = 278, 279, 281, 284 gesehen)."""
    marks = [mm.start() for mm in FACE_MARK.finditer(b)]
    arr = arrays(b)
    sets = []
    for i, (o, t, n) in enumerate(arr):
        if t > 250 and i + 2 < len(arr) and arr[i + 1][1] < 16 and arr[i + 2][1] == t + 1 and arr[i + 2][2] == n:
            pos = struct.unpack_from(f"<{n*3}f", b, o + 16)
            io, _, ni = arr[i + 1]
            idx = struct.unpack_from(f"<{ni}I", b, io + 16)
            if ni % 3 or (idx and max(idx) >= n):
                continue
            mark = max((k for k in marks if k < o), default=-1)
            sets.append(dict(at=o, mark=mark, pos=pos, idx=idx, n=n))
    # Je Flächen-Eintrag erst die feine, dann die grobe Stufe -> nur den ersten Satz nehmen
    first, seen = [], set()
    for s in sets:
        if s["mark"] not in seen:
            seen.add(s["mark"])
            first.append(s)
    return sets, first, marks


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
    """Materialsätze im Grafiksegment als [(offset, rgba)], aufsteigend.
    Kopf variiert (39010000 02000000 in der Tabelle am Ende, 2b010000 01000000 zwischen den Flächen), darum über
    die Form gesucht: 16 Null-Byte + zweimal dieselbe RGBA. Ein Satz gilt für die folgenden Flächen
    (Teilfarbe vor der ersten Fläche, Feature-Darstellung vor den Flächen des Features) – an 4 Dateien gesehen."""
    out = []
    for mm in re.finditer(rb"\x00{16,}", b):
        rgba = _rgba_pair_at(b, mm.end() - 16)
        if rgba:
            out.append((mm.end() - 16, rgba))
    return out


def material_before(states, mark):
    k = bisect.bisect_left([o for o, _ in states], mark) - 1
    return states[k][1] if k >= 0 else None


# ---------- B-Rep (ASM/ACIS SAB) ----------

def sab_entities(b):
    """Entities als Token-Listen; Index = Zeigerwert in SAB (Kopf ist Entity 0)."""
    i = b.find(b"ASM BinaryFile")
    if i < 0:
        return []
    o = i + len(b"ASM BinaryFile4") + 16
    ents, cur, n = [], [], len(b)
    while o < n:
        t = b[o]; o += 1
        if t == 0x04:
            cur.append(("i", struct.unpack_from("<i", b, o)[0])); o += 4
        elif t == 0x06:
            cur.append(("d", struct.unpack_from("<d", b, o)[0])); o += 8
        elif t in (0x07, 0x08, 0x09, 0x12):
            size = {0x07: 1, 0x08: 2, 0x09: 4, 0x12: 4}[t]
            ln = int.from_bytes(b[o:o + size], "little"); o += size
            cur.append(("s", b[o:o + ln].decode("latin-1"))); o += ln
        elif t in (0x0A, 0x0B):
            cur.append(("b", t == 0x0B))
        elif t == 0x0C:
            cur.append(("p", struct.unpack_from("<i", b, o)[0])); o += 4
        elif t in (0x0D, 0x0E):
            ln = b[o]; o += 1
            cur.append(("id", b[o:o + ln].decode("latin-1"))); o += ln
        elif t in (0x0F, 0x10):
            pass
        elif t == 0x11:
            ents.append(cur); cur = []
            name = "-".join(v for k, v in ents[-1] if k == "id")     # Bezeichner kommen als Einzelteile
            if name.startswith(("End-of-ASM-data", "Begin-of-ASM-History")):
                ents.pop()              # Historie (alte Stände) nicht mitlesen
                break
        elif t in (0x13, 0x14):
            cur.append(("v", struct.unpack_from("<3d", b, o))); o += 24
        elif t == 0x15:
            cur.append(("e", struct.unpack_from("<i", b, o)[0])); o += 4
        elif t == 0x16:
            cur.append(("v2", struct.unpack_from("<2d", b, o))); o += 16
        else:
            break                       # unbekanntes Tag: Rest ist Historie o. Ä.
    return ents


def _kind(ent):
    """Letzter Bezeichner vor dem ersten Wert: 'face', 'surface', 'attrib' …; Unterart davor ('plane', 'ATTRIB_CUSTOM')."""
    ids = []
    for k, v in ent:
        if k != "id":
            break
        ids.append(v)
    return ids[-1] if ids else "", ids[0] if ids else ""


def brep_faces(b):
    """-> Liste dict(face, body, points[N,3], colors[(stufe, name)], feature, surface=(art, werte))."""
    ents = sab_entities(b)
    if not ents:
        return []
    kind = [_kind(e) for e in ents]

    def ptrs(i):
        return [v for k, v in ents[i] if k == "p"]

    def ptr_of(i, wanted):
        return [p for p in ptrs(i) if 0 <= p < len(ents) and kind[p][0] == wanted]

    def body_of(face):
        shell = (ptr_of(face, "shell") or [None])[0]
        lump = (ptr_of(shell, "lump") or [None])[-1] if shell is not None else None
        body = (ptr_of(lump, "body") or [None])[0] if lump is not None else None
        return body

    loops_of = defaultdict(list)
    for i, (k, _) in enumerate(kind):
        if k == "loop":
            f = ptr_of(i, "face")
            if f:
                loops_of[f[-1]].append(i)
    coedges_of_loop = defaultdict(list)
    for i, (k, _) in enumerate(kind):
        if k == "coedge":
            lp = ptr_of(i, "loop")
            if lp:
                coedges_of_loop[lp[0]].append(i)

    out = []
    for f, (k, _) in enumerate(kind):
        if k != "face":
            continue
        pts = {}
        for lp in loops_of[f]:
            for ce in coedges_of_loop[lp]:
                for e in ptr_of(ce, "edge"):
                    for v in ptr_of(e, "vertex"):
                        for p in ptr_of(v, "point"):
                            vec = next((val for kk, val in ents[p] if kk == "v"), None)
                            if vec:
                                pts[p] = vec
        colors, feature = [], None
        a = ptrs(f)[0] if ptrs(f) else -1
        guard = 0
        while 0 <= a < len(ents) and kind[a][0] == "attrib" and guard < 100:
            strs = [v for kk, v in ents[a] if kk == "s"]
            ints = [v for kk, v in ents[a] if kk == "i"]
            if "INV_NMX_FACE_COLOR_ATTRIB" in strs and len(strs) > 1 and len(ints) > 3:
                colors.append((ints[3], strs[1]))
            elif "INV_NMX_SWEEPGENERATED_TAG" in strs and len(ints) > 4:
                feature = ints[4]
            pa = ptrs(a)
            a = pa[1] if len(pa) > 1 else -1
            guard += 1
        surf = None
        sp = ptr_of(f, "surface")
        if sp:
            s = sp[0]
            vals = [(kk, v) for kk, v in ents[s] if kk in ("v", "d")]
            surf = (kind[s][1], vals)
        out.append(dict(face=f, body=body_of(f), points=np.array(list(pts.values())).reshape(-1, 3),
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
            c, ax, R, r = np.array(vs[0]), np.array(vs[1]), ds[0], abs(ds[1])
            q = pts - c
            h = q @ ax
            rho = np.linalg.norm(q - np.outer(h, ax), axis=1)
            return float(np.abs(np.hypot(rho - R, h) - r).max())
        if art == "cone" and len(ds) >= 3 and abs(ds[1]) < 1e-12:      # Zylinder (sin = 0)
            root, ax, major = np.array(vs[0]), np.array(vs[1]), np.array(vs[2])
            q = pts - root
            rho = np.linalg.norm(q - np.outer(q @ ax, ax), axis=1)
            return float(np.abs(rho - np.linalg.norm(major)).max())
    except (IndexError, ValueError):
        return None
    return None


# ---------- Darstellungen ----------

def appearance_rgba(app, name):
    """Darstellungsname (deutsch, wie im B-Rep) -> RGBA aus PmAppSegment oder None."""
    if not app or not name:
        return None
    key = name.encode("utf-16le")
    start = 0
    while True:
        k = app.find(key, start)
        if k < 0:
            return None
        asset = app.find("1:".encode("utf-16le"), k, k + 800)
        if asset >= 0:
            for o in range(asset, min(asset + 900, len(app) - 16)):
                r, g, bl, a = struct.unpack_from("<4f", app, o)
                if a == 1.0 and all(0.0 <= x <= 1.0 for x in (r, g, bl)) and max(r, g, bl) > 0.01:
                    return (r, g, bl, a)
        start = k + 2


# ---------- Zusammenführen ----------

def read_part(path):
    """-> dict(bodies=[dict(body, faces=[set], rgba=[...])], unmatched=[...], info)"""
    seg = segments(path)
    gb = seg.get("PmGraphicsSegment")
    if gb is None:
        raise ValueError("kein PmGraphicsSegment")
    _, gfaces, _ = faces(gb)
    gfaces = [g for g in gfaces if g["mark"] >= 0]
    for g in gfaces:
        g["pts"] = np.array(g["pos"]).reshape(-1, 3)
        g["feature"] = struct.unpack_from("<I", gb, g["mark"] + 16)[0]
    bfaces = brep_faces(seg.get("PmBRepSegment", b""))

    # Punkt -> Grafikflächen (gerastert auf POINT_TOL, Nachbarzellen mitprüfen)
    grid = defaultdict(set)
    for gi, g in enumerate(gfaces):
        for p in np.floor(g["pts"] / POINT_TOL).astype(np.int64):
            grid[tuple(p)].add(gi)

    def faces_at(p):
        c = np.floor(np.asarray(p) / POINT_TOL).astype(np.int64)
        hit = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    hit |= grid.get((c[0] + dx, c[1] + dy, c[2] + dz), set())
        return hit

    # Kandidaten je B-Rep-Fläche: Grafikflächen, die alle Eckpunkte enthalten; Feature-ID muss passen
    pairs = []
    for bi, f in enumerate(bfaces):
        cand = None
        for p in f["points"]:
            hit = faces_at(p)
            cand = hit if cand is None else cand & hit
        if cand is None:                      # Fläche ohne Eckpunkte (Kugel, Torus): alle prüfen
            cand = set(range(len(gfaces)))
        for gi in cand:
            if f["feature"] is not None and gfaces[gi]["feature"] != f["feature"]:
                continue
            d = surface_distance(f["surface"], gfaces[gi]["pts"])
            if d is not None and d > SURF_TOL:
                continue
            pairs.append((0 if d is not None else 1, len(f["points"]) == 0, bi, gi))
    # Eindeutige zuerst: immer die B-Rep-Fläche mit den wenigsten freien Kandidaten zuordnen
    cands = defaultdict(list)
    for prio, novert, bi, gi in sorted(pairs):
        cands[bi].append(gi)
    used_g, match = set(), {}
    while cands:
        bi = min(cands, key=lambda k: (len([g for g in cands[k] if g not in used_g]), k))
        free = [g for g in cands.pop(bi) if g not in used_g]
        if free:
            used_g.add(free[0]); match[free[0]] = bi

    app = seg.get("PmAppSegment")
    states = material_states(gb)
    bodies = defaultdict(list)
    for gi, g in enumerate(gfaces):
        f = bfaces[match[gi]] if gi in match else None
        name = None
        if f and f["colors"]:
            name = max(f["colors"])[1]                 # Stufe 2 (Fläche) vor 1 (Körper)
        rgba = appearance_rgba(app, name) if name else None
        if rgba is None:
            rgba = material_before(states, g["mark"])
        g["color_name"] = name
        g["rgba"] = rgba or DEFAULT_RGBA
        bodies[f["body"] if f else None].append(g)
    return dict(bodies=bodies, gfaces=gfaces, bfaces=bfaces, matched=len(match))


if __name__ == "__main__":
    for path in sys.argv[1:]:
        r = read_part(path)
        print(f"{path.split('/')[-1]}: Grafikflächen {len(r['gfaces'])}, B-Rep-Flächen {len(r['bfaces'])}, "
              f"zugeordnet {r['matched']}, Körper {len([k for k in r['bodies'] if k is not None])}"
              f"{', ohne Körper ' + str(len(r['bodies'][None])) if None in r['bodies'] else ''}")
        for body, gs in r["bodies"].items():
            cols = defaultdict(int)
            for g in gs:
                cols[(g["color_name"], tuple(round(x, 3) for x in g["rgba"][:3]))] += 1
            print(f"   Körper {body}: {len(gs)} Flächen, {sum(len(g['idx']) // 3 for g in gs)} Dreiecke, Farben {dict(cols)}")

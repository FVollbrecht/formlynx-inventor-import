# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Joints und Abhängigkeiten einer Inventor-Baugruppe (.iam) lesen – ohne bpy.

RSeStorage-Segment = Meta-Stream M… (Blockgrößen, Blocktyp-GUIDs) + Daten-Stream B… (Blöcke).
Aufbau nach eigener Untersuchung, Segmentrahmen angelehnt an InventorLoader (J. M. Plonka, GPL-2, nur gelesen).
Joints und Abhängigkeiten sind im AmDcSegment derselbe Knoten (Placement-Constraint) mit einem Stil-Byte.
Je Seite: Vorkommen (Name) und Gelenk-Koordinatensystem im Teil (Transformation3D, cm).
Namen der Einträge kommen aus dem Browser-Segment, gleiche Reihenfolge.
"""
import re
import struct
import uuid
import zlib

import numpy as np

from . import inventor_zstd
from .inventor_data import MAX_UNPACKED, InventorError

CM_TO_MM = 10.0

# Blocktypen (erste 8 Hex-Stellen der GUID)
T_CONSTRAINT = "4E86F047"
T_SELECTION = ("B91FCE52", "F2C761B8")
T_OCCURRENCE = "90874D61"
T_OCC_NAME = "2B48A42B"
T_FRAME = "55CE9B23"
T_VALUE = "258EC6E1"
T_PARAMETER = "90874D26"

# Stil-Byte -> Art. Gemessen an testdaten/inventor/iam/joints/Baugruppe2.iam (Inventor 2026).
KINDS = {
    0x01: 'MATE', 0x02: 'FLUSH', 0x04: 'TANGENT', 0x14: 'SYMMETRY',
    0x0E: 'REVOLUTE', 0x10: 'RIGID', 0x11: 'BALL', 0x12: 'CYLINDRICAL', 0x13: 'PLANAR',
    0x0F: 'SLIDER',             # nicht gemessen: Lücke zwischen Drehbar und Starr
    0x07: 'ROTATE_TRANSLATE',   # Bewegung Drehung/Translation, Wert = Weg je Umdrehung in cm (iam/rottrans/Ensamblaje1)
}
JOINTS = {'REVOLUTE', 'SLIDER', 'CYLINDRICAL', 'PLANAR', 'BALL', 'RIGID'}


# ---------- Segmente ----------

def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _text8(b, o):
    n = _u32(b, o)
    return o + 4 + n


def _unpack(kind, raw):
    if kind == 2:
        return inventor_zstd.decompress(raw, MAX_UNPACKED)
    return zlib.decompressobj().decompress(raw, MAX_UNPACKED)


def _meta(m):
    """Meta-Stream -> (Segmentname, [(Länge, hat_Daten)], [Typ-GUID])"""
    o = _text8(m, 0)
    ver = struct.unpack_from("<H", m, o)[0]
    o += 2 + 16
    n = _u32(m, o)
    name = m[o + 4:o + 4 + 2 * n].decode("utf-16-le", "replace")
    o += 4 + 2 * n + 16 + 12
    if ver < 7:
        o = _text8(m, o + 4)
        o = _text8(m, o + 4)
    else:
        o = _text8(m, o)
        o = _text8(m, o)
    data = _unpack(m[o], m[o + 1:])
    o = 14
    cnt = _u32(data, o)
    o += 4
    sizes = []
    for _ in range(cnt):
        v = _u32(data, o)
        sizes.append((v & 0x7FFFFFFF, bool(v & 0x80000000)))
        o += 4
    o += 4
    cnt = _u32(data, o)
    o += 4
    o += cnt * (32 if ver == 3 else 20 if ver == 4 else 10)
    o += 4
    cnt = _u32(data, o)
    o += 4 + cnt * 28 + 4
    cnt = _u32(data, o)
    o += 4
    types = []
    for _ in range(cnt):
        types.append(str(uuid.UUID(bytes_le=bytes(data[o:o + 16])))[:8].upper())
        o += 28
    return name, sizes, types


def _trailer(b, o):
    flag = b[o]
    o += 1
    if not flag:
        return o
    n = _u32(b, o)
    o += 4
    if n & 0x80000000:
        return o
    for _ in range(n):
        o = _text8(b, o)
        t = _u32(b, o)
        o += 4
        if t == 1:
            o += 3
        elif t in (3, 7):
            o += 4
        elif t in (8, 10):
            o += 6
        elif t == 11:
            o += 10
        elif t == 14:
            o += 2
            o += 4 + _u32(b, o)
        else:
            raise InventorError("Unknown segment trailer")
    o += 4
    cnt = _u32(b, o)
    o += 4
    if cnt:
        o += 8
        for _ in range(cnt):
            o = _text8(b, o) + 4
    return o


def segment_nodes(cfb, wanted):
    """{Segmentname: [(Typ, Daten)]} für die gewünschten Segmente; Knotenindex = Listenindex + 1"""
    out = {}
    for p in cfb.paths():
        if not re.fullmatch(r"RSeStorage/M[a-z0-9]+", p):
            continue
        try:
            name, sizes, types = _meta(cfb.get(p))
        except (struct.error, IndexError, zlib.error, inventor_zstd.ZstdError):
            continue
        if name not in wanted:
            continue
        raw = cfb.get("RSeStorage/B" + p[12:])
        if raw is None or len(raw) < 20:
            continue
        try:
            data = _unpack(raw[17], raw[18:])
            nodes, o = [], 0
            for size, has in sizes:
                if not has:
                    continue
                t = _u32(data, o)
                nodes.append((types[t & 0xFF], bytes(data[o + 4:o + 4 + size])))
                o += size + 8
                o = _trailer(data, o)
        except (struct.error, IndexError, zlib.error, inventor_zstd.ZstdError) as error:
            raise InventorError(f"Inventor segment {name} unreadable: {error}")
        out[name] = nodes
    return out


# ---------- Knoten ----------

def _refs(b, count):
    """Verweise 0x8000xxxx an beliebiger Byteposition, in Reihenfolge"""
    out = []
    for k in range(len(b) - 3):
        v = _u32(b, k)
        if v & 0xFFFF0000 == 0x80000000 and 0 < (v & 0xFFFF) <= count:
            out.append(v & 0xFFFF)
    return out


def _lists(b):
    """Verweislisten: 02 00 00 30, Anzahl, Anzahl, 0x10, Verweise"""
    out, k = [], 0
    while True:
        k = b.find(b"\x02\x00\x00\x30", k)
        if k < 0:
            return out
        n = _u32(b, k + 4) if k + 8 <= len(b) else 0
        if 0 < n < 50 and k + 16 + 4 * n <= len(b) and _u32(b, k + 12) == 0x10:
            out.append((k, [_u32(b, k + 16 + 4 * j) & 0xFFFF for j in range(n)]))
            k += 16 + 4 * n
        else:
            k += 4


def _utf16(b):
    return [x.decode("utf-16-le") for x in re.findall(rb"(?:[\x20-\x7e\xa0-\xff]\x00){2,}", b)]


def transformation(b, i):
    """Transformation3D: u16 Einsen-Maske, u16 Nullen-Maske, sonst Doubles; zeilenweise, Verschiebung cm -> mm"""
    ones, zeros = struct.unpack_from("<HH", b, i)
    i += 4
    m = np.eye(4)
    for j in range(16):
        bit = 1 << j
        r, c = divmod(j, 4)
        if zeros & bit:
            m[r, c] = -1.0 if ones & bit else 0.0
        elif ones & bit:
            m[r, c] = 1.0
        else:
            m[r, c] = struct.unpack_from("<d", b, i)[0]
            i += 8
    m[:3, 3] *= CM_TO_MM
    return m


_PARAM_NAME = re.compile(rb"([\x01-\x40])\x00\x00\x00((?:[\x20-\x7e\xa0-\xff]\x00)+)", re.S)


def _parameter(b):
    """Wert eines Parameters: u32 Zeichenzahl, Name UTF-16 („d16“), 12 Byte, dann Double (rad bzw. cm)"""
    for m in _PARAM_NAME.finditer(b):
        n = m.group(1)[0]
        if len(m.group(2)) < 2 * n:
            continue
        k = m.start(2) + 2 * n + 12
        if k + 8 <= len(b):
            return struct.unpack_from("<d", b, k)[0]
    return 0.0


# Art -> Klasse des Browser-Eintrags
ENTRY_CLASS = {
    'REVOLUTE': "AmRevoluteJointEntry", 'SLIDER': "AmTranslationJointEntry", 'CYLINDRICAL': "AmCylindricalJointEntry",
    'PLANAR': "AmPlanarJointEntry", 'BALL': "AmBallJointEntry", 'RIGID': "AmWeldJointEntry",
    'MATE': "AmMateEntry", 'FLUSH': "AmFlushEntry", 'TANGENT': "AmTangentEntry", 'SYMMETRY': "AmSymmetryEntry",
    'ROTATE_TRANSLATE': "AmRelateRotateTranslateEntry",
}


def _browser_names(nodes):
    """Namen der Abhängigkeiten im Browser je Eintragsklasse, in Reihenfolge: {Klasse: [Name]}"""
    wanted = set(ENTRY_CLASS.values())
    out = {}
    for _t, b in nodes:
        texts = _utf16(b)
        for i, t in enumerate(texts):
            if t in wanted:             # manche Einträge (Muster) haben keinen eigenen Namen
                out.setdefault(t, []).append(texts[i - 1] if i else None)
                break
    return out


def read(cfb):
    """Joints und Abhängigkeiten -> [dict(name, kind, style, sides, values)].
    sides: [(Vorkommen-Name, 4x4 Rahmen im Teil in mm oder None)] – Rahmen nur bei Joints."""
    segs = segment_nodes(cfb, ("AmDcSegment", "AmBrowserSegment"))
    nodes = segs.get("AmDcSegment")
    if not nodes:
        return []
    count = len(nodes)

    def typ(i):
        return nodes[i - 1][0] if 0 < i <= count else ""

    def data(i):
        return nodes[i - 1][1]

    def occ_name(i):
        for r in _refs(data(i), count):
            if typ(r) == T_OCC_NAME:
                texts = _utf16(data(r))
                if texts:
                    return texts[0]
        return None

    names = _browser_names(segs.get("AmBrowserSegment", []))
    out = []
    for i, (t, b) in enumerate(nodes, 1):
        if t != T_CONSTRAINT:
            continue
        lists = _lists(b)
        if not lists or lists[0][0] < 1:
            continue
        style = b[lists[0][0] - 1]
        kind = KINDS.get(style, 'UNKNOWN')
        sides = []
        for sel in lists[0][1]:
            if typ(sel) not in T_SELECTION:
                continue
            occ, frame = None, None
            for r in _refs(data(sel), count):
                if typ(r) == T_OCCURRENCE and occ is None:
                    occ = occ_name(r)
                elif typ(r) == T_FRAME and frame is None and len(data(r)) >= 54:
                    try:
                        frame = transformation(data(r), 50)
                    except struct.error:
                        frame = None
            sides.append((occ, frame))
        values = []
        for val in (lists[1][1] if len(lists) > 1 else []):
            if typ(val) != T_VALUE:
                continue
            par = [r for r in _refs(data(val), count) if typ(r) == T_PARAMETER]
            values.append(_parameter(data(par[0])) if par else 0.0)
        out.append(dict(name=None, kind=kind, style=style, sides=sides, values=values))
    # Namen aus dem Browser: je Art gleiche Reihenfolge; nur übernehmen, wenn die Anzahl je Art passt
    for kind, cls in ENTRY_CLASS.items():
        entries = [e for e in out if e["kind"] == kind]
        if entries and len(names.get(cls, ())) == len(entries):
            for entry, name in zip(entries, names[cls]):
                entry["name"] = entry["name"] or name
    for n, entry in enumerate(out, 1):
        if not entry["name"]:
            entry["name"] = f"{entry['kind'].title()}:{n}"
    return out

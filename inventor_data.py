# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Inventor-Baugruppen (.iam) lesen – ohne bpy, ohne Inventor.

Geometrie kommt aus dem Grafik-Cache, den Inventor für den Express-Modus in der Baugruppe speichert
(CacheGraphics/OGSCache, bzip2): fertig tessellierte Netze plus Platzierung je Vorkommen, Einheit cm.
Namen kommen aus dem Modellbrowser in der RSe-Datenbank (zlib, ab Inventor 2026 Zstandard). Das Format ist undokumentiert;
die Erkenntnisse stammen aus dem Prototyp in AssemblyHTML (src/inventor, docs/Plan-Inventor-Import.md),
getestet mit Inventor 2023.4 und 2025.4.

Bauteile (.ipt) haben keinen Grafik-Cache, nur das ShapeManager-B-Rep -> InventorError mit Hinweis auf STEP.
"""
import bz2
import re
import struct
import zlib

import numpy as np

from . import inventor_zstd

OGS_PATH = "CacheGraphics/OGSCache/OGS/DefaultScene/streams"
LWUFRX_PATH = "CacheGraphics/LWUFRx/streams"
CM_TO_MM = 10.0
CFB_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT, ENDOFCHAIN = 0xFFFFFFFF, 0xFFFFFFFE
MAX_STREAMS = 20000
MAX_TOTAL = 1 << 30                 # Summe aller Stream-Größen (Schutz gegen beschädigte Dateien)
MAX_UNPACKED = 512 << 20            # bzip2/zlib-Ausgabe höchstens so groß


class InventorError(Exception):
    """Datei nicht lesbar; die Meldung ist für den Nutzer gedacht (englisch, wie die UI)"""


class PartFileError(InventorError):
    """Bauteil (.ipt) ohne Grafik-Cache"""


# ---------- OLE-Verbunddatei (MS-CFB), nur lesen ----------

class Cfb:
    """Streams als 'Pfad/zum/Stream' -> bytes, erst bei Bedarf gelesen.
    Steuerzeichen am Namensanfang (\\x05, \\x01) werden entfernt."""

    def __init__(self, data):
        if len(data) < 512 or data[:8] != CFB_SIGNATURE:
            raise InventorError("Not an Inventor file (no OLE compound file)")
        self.data = data
        sec_shift, mini_shift = struct.unpack_from("<HH", data, 30)
        if sec_shift not in (9, 12):
            raise InventorError("Damaged Inventor file (sector size)")
        self.sec = 1 << sec_shift
        self.mini = 1 << mini_shift
        num_fat, first_dir = struct.unpack_from("<II", data, 44)
        self.cutoff, first_mini_fat = struct.unpack_from("<II", data, 56)
        first_difat = struct.unpack_from("<I", data, 68)[0]
        self.n_sectors = (len(data) - self.sec) // self.sec + 1

        # DIFAT -> FAT-Sektoren
        fat_secs = list(struct.unpack_from("<109I", data, 76))[:num_fat]
        s, guard, per = first_difat, 0, self.sec // 4 - 1
        while s not in (ENDOFCHAIN, FREESECT) and len(fat_secs) < num_fat:
            if s >= self.n_sectors or guard > self.n_sectors:
                raise InventorError("Damaged Inventor file (DIFAT)")
            vals = struct.unpack_from(f"<{per + 1}I", data, self._off(s))
            fat_secs += vals[:per][:num_fat - len(fat_secs)]
            s, guard = vals[per], guard + 1
        fat = []
        for s in fat_secs:
            if s >= self.n_sectors:
                raise InventorError("Damaged Inventor file (FAT)")
            fat.extend(struct.unpack_from(f"<{self.sec // 4}I", data, self._off(s)))
        self.fat = fat

        # Verzeichnis
        directory = self._read_chain(first_dir)
        entries = []
        for o in range(0, len(directory) - 127, 128):
            name_len = struct.unpack_from("<H", directory, o + 64)[0]
            chars = max(0, min(name_len // 2 - 1, 32))
            name = directory[o:o + chars * 2].decode("utf-16-le", "replace").lstrip("".join(map(chr, range(32))))
            left, right, child = struct.unpack_from("<III", directory, o + 68)
            start, size = struct.unpack_from("<II", directory, o + 116)
            entries.append((name, directory[o + 66], left, right, child, start, size))
        if not entries or entries[0][1] != 5:
            raise InventorError("Damaged Inventor file (root directory)")
        root = entries[0]
        self.mini_stream = self._read_chain(root[5], root[6])
        mini_fat_bytes = b"" if first_mini_fat == ENDOFCHAIN else self._read_chain(first_mini_fat)
        self.mini_fat = list(struct.unpack_from(f"<{len(mini_fat_bytes) // 4}I", mini_fat_bytes))

        self.streams = {}
        total, seen = 0, set()

        def walk(index, prefix):
            nonlocal total
            stack = [index]
            while stack:
                i = stack.pop()
                if i == FREESECT or i >= len(entries) or i in seen:
                    continue
                seen.add(i)
                name, kind, left, right, child, start, size = entries[i]
                stack += (left, right)
                path = prefix + "/" + name if prefix else name
                if kind == 1:
                    walk(child, path)
                elif kind == 2:
                    total += size
                    if len(self.streams) >= MAX_STREAMS or total > MAX_TOTAL:
                        raise InventorError("Damaged Inventor file (too many or too large streams)")
                    self.streams[path] = (start, size)

        walk(root[4], "")

    def _off(self, sector):
        return (sector + 1) * self.sec

    def _chain(self, start, table, limit):
        out, s = [], start
        while s not in (ENDOFCHAIN, FREESECT):
            if s >= len(table) or len(out) > limit:
                raise InventorError("Damaged Inventor file (sector chain)")
            out.append(s)
            s = table[s]
        return out

    def _read_chain(self, start, size=None):
        secs = self._chain(start, self.fat, self.n_sectors)
        parts = []
        for s in secs:
            o = self._off(s)
            if o >= len(self.data):
                raise InventorError("Damaged Inventor file (truncated)")
            parts.append(self.data[o:o + self.sec])
        out = b"".join(parts)
        return out if size is None else out[:size]

    def _read_mini(self, start, size):
        parts = []
        for s in self._chain(start, self.mini_fat, len(self.mini_stream) // self.mini + 1):
            parts.append(self.mini_stream[s * self.mini:(s + 1) * self.mini])
        out = b"".join(parts)[:size]
        if len(out) < size:
            raise InventorError("Damaged Inventor file (mini stream)")
        return out

    def paths(self):
        return list(self.streams)

    def get(self, path):
        entry = self.streams.get(path)
        if entry is None:
            return None
        start, size = entry
        return self._read_mini(start, size) if size < self.cutoff else self._read_chain(start, size)


def unpack_segment(raw):
    """RSeStorage-Daten-Stream (B…): 16 Byte GUID, u16 mit Packart im oberen Byte (1 zlib, 2 Zstandard), dann Daten.
    -> entpackte Bytes oder None"""
    if raw is None or len(raw) < 20:
        return None
    try:
        if raw[17] == 2:
            return inventor_zstd.decompress(raw[18:], MAX_UNPACKED)
        if raw[18] == 0x78:
            return zlib.decompressobj().decompress(raw[18:], MAX_UNPACKED)
    except (zlib.error, inventor_zstd.ZstdError):
        return None
    return None


def _bunzip(raw):
    out = bz2.BZ2Decompressor().decompress(raw, MAX_UNPACKED)
    if len(out) >= MAX_UNPACKED:
        raise InventorError("Inventor graphics cache too large")
    return out


# ---------- Bezeichner: u32 Zeichenanzahl + UTF-16LE (nur ASCII) ----------

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_:]*\Z")
_PTR = re.compile(r"[0-9A-F]{16}\Z")
_TOKEN_START = re.compile(rb"[\x03-\x40]\x00\x00\x00(?=[\x20-\x7e]\x00)")
_U16_ASCII = re.compile(rb"(?:[\x20-\x7e]\x00)+")


def _token_at(b, p, max_len=64):
    if p + 4 > len(b):
        return None
    n = int.from_bytes(b[p:p + 4], "little")
    end = p + 4 + n * 2
    if n < 1 or n > max_len or end > len(b):
        return None
    raw = b[p + 4:end]
    if not _U16_ASCII.fullmatch(raw):
        return None
    return raw.decode("utf-16-le"), end


def scan_tokens(b, start, stop):
    """Bezeichner und Zeiger-IDs im serialisierten Szenengraph, in Dateireihenfolge: [(pos, text, ende)]"""
    out, last = [], start
    for m in _TOKEN_START.finditer(b, start, stop):
        p = m.start()
        if p < last:
            continue
        t = _token_at(b, p)
        if t and (_IDENT.match(t[0]) or _PTR.match(t[0])):
            out.append((p, t[0], t[1]))
            last = t[1]
    return out


def _u32(b, o):
    return int.from_bytes(b[o:o + 4], "little")


# ---------- Szene (OGS-Cache) ----------

def parse_scene(d):
    """Knoten je Körper (Vorkommen-Pfad + Teilnetze mit Matrix), Netze und Materialien"""
    blocks, p = {}, 0
    # Folge benannter Blöcke: u32 Namenslänge in BYTES, UTF-16-Name, u32 Größe, u32 0, Daten.
    # 2023: stream_mesh_000, world – 2025 zusätzlich Texturen (…_tmp_dds) davor.
    while p + 8 <= len(d):
        nb = _u32(d, p)
        if nb == 0 or nb > 1024 or nb % 2 or p + 4 + nb + 8 > len(d):
            break
        name = d[p + 4:p + 4 + nb].decode("utf-16-le", "replace")
        size, start = _u32(d, p + 4 + nb), p + 4 + nb + 8
        if start + size > len(d):
            raise InventorError(f"Inventor graphics cache: block '{name}' truncated")
        blocks[name] = (start, size)
        p = start + size
    if "stream_mesh_000" not in blocks or "world" not in blocks:
        raise InventorError("Unknown Inventor graphics cache layout (mesh or scene block missing)")
    blob_start, blob_size = blocks["stream_mesh_000"]
    blob = d[blob_start:blob_start + blob_size]
    w_start, w_size = blocks["world"]
    toks = scan_tokens(d, w_start, w_start + w_size)

    def f32s(o, n):
        return struct.unpack_from(f"<{n}f", d, o)

    nodes, meshes, mats, vf_attrs = [], {}, {}, {}
    node = sub = mesh = mat = None
    section, wrappers = "faces", 0
    for i, (_, s, end) in enumerate(toks):
        ptr = toks[i + 1] if i + 1 < len(toks) and _PTR.match(toks[i + 1][1]) else None
        after = ptr[2] if ptr else end
        if s == "OGSInventorRenderNode":
            node = {"path": "", "items": []}
            nodes.append(node)
            wrappers, section = 0, "faces"
        elif s == "SMxCSNBIWrapper":
            # 2. Wrapper hinter dem Render-Knoten trägt den Vorkommen-Pfad ("17", "1:1:3:1")
            wrappers += 1
            if node and wrappers == 2 and not node["path"]:
                n = _u32(d, after)
                if 0 < n < 200 and _u32(d, after + 4) == n:
                    path = d[after + 8:after + 8 + n * 2].decode("utf-16-le", "replace")
                    if re.fullmatch(r"[0-9:]+", path):
                        node["path"] = path
        elif s == "InvLODFacesStart":
            section = "faces"
        elif s == "InvLODEdgesStart":
            section = "edges"
        elif s == "OGSBridge::SubRenderNode":
            sub = f32s(after + 1, 16)               # 4x4 spaltenweise, Verschiebung in cm
        elif s == "SimpleMesh":
            if ptr:
                mesh = ptr[1]
                meshes.setdefault(mesh, {"kind": section})
                if node and section == "faces":
                    node["items"].append({"mesh": mesh, "matrix": sub, "mat": None})
        elif s == "VertexFormat":
            # Definition (nur beim ersten Auftreten): u32 Attributanzahl, u32 2, …
            if ptr and ptr[1] not in vf_attrs:
                n = _u32(d, after)
                if 1 <= n <= 8 and _u32(d, after + 4) == 2:
                    vf_attrs[ptr[1]] = n
        elif s == "VertexBuffer":
            # VertexBuffer -> VertexFormat-Verweis -> u32 1, u32 0, u32 Offset, u32 Größe, u32 1
            if mesh and i + 3 < len(toks) and toks[i + 2][1] == "VertexFormat" and _PTR.match(toks[i + 3][1]):
                vf_end = toks[i + 3][2]
                m = meshes[mesh]
                m["v_off"], m["v_size"] = _u32(d, vf_end + 8), _u32(d, vf_end + 12)
                m["attrs"] = vf_attrs.get(toks[i + 3][1])
        elif s == "AIndexBuffer":
            if mesh and ptr:
                m = meshes[mesh]
                m["i_off"], m["i_size"], m["i_type"] = _u32(d, after + 5), _u32(d, after + 9), _u32(d, after + 13)
        elif s in ("AdvancedMaterialEffectInstance", "InventorAdvancedEffectInstance"):
            mat = ptr[1] if ptr else None
            if mat:
                mats.setdefault(mat, {"color": None, "metal": False})
            if node and section == "faces" and node["items"]:
                node["items"][-1]["mat"] = mat
        elif s == "DiffuseColor":
            if mat in mats and mats[mat]["color"] is None:
                mats[mat]["color"] = tuple(min(max(c, 0.0), 1.0) for c in f32s(end + 8, 3))
        elif mat in mats and s.startswith("AME_") and "_Metal_" in s:
            mats[mat]["metal"] = True

    for m in meshes.values():
        _read_mesh(m, blob)
    return nodes, meshes, mats


def _read_mesh(m, blob):
    """Position (mm), Normale und Dreiecksindizes aus dem Datenblock; bei Fehlern m['error']"""
    if m["kind"] != "faces" or m.get("v_size") is None or m.get("i_size") is None:
        return
    if m["v_off"] + m["v_size"] > len(blob) or m["i_off"] + m["i_size"] > len(blob):
        m["error"] = "buffer out of range"
        return
    wide = m["i_type"] == 2
    index = np.frombuffer(blob, dtype="<u4" if wide else "<u2", count=m["i_size"] // (4 if wide else 2), offset=m["i_off"])
    # Vertex-Größe: 1 Attribut = 12 Byte (Kanten), 3 = Position, Normale, UV (32 Byte); sonst aus dem größten Index
    stride = {1: 12, 3: 32}.get(m.get("attrs"))
    if not stride and len(index):
        stride = m["v_size"] / (int(index.max()) + 1)
    if not stride or stride != int(stride) or stride < 24 or stride % 4 or m["v_size"] % stride:
        m["error"] = f"vertex format {m.get('attrs')}/{stride}"
        return
    stride = int(stride)
    nv = m["v_size"] // stride
    raw = np.frombuffer(blob, dtype="<f4", count=m["v_size"] // 4, offset=m["v_off"]).reshape(nv, stride // 4)
    if len(index) % 3 or (len(index) and int(index.max()) >= nv):
        m["error"] = "invalid indices"
        return
    m["position"] = raw[:, 0:3].astype(np.float64) * CM_TO_MM
    m["normal"] = raw[:, 3:6].astype(np.float64)
    m["index"] = index.astype(np.int64).reshape(-1, 3)


# ---------- Namen (Modellbrowser in der RSe-Datenbank) ----------

def _read_utf16_before(b, end):
    """UTF-16-Text, der direkt vor 'end' endet; Zeichenanzahl steht als u32 davor"""
    for n in range(1, 260):
        p = end - n * 2 - 4
        if p < 0:
            break
        if _u32(b, p) == n:
            return b[p + 4:p + 4 + n * 2].decode("utf-16-le", "replace"), p
    return None


_ID_MARK = re.compile(rb"[^\x00]\x01\x00\x00\x00\x00\x00\x00..\x02\x00\x00\x30", re.S)


def parse_browser(b):
    """Vorkommen der obersten Ebene: [{name, assembly, id}] in Dateireihenfolge"""
    out = []
    for kind in ("AmPartInstEntry", "AmAssmInstEntry"):
        needle = kind.encode("utf-16-le")
        p = b.find(needle)
        while p >= 0:
            found = _read_utf16_before(b, p - 4)
            if found:
                name, start = found
                # Objekt-ID: <Kennung> 01 00×6 <u16 id> 02 00 00 30, letztes Vorkommen vor dem Namen
                # Kennung wechselt je Speicherstand (gesehen 0x0A–0x0E), daher beliebig außer 0
                ident = -1
                lo = max(0, start - 600)
                for m in _ID_MARK.finditer(b, lo, start):
                    ident = int.from_bytes(b[m.start() + 8:m.start() + 10], "little")
                out.append({"name": name, "assembly": kind == "AmAssmInstEntry", "id": ident, "at": p})
            p = b.find(needle, p + len(needle))
    return sorted(out, key=lambda e: e["at"])


_LW_PATH = re.compile(rb"([\x01-\x40])\x00\x00\x00((?:[\x30-\x3a]\x00)+)")


def lw_paths(b):
    """Vorkommen-Pfade in LWUFRx ("7", "1:1:3:1"), auch ausgeblendete ohne Grafik"""
    out = []
    for m in _LW_PATH.finditer(b):
        n = m.group(1)[0]
        text = m.group(2).decode("utf-16-le")
        if len(text) < n:
            continue
        text = text[:n]
        if text[0] != ":" and text[-1] != ":" and "::" not in text:
            out.append(text)
    return out


# ---------- Gesamt ----------

def read(path):
    """Liest eine .iam: dict(nodes, meshes, mats, names, names_ok)"""
    with open(path, "rb") as f:
        data = f.read()
    cfb = Cfb(data)
    ogs = cfb.get(OGS_PATH)
    if ogs is None:
        paths = cfb.paths()
        if any(p.startswith("RSeStorage/") for p in paths) and not any(p.startswith("CacheGraphics/") for p in paths):
            raise PartFileError("This is an Inventor part (.ipt), not an assembly")
        raise InventorError("No graphics cache found in the Inventor file")
    nodes, meshes, mats = parse_scene(_bunzip(ogs))

    # Browser-Stream suchen (18 Byte Kopf + zlib/Zstandard)
    entries = []
    marker = "AmBrowserArrangement".encode("utf-16-le")
    for p in cfb.paths():
        if not re.fullmatch(r"RSeStorage/B[a-z0-9]+", p):
            continue
        dec = unpack_segment(cfb.get(p))
        if dec is None:
            continue
        if marker in dec:
            entries = parse_browser(dec)
            break

    def top_of(p):
        return p.split(":")[0]

    scene_tops = sorted({top_of(n["path"]) for n in nodes if n["path"]}, key=int)
    tops = scene_tops
    lw = cfb.get(LWUFRX_PATH)
    if lw is not None:
        try:
            lw_tops = sorted({top_of(p) for p in lw_paths(_bunzip(lw))}, key=int)
            if all(t in lw_tops for t in scene_tops):
                tops = lw_tops
        except (OSError, ValueError, InventorError):
            pass                # ohne LWUFRx weiter mit den Pfaden der Szene
    # Zuordnung: Pfadnummern aufsteigend <-> Browser-Einträge nach Objekt-ID aufsteigend (= Einfügereihenfolge)
    by_id = sorted((e for e in entries if e["id"] >= 0), key=lambda e: e["id"])
    ordered = None
    if len(by_id) == len(entries) == len(tops):
        ordered = by_id
    elif len(entries) == 1 and len(tops) == 1:
        ordered = entries
    names = {t: (ordered[i] if ordered else {"name": f"Component {i + 1}", "assembly": False}) for i, t in enumerate(tops)}
    return {"nodes": nodes, "meshes": meshes, "mats": mats, "names": names, "names_ok": ordered is not None}


def matrix_rows(values):
    """16 Floats spaltenweise (cm) -> 4x4-Zeilenliste in mm"""
    m = np.array(values, dtype=np.float64).reshape(4, 4).T
    m[:3, 3] *= CM_TO_MM
    return m


def body_mesh(items, meshes):
    """Teilnetze eines Körpers zu einem Netz relativ zur ersten Platzierung zusammenführen.
    Gleiche Positionen werden verschweißt (Inventor dupliziert Ecken an Kanten), die Normalen bleiben je Ecke.
    Rückgabe: (verts mm, tris, loop_normals, mat_index je Dreieck, base 4x4) oder None"""
    items = [it for it in items if "position" in meshes.get(it["mesh"], {})]
    if not items:
        return None
    base = matrix_rows(items[0]["matrix"]) if items[0]["matrix"] else np.eye(4)
    inv = np.linalg.inv(base)
    pos, nor, tris, mat_idx, offset = [], [], [], [], 0
    for k, it in enumerate(items):
        m = meshes[it["mesh"]]
        p, n = m["position"], m["normal"]
        if it["matrix"] and k:
            rel = inv @ matrix_rows(it["matrix"])
            if not np.allclose(rel, np.eye(4)):
                p = p @ rel[:3, :3].T + rel[:3, 3]
                rot = np.linalg.inv(rel[:3, :3]).T
                n = n @ rot.T
                n /= np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)
        pos.append(p)
        nor.append(n)
        tris.append(m["index"] + offset)
        mat_idx.append(np.full(len(m["index"]), k, dtype=np.int32))
        offset += len(p)
    pos, nor, tris, mat_idx = np.concatenate(pos), np.concatenate(nor), np.concatenate(tris), np.concatenate(mat_idx)
    loop_normals = nor[tris.ravel()]
    # Verschweißen: exakt gleiche Koordinaten (Inventor schreibt Kanten-Duplikate bitgleich)
    key = np.ascontiguousarray(pos.astype(np.float32)).view(np.dtype((np.void, 12))).ravel()
    _, first, inverse = np.unique(key, return_index=True, return_inverse=True)
    verts = pos[first]
    tris = inverse.ravel()[tris]
    keep = (tris[:, 0] != tris[:, 1]) & (tris[:, 1] != tris[:, 2]) & (tris[:, 0] != tris[:, 2])
    loop_normals = loop_normals.reshape(-1, 3, 3)[keep].reshape(-1, 3)
    return verts, tris[keep], loop_normals, mat_idx[keep], base

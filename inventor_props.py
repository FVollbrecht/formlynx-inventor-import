# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""iProperties aus Inventor-Dateien (.ipt/.iam) lesen, ohne bpy.

Die iProperties liegen als OLE-Property-Sets in eigenen Streams der Datei (Kopf FE FF). Jeder Satz trägt seinen Namen
als Eigenschaft „Property Set Name“. Gelesen werden nur die Sätze, die Inventor im iProperties-Dialog zeigt;
interne Sätze (_iLogic, _Private …) bleiben außen vor.
Feldnummern der Design Tracking Properties nach der Inventor-API, nicht alle an Dateien geprüft.
Physikalische Werte stehen in g, cm², cm³, g/cm³ in der Datei (an Testteilen: Masse = Dichte × Volumen).
"""
import datetime
import re
import struct

from .inventor_data import Cfb, InventorError

SUMMARY = {2: "Title", 3: "Subject", 4: "Author", 5: "Keywords", 6: "Comments", 9: "Revision Number"}
DOC_SUMMARY = {2: "Category", 14: "Manager", 15: "Company"}
DESIGN_TRACKING = {
    4: "Creation Date", 5: "Part Number", 7: "Project", 9: "Cost Center", 10: "Checked By", 11: "Date Checked",
    12: "Eng. Approved By", 13: "Eng. Approved Date", 17: "User Status", 20: "Material", 23: "Web Link",
    29: "Description", 30: "Vendor", 34: "Mfg. Approved By", 35: "Mfg. Approved Date", 36: "Estimated Cost",
    40: "Design State", 41: "Designer", 42: "Engineer", 43: "Authority", 48: "Manufacturer", 55: "Stock Number",
    56: "Categories", 72: "Appearance",
}
# Physikalisch: Dateiwert -> Anzeige in mm/kg
PHYSICAL = {58: ("Mass (kg)", 1e-3), 59: ("Area (mm²)", 100.0), 60: ("Volume (mm³)", 1000.0),
            61: ("Density (g/cm³)", 1.0)}
SETS = {"Inventor Summary Information": SUMMARY, "Inventor Document Summary Information": DOC_SUMMARY,
        "Design Tracking Properties": DESIGN_TRACKING}
USER_SET = "Inventor User Defined Properties"
DESIGN_STATES = {1: "Work In Progress", 2: "Pending", 3: "Released"}
MAX_KEY = 63                     # Blender: Länge eines Custom-Property-Namens


def _value(b, p, cp):
    """Wert an Offset p (VT-Kopf) -> Python-Wert oder None (nicht darstellbar)"""
    vt = struct.unpack_from("<H", b, p)[0]
    p += 4
    if vt == 0x1E:                                           # VT_LPSTR
        n = struct.unpack_from("<I", b, p)[0]
        return b[p + 4:p + 4 + n].decode("utf-8" if cp == 65001 else "cp1252", "replace").rstrip("\0")
    if vt == 0x1F:                                           # VT_LPWSTR
        n = struct.unpack_from("<I", b, p)[0]
        return b[p + 4:p + 4 + n * 2].decode("utf-16le", "replace").rstrip("\0")
    if vt in (2, 3, 0x13):                                   # I2, I4, UI4
        return struct.unpack_from({2: "<h", 3: "<i", 0x13: "<I"}[vt], b, p)[0]
    if vt == 5:                                              # R8
        return struct.unpack_from("<d", b, p)[0]
    if vt == 11:                                             # BOOL
        return struct.unpack_from("<h", b, p)[0] != 0
    if vt == 0x40:                                           # FILETIME
        ft = struct.unpack_from("<Q", b, p)[0]
        if not ft:
            return None
        try:
            return (datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=ft // 10)).date().isoformat()
        except OverflowError:
            return None
    return None


def property_set(b):
    """Ein Property-Set-Stream -> {Feldnummer oder Name: Wert}; nicht lesbare Felder fehlen"""
    out = {}
    if len(b) < 48 or b[:2] != b"\xfe\xff":
        return out
    try:
        off = struct.unpack_from("<I", b, 44)[0]             # erster Abschnitt
        _, count = struct.unpack_from("<II", b, off)
        ids = [struct.unpack_from("<II", b, off + 8 + i * 8) for i in range(min(count, 4096))]
        cp = 1252
        for pid, po in ids:
            if pid == 1:
                cp = struct.unpack_from("<H", b, off + po + 4)[0]
        names = {}
        for pid, po in ids:
            if pid != 0:
                continue
            p = off + po
            n = struct.unpack_from("<I", b, p)[0]
            p += 4
            for _ in range(min(n, 4096)):
                key, size = struct.unpack_from("<II", b, p)
                p += 8
                if cp == 1200:
                    names[key] = b[p:p + size * 2].decode("utf-16le", "replace").rstrip("\0")
                    p += size * 2
                    p += (-p) % 4
                else:
                    names[key] = b[p:p + size].decode("cp1252", "replace").rstrip("\0")
                    p += size
        for pid, po in ids:
            if pid in (0, 1) or pid >= 0x80000000:
                continue
            value = _value(b, off + po, cp)
            if value is not None:
                out[names.get(pid, pid)] = value
    except struct.error:
        pass                                                  # abgeschnittener Satz: was gelesen ist, bleibt
    return out


def read_cfb(cfb):
    """iProperties einer geöffneten Datei -> {Anzeigename: Wert} (leere Werte weggelassen)"""
    sets = {}
    for path in cfb.paths():
        data = cfb.get(path) or b""
        if data[:2] == b"\xfe\xff":
            props = property_set(data)
            name = props.pop("Property Set Name", None)
            if isinstance(name, str):
                sets[name] = props
    out = {}
    for set_name, fields in SETS.items():
        props = sets.get(set_name, {})
        for pid, label in fields.items():
            value = props.get(pid)
            if label == "Design State":
                value = DESIGN_STATES.get(value)
            if value not in (None, ""):
                out[label] = value.strip() if isinstance(value, str) else value
    tracking = sets.get("Design Tracking Properties", {})
    if any(isinstance(tracking.get(pid), float) and tracking[pid] > 0 for pid in PHYSICAL):
        for pid, (label, factor) in PHYSICAL.items():
            value = tracking.get(pid)
            if isinstance(value, float):
                out[label] = value * factor
    for name, value in sets.get(USER_SET, {}).items():
        if isinstance(name, str) and value not in (None, ""):
            key = name if name not in out else f"{name} (Custom)"
            out[key[:MAX_KEY]] = value
    out.update(parameters(cfb))
    return out


def parameters(cfb):
    """Inventor-Parameter (Modell, Benutzer, Blech) als Custom-Property-Gruppen; leer, wenn unlesbar"""
    from . import inventor_params        # spät: inventor_params -> inventor_joints -> inventor_data
    try:
        params = inventor_params.read_cfb(cfb)
    except (InventorError, struct.error, ValueError, IndexError):
        return {}
    groups = inventor_params.as_props(params)
    return {key: {name[:MAX_KEY]: value for name, value in group.items()} for key, group in groups.items()}


def read(path):
    """iProperties einer .ipt/.iam -> {Anzeigename: Wert}"""
    with open(path, "rb") as f:
        return read_cfb(Cfb(f.read()))


_REF = re.compile(rb"(?:[\x20-\x7e]\x00){3,259}\.\x00(?:[iI]\x00[pP]\x00[tT]\x00|[iI]\x00[aA]\x00[mM]\x00)")


def referenced_files(cfb):
    """Gespeicherte Pfade der Bauteile/Unterbaugruppen einer .iam (Stream UFRxDoc) -> [Pfad]"""
    data = cfb.get("UFRxDoc") or b""
    seen, out = set(), []
    for m in _REF.finditer(data):
        path = m.group(0).decode("utf-16le")
        if path.lower() not in seen:
            seen.add(path.lower())
            out.append(path)
    return out

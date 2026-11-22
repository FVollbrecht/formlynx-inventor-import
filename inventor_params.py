# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Inventor-Parameter (Modell-, Benutzer- und Blechparameter) aus .ipt/.iam lesen – ohne bpy.

Parameter-Knoten 90874D26 im DC-Segment: Name (u32 Zeichen + UTF-16), 4 Byte, Verweis auf die Einheit (F8A779FD),
Verweis auf die Formel, Nennwert, Modellwert (intern cm bzw. rad). Formelknoten: Wert F8A77A04, Verweis F8A77A05,
Operatoren F8A77A06…0B, Minus F8A77A0C, Konstante 0AA8AF46. Merker im Kopf: Bit 0x08 = intern (RDxVar, SeamGap),
fehlt auch in Inventors Parameter-Export. Abgeglichen mit testdaten/inventor/ipt/blech/Bauteil5-params.xml.
"""
import math
import re
import struct

from .inventor_joints import segment_nodes

T_PARAMETER = "90874D26"
T_UNIT = "F8A779FD"
T_VALUE = "F8A77A04"
T_REF = "F8A77A05"
T_CONSTANT = "0AA8AF46"
OPERATORS = {"F8A77A06": "+", "F8A77A07": "-", "F8A77A08": "*", "F8A77A09": "/", "F8A77A0A": "%", "F8A77A0B": "^"}
T_MINUS = "F8A77A0C"
SEGMENTS = ("PmDCSegment", "AmDcSegment")
HIDDEN = 0x08

# Einheiten-Knoten -> (Kürzel, Faktor intern -> Anzeige); intern: Länge cm, Winkel rad
UNITS = {
    "624120BC": ("mm", 10.0), "F8A779F5": ("m", 0.01), "F8A779F6": ("in", 1 / 2.54), "F8A779F7": ("ft", 1 / 30.48),
    "5C30CDF2": ("rad", 1.0), "5C30CDF0": ("deg", 180 / math.pi), "5C30CDF6": ("deg", 180 / math.pi),
    "5F9D0023": ("", 1.0),
}
_NAME = re.compile(rb"([\x01-\x40])\x00\x00\x00((?:[\x20-\x7e\xa0-\xff]\x00)+)", re.S)


def _u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def _name(b):
    """(Name, Position des Längenfelds) des Parameters"""
    for m in _NAME.finditer(b):
        n = m.group(1)[0]
        if len(m.group(2)) >= 2 * n:
            raw = b[m.start(2):m.start(2) + 2 * n]
            try:
                return raw.decode("utf-16-le"), m.start()
            except UnicodeDecodeError:
                continue
    return None, -1


class _Reader:
    def __init__(self, nodes):
        self.nodes = nodes
        self.names = {}

    def typ(self, i):
        return self.nodes[i - 1][0] if 0 < i <= len(self.nodes) else ""

    def data(self, i):
        return self.nodes[i - 1][1]

    def unit(self, i):
        """Einheiten-Knoten -> (Kürzel, Faktor); Zähler/Nenner: Liste 03 00 00 30, Anzahl, 0x0E, Verweise"""
        if self.typ(i) != T_UNIT:
            return ("", 1.0)
        b = self.data(i)
        k = b.find(b"\x03\x00\x00\x30")
        if k < 0:
            return ("", 1.0)
        num = [_u32(b, k + 12 + 4 * j) & 0xFFFF for j in range(min(_u32(b, k + 4), 4))]
        k2 = k + 12 + 4 * len(num)
        den = []
        if b[k2:k2 + 4] == b"\x03\x00\x00\x30":
            den = [_u32(b, k2 + 12 + 4 * j) & 0xFFFF for j in range(min(_u32(b, k2 + 4), 4))]
        if len(num) == 1 and not den:
            return UNITS.get(self.typ(num[0]), ("", 1.0))
        if not num and not den:
            return ("", 1.0)
        parts = [UNITS.get(self.typ(x), ("?", 1.0)) for x in num]
        below = [UNITS.get(self.typ(x), ("?", 1.0)) for x in den]
        text = "*".join(p[0] for p in parts) + ("/" + "/".join(p[0] for p in below) if below else "")
        factor = math.prod(p[1] for p in parts) / math.prod(p[1] for p in below)
        return (text, factor)

    def expression(self, i, unit=("", 1.0), depth=0):
        """Formelbaum als Text; Zahlen mit Einheit in der Einheit des Parameters, Faktoren ohne Einheit"""
        if depth > 64:
            return "?"
        t, b = self.typ(i), self.data(i) if 0 < i <= len(self.nodes) else b""
        if t == T_VALUE and len(b) >= 18:
            own, own_factor = self.unit(_u32(b, 6))
            value = struct.unpack_from("<d", b, 10)[0]
            if not own:
                return f"{value:.10g}"
            text, factor = unit if unit[0] else (own, own_factor)
            return f"{value * factor:.10g} {text}"
        if t == T_REF and len(b) >= 14:
            ref = _u32(b, 10)
            return self.names.get(ref & 0xFFFF, "?") if ref & 0x80000000 else "?"
        if t in OPERATORS and len(b) >= 18:
            left = self.expression(_u32(b, 10), unit, depth + 1)
            right = self.expression(_u32(b, 14), unit, depth + 1)
            return f"({left} {OPERATORS[t]} {right})" if depth else f"{left} {OPERATORS[t]} {right}"
        if t == T_MINUS and len(b) >= 14:
            return "-" + self.expression(_u32(b, 10), unit, depth + 1)
        if t == T_CONSTANT:
            name, _p = _name(b)
            return name or "?"
        return "?"


def read_cfb(cfb):
    """-> [dict(name, value, unit, expression, formula)] in Dateireihenfolge, ohne interne Parameter.
    value in der Anzeigeeinheit (mm, deg, …); formula True, wenn der Wert aus einer Formel/einem Verweis kommt."""
    segs = segment_nodes(cfb, SEGMENTS)
    nodes = next((segs[s] for s in SEGMENTS if s in segs), None)
    if not nodes:
        return []
    reader = _Reader(nodes)
    found = []
    for i, (t, b) in enumerate(nodes, 1):
        if t != T_PARAMETER:
            continue
        name, p = _name(b)
        if not name or p < 20:
            continue
        end = p + 4 + 2 * len(name.encode("utf-16-le")) // 2
        if end + 28 > len(b):
            continue
        flags = b[p - 21] if b[p - 23] == 0x42 else 0      # 42 <Art> <Merker> vor 20 Byte Kopf
        unit_ref, formula_ref = _u32(b, end + 4), _u32(b, end + 8)
        value = struct.unpack_from("<d", b, end + 20)[0]
        reader.names[i] = name
        found.append((i, name, flags, unit_ref, formula_ref, value))
    out, seen = [], set()
    for i, name, flags, unit_ref, formula_ref, value in found:
        if flags & HIDDEN or name in seen:
            continue                    # interne Größen; Blechregel-Kopien weiter hinten
        seen.add(name)
        unit, factor = reader.unit(unit_ref)
        expression = reader.expression(formula_ref, (unit, factor))
        out.append(dict(name=name, value=value * factor, unit=unit, expression=expression,
                        formula=reader.typ(formula_ref) != T_VALUE))
    return out


def as_props(params):
    """Custom-Property-Gruppen: Werte (Anzeigeeinheit) und Formeln (nur wo der Wert aus einer Formel kommt)"""
    if not params:
        return {}
    values = {p["name"]: round(p["value"], 9) for p in params}
    formulas = {p["name"]: p["expression"] for p in params if p["formula"]}
    units = {p["name"]: p["unit"] for p in params if p["unit"]}
    out = {"Inventor Parameters": values, "Inventor Parameter Units": units}
    if formulas:
        out["Inventor Parameter Formulas"] = formulas
    return out

# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Ersatz für MODULYNX geometry/i18n: Einheiten und Übersetzungen (Teilkatalog aus locale/)."""
import os

import bpy
from bpy.app import translations as _t

CTX = "FORMLYNX_INV"
CONTEXTS = (CTX, "*", "Operator")
LOCALE_DIR = os.path.join(os.path.dirname(__file__), "locale")


def mm_to_bu(scene, mm):
    us = scene.unit_settings
    scale = us.scale_length if us.system != 'NONE' and us.scale_length > 0 else 1.0
    return mm * 0.001 / scale


def rpt_(msgid):
    """Meldung übersetzen."""
    return _t.pgettext_rpt(msgid, CTX)


def _unquote(text):
    return text[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")


def parse_po(path):
    """{(msgctxt, msgid): msgstr} – nur einfache Einträge, wie sie der Build schreibt."""
    entries, fields, key = {}, {}, None
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines() + [""]
    for line in lines:
        line = line.strip()
        if not line:
            if fields.get("msgid") and fields.get("msgstr"):
                entries[(fields.get("msgctxt"), fields["msgid"])] = fields["msgstr"]
            fields, key = {}, None
        elif line.startswith("#"):
            continue
        elif line.startswith('"') and key:
            fields[key] += _unquote(line)
        else:
            key, _, value = line.partition(" ")
            fields[key] = _unquote(value)
    return entries


def load_translations():
    out = {}
    if not os.path.isdir(LOCALE_DIR):
        return out
    for name in sorted(os.listdir(LOCALE_DIR)):
        if name.endswith(".po"):
            table = {}
            for (ctxt, msgid), msgstr in parse_po(os.path.join(LOCALE_DIR, name)).items():
                for c in ((ctxt,) if ctxt else CONTEXTS):
                    table[(c, msgid)] = msgstr
            out[name[:-3]] = table
    return out


def register():
    bpy.app.translations.register(__package__, load_translations())


def unregister():
    bpy.app.translations.unregister(__package__)

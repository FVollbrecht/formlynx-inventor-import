# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""FORMLYNX Inventor Import – freie Extension, gebaut aus MODULYNX (tools/build_inventor_ext.py).

Quelle ist modulynx/inventor_import.py + inventor_data.py + inventor_part.py; hier nicht von Hand ändern, nur die Vorlage in
modulynx/tools/inventor_ext/.
"""
import bpy

from . import _compat, inventor_import


def register():
    _compat.register()
    for cls in inventor_import.classes:
        cls.bl_translation_context = _compat.CTX
        bpy.utils.register_class(cls)
    inventor_import.register()


def unregister():
    inventor_import.unregister()
    for cls in reversed(inventor_import.classes):
        bpy.utils.unregister_class(cls)
    _compat.unregister()

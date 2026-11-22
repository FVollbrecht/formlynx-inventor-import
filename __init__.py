# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""FORMLYNX Inventor Import – Autodesk-Inventor-Baugruppen (.iam) und -Bauteile (.ipt) als Meshes, ohne Inventor.

Quelle ist dieses Repo. Tests: tests/test_inventor.py (t.sh inventor), Paket: tools/build.py.
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

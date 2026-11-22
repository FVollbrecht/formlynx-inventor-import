# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Live-Lösung der Schleifen in Inventor-Rigs: Handler und Bake-Operator.

Das Modell (inventor_solver) liegt als JSON an der Armature. Ändert der Nutzer (oder eine Animation) den Wert eines
Gelenks, das an einer Schleife hängt, werden die übrigen Gelenkwerte der Schleife neu gelöst und zurückgeschrieben.
"""
import json
import math

import bpy
import numpy as np
from bpy.app.handlers import persistent
from mathutils import Quaternion, Vector

from . import inventor_solver
from ._compat import mm_to_bu, rpt_

EPS = 1e-9
STEP_ANGLE = math.radians(30.0)         # größter Schritt je Lösung (Winkel)
STEP_LENGTH = 0.1                       # größter Schritt je Lösung, Anteil von M.length

_models = {}        # as_pointer -> (Hash des JSON, Model)
_last = {}          # as_pointer -> zuletzt geschriebene/gesehene Werte
_busy = False


def _model(obj):
    text = obj.get("inventor_solver")
    if not text:
        return None
    key, h = obj.as_pointer(), hash(text)
    cached = _models.get(key)
    if cached is None or cached[0] != h:
        cached = (h, inventor_solver.Model(json.loads(text)))
        _models[key] = cached
        _last.pop(key, None)
    return cached[1]


def _rotvec(pb):
    axis, ang = pb.rotation_quaternion.normalized().to_axis_angle()
    if ang > math.pi:
        ang -= 2 * math.pi
    return axis * ang


def _read(obj, M):
    q = np.zeros(len(M.vars))
    for n, (i, slot) in enumerate(M.vars):
        pb = obj.pose.bones.get(M.bones[i]["name"])
        if pb is None:
            continue
        if slot == 'ry':
            q[n] = pb.rotation_euler[1]
        elif slot[0] == 'l':
            q[n] = pb.location["xyz".index(slot[1])]
        else:
            q[n] = _rotvec(pb)["xyz".index(slot[1])]
    return q


def _write(obj, M, q, cur):
    """Nur geänderte Werte schreiben (vermeidet unnötige Updates)"""
    quats = {}
    for n, (i, slot) in enumerate(M.vars):
        if abs(q[n] - cur[n]) <= EPS:
            continue
        pb = obj.pose.bones.get(M.bones[i]["name"])
        if pb is None:
            continue
        if slot == 'ry':
            pb.rotation_euler[1] = q[n]
        elif slot[0] == 'l':
            pb.location["xyz".index(slot[1])] = q[n]
        else:
            quats.setdefault(i, []).append(n)
    for i, _ns in quats.items():
        pb = obj.pose.bones[M.bones[i]["name"]]
        v = Vector([q[M.index[(i, s)]] if (i, s) in M.index else 0.0 for s in ('qx', 'qy', 'qz')])
        pb.rotation_quaternion = Quaternion(v.normalized(), v.length) if v.length > 1e-12 else Quaternion()


def _set_pitch(obj, M, scene):
    scale = mm_to_bu(scene, 1.0)
    for i, b in enumerate(M.bones):
        if b["screw"] >= 0:
            src = obj.pose.bones.get(M.bones[b["screw"]]["name"])
            M.pitch[i] = (src.get("Inventor pitch mm", 0.0) if src else 0.0) * scale / (2 * math.pi)


def _solve(obj, scene, M, q_new, q_old):
    """Geänderte (held) Werte in Schritten ansteuern, den Rest der Schleife lösen; None wenn nichts zu tun"""
    held = np.abs(q_new - q_old) > EPS
    if not (held & M.involved).any():
        return None
    _set_pitch(obj, M, scene)
    steps = 1
    for n in np.flatnonzero(held):
        lim = STEP_ANGLE if M.vars[n][1] in inventor_solver.ANGULAR else STEP_LENGTH * M.length
        steps = max(steps, int(math.ceil(abs(q_new[n] - q_old[n]) / lim - 1e-9)))
    q = q_old.copy()
    for k in range(1, steps + 1):
        q[held] = q_old[held] + (q_new[held] - q_old[held]) * (k / steps)
        q, _cost = M.solve(q, held)
    q[held] = q_new[held]
    return q


def _process(obj, scene, first_solve):
    M = _model(obj)
    if M is None or not obj.get("Inventor solver live", True):
        return False
    key = obj.as_pointer()
    cur = _read(obj, M)
    old = _last.get(key)
    if old is None and first_solve:
        old = np.zeros(len(cur))                # Ruhelage = alle Werte 0
    if old is None or len(old) != len(cur):
        _last[key] = cur
        return False
    q = _solve(obj, scene, M, cur, old)
    if q is None:
        _last[key] = cur
        return False
    _write(obj, M, q, cur)
    _last[key] = _read(obj, M)
    return True


def solve_rig(obj, scene):
    """Rig sofort lösen (Werte seit dem letzten Stand gelten als gesetzt, ohne Stand die Ruhelage als Bezug)"""
    global _busy
    if _busy:
        return False
    _busy = True
    try:
        return _process(obj, scene, True)
    finally:
        _busy = False


def _run(scene):
    global _busy
    if _busy:
        return
    _busy = True
    try:
        for obj in scene.objects:
            if obj.type == 'ARMATURE' and "inventor_solver" in obj:
                _process(obj, scene, False)
    finally:
        _busy = False


@persistent
def _on_depsgraph(scene, *_args):
    _run(scene)


@persistent
def _on_frame(scene, *_args):
    _run(scene)


@persistent
def _on_load(*_args):
    _models.clear()
    _last.clear()


class IVX_OT_bake_inventor_rig(bpy.types.Operator):
    bl_idname = "ivx.bake_inventor_rig"
    bl_label = "Bake Inventor Rig"
    bl_description = "Solve the loops of the active Inventor rig for every frame of the scene range and key all joint values"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.object
        return o is not None and o.type == 'ARMATURE' and "inventor_solver" in o

    def execute(self, context):
        scene, obj = context.scene, context.object
        M = _model(obj)
        if M is None:
            self.report({'ERROR'}, rpt_("No loop model on this rig"))
            return {'CANCELLED'}
        if obj.animation_data is None:
            obj.animation_data_create()
        keep = scene.frame_current
        _last[obj.as_pointer()] = np.zeros(len(M.vars))           # erster Frame wird gegen die Ruhelage gelöst
        names = []
        for i, slot in M.vars:
            if slot in ('qx', 'qy', 'qz'):
                prop, idx = "rotation_quaternion", -1
            elif slot == 'ry':
                prop, idx = "rotation_euler", 1
            else:
                prop, idx = "location", "xyz".index(slot[1])
            if (M.bones[i]["name"], prop, idx) not in names:
                names.append((M.bones[i]["name"], prop, idx))
        try:
            for f in range(scene.frame_start, scene.frame_end + 1):
                scene.frame_set(f)
                for bone, prop, idx in names:
                    obj.pose.bones[bone].keyframe_insert(prop, index=idx, frame=f)
        finally:
            scene.frame_set(keep)
        return {'FINISHED'}


def menu_pose(self, context):
    self.layout.operator("ivx.bake_inventor_rig")


classes = (IVX_OT_bake_inventor_rig,)


def register():
    for h, fn in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
                  (bpy.app.handlers.frame_change_post, _on_frame), (bpy.app.handlers.load_post, _on_load)):
        if fn not in h:
            h.append(fn)
    bpy.types.VIEW3D_MT_pose.append(menu_pose)


def unregister():
    bpy.types.VIEW3D_MT_pose.remove(menu_pose)
    for h, fn in ((bpy.app.handlers.depsgraph_update_post, _on_depsgraph),
                  (bpy.app.handlers.frame_change_post, _on_frame), (bpy.app.handlers.load_post, _on_load)):
        if fn in h:
            h.remove(fn)
    _models.clear()
    _last.clear()

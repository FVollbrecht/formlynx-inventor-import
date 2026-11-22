# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Inventor-Joints als Blender-Armature mit IK-Sperren und Grenzen.

Ruhelage = importierte Lage. Ein Bone je Joint im Gelenkursprung, Bone-Y = Gelenkachse (Inventor-Z).
Drehungen: IK-Sperren/IK-Grenzen plus Limit Rotation (auch für FK). Verschiebungen: Bone-Bewegung mit
gesperrten Achsen und Limit Location – der IK-Solver bewegt sie nicht (üblich in Mechanik-Rigs, FBX-tauglich).
Spannbaum ab dem ersten Vorkommen je Gruppe (Inventor fixiert das erste Teil); Joints, die eine Schleife schließen,
werden ein IK-Ziel: PIN-Bone an der einen Seite, TIP-Bone mit IK an der anderen (IK zielt auf den Kopf des Ziels).
Schließt die Schleife an einem Zylinder- oder Schiebe-Joint, darf der TIP-Bone längs der Achse dehnen (IK-Stretch nur
an ihm): die Schleife hält dann die Achse, nicht einen Punkt – das Teil gleitet.
Abhängigkeiten (Passend, Fluchtend, …) werden gelesen, aber noch nicht übersetzt. Ausnahme Drehung/Translation:
liegt sie auf einem Zylinder-Joint, trägt ein Kind-Bone „SCREW …“ das Teil und ein Treiber schiebt ihn aus der
Drehung des Joint-Bones (eigener Bone, sonst Abhängigkeitszyklus); Steigung je Umdrehung als Eigenschaft am Joint-Bone.
"""
import math
import re
from collections import deque

import bpy
from mathutils import Matrix, Vector

from . import inventor_joints

BONE_FRACTION = 0.4             # Bone-Länge relativ zur größten Abmessung des bewegten Teils
MIN_BONE = 0.002                # m (in Blender-Einheiten umgerechnet über scale je mm)
ROTATING = {'REVOLUTE', 'CYLINDRICAL', 'PLANAR', 'BALL'}


class Occurrence:
    """Vorkommen der obersten Ebene: Objekte, die es tragen (Empty oder Körper), Weltmatrix falls bekannt"""

    def __init__(self, name, handles, world):
        self.name, self.handles, self.world = name, handles, world


def _scaled(frame, scale):
    m = Matrix(frame.tolist())
    m.translation = m.translation * scale
    return m


def _limits(values, kind):
    """(aktueller Wert, (min, max) oder None) aus der Werteliste; Winkel rad, Längen mm"""
    if kind == 'REVOLUTE':
        current = values[1] if len(values) > 1 else 0.0
        rng = sorted(values[2:4]) if len(values) >= 4 else None
        return current, rng
    if kind == 'CYLINDRICAL':
        return (values[0] if values else 0.0), None
    return 0.0, None


def _prepare(joints, occurrences, scale):
    """Nutzbare Joints mit Weltrahmen; Rest zählen"""
    usable, info = [], dict(constraints=0, unmatched=[], no_frame=[])
    for j in joints:
        if j["kind"] not in inventor_joints.JOINTS:
            info["constraints"] += 1
            continue
        if len(j["sides"]) != 2:
            info["no_frame"].append(j["name"])
            continue
        (na, fa), (nb, fb) = j["sides"]
        a, b = occurrences.get(_key(na)), occurrences.get(_key(nb))
        if a is None or b is None or a is b:
            info["unmatched"].append(j["name"])
            continue
        # Rahmen von einer Seite mit bekannter Weltlage (Teile; Unterbaugruppen haben keine eigene Matrix)
        if a.world is not None and fa is not None:
            world, ref = a.world @ _scaled(fa, scale), a
        elif b.world is not None and fb is not None:
            world, ref = b.world @ _scaled(fb, scale), b
        else:
            info["no_frame"].append(j["name"])
            continue
        usable.append(dict(joint=j, a=a, b=b, world=world, ref=ref))
    return usable, info


def _key(name):
    if not name:
        return None
    return re.sub(r"\.(iam|ipt)(?=:\d+$)", "", name, flags=re.I).strip().casefold()


def _collinear(axes, tol=1e-4):
    """Alle Drehachsen auf einer Linie (Kugelgelenke zählen nie dazu)"""
    if any(ball for _h, _d, ball in axes):
        return False
    p0, d0 = axes[0][0], axes[0][1]
    scale = max((h - p0).length for h, _d, _b in axes) or 1.0
    for h, d, _b in axes[1:]:
        if d.cross(d0).length > tol or (h - p0).cross(d0).length > tol * scale + 1e-9:
            return False
    return True


def _depth(bone):
    n = 0
    while bone is not None:
        n += 1
        bone = bone.parent
    return n


def _size(handles):
    size = 0.0
    for h in handles:
        objs = [h] + [c for c in h.children_recursive]
        for o in objs:
            size = max(size, max(o.dimensions) if o.type == 'MESH' else 0.0)
    return size


def _couple(obj, pb, screw, coupling, scale):
    """Treiber am SCREW-Bone: Verschiebung längs Y = Drehung des Joint-Bones um Y / 2π · Steigung. Steigung (mm je
    Umdrehung) steht am Joint-Bone, Vorzeichen dort umkehrbar. Wirkt auf die gestellte Drehung (FK), nicht auf IK."""
    name, pitch = coupling
    pb["Inventor coupling"] = name
    pb["Inventor pitch mm"] = pitch
    pb.lock_location = (True, True, True)       # Verschiebung kommt jetzt aus der Drehung
    screw.lock_location = screw.lock_rotation = (True, True, True)
    screw.lock_ik_x = screw.lock_ik_y = screw.lock_ik_z = True
    bone = bpy.utils.escape_identifier(pb.name)
    drv = screw.driver_add("location", 1).driver
    drv.type = 'SCRIPTED'
    for var_name, path in (("rot", f'pose.bones["{bone}"].rotation_euler[1]'),
                           ("pitch", f'pose.bones["{bone}"]["Inventor pitch mm"]')):
        var = drv.variables.new()
        var.name, var.type = var_name, 'SINGLE_PROP'
        var.targets[0].id = obj
        var.targets[0].data_path = path
    drv.expression = f"rot * pitch * {scale / (2 * math.pi)!r}"


def build(context, cfb, occurrences, collection, name, scale):
    """Armature aus den Joints der Baugruppe. occurrences: {Schlüssel: Occurrence} in Einfügereihenfolge.
    -> (Armature-Objekt oder None, Info-dict)"""
    joints = inventor_joints.read(cfb)
    usable, info = _prepare(joints, occurrences, scale)
    info.update(joints=0, loops=0, ik_chains=0, couplings=0)
    if not usable:
        return None, info

    # Drehung/Translation: Paar der Vorkommen -> Steigung in mm je Umdrehung (Inventor speichert cm)
    couplings = {}
    for j in joints:
        if j["kind"] == 'ROTATE_TRANSLATE' and len(j["sides"]) == 2 and j["values"]:
            pair = frozenset(id(occurrences.get(_key(n))) for n, _f in j["sides"])
            couplings[pair] = (j["name"], j["values"][0] * 10.0)

    def coupled(e):
        return e["joint"]["kind"] == 'CYLINDRICAL' and frozenset((id(e["a"]), id(e["b"]))) in couplings

    adjacency = {}
    for e in usable:
        adjacency.setdefault(id(e["a"]), []).append(e)
        adjacency.setdefault(id(e["b"]), []).append(e)
    for edges in adjacency.values():     # gekoppelte Joints zuerst: sie sollen Bones werden, kein Schleifenschluss
        edges.sort(key=lambda e: not coupled(e))

    # Spannbaum je Gruppe, Wurzel = erstes Vorkommen in Einfügereihenfolge
    parent_of, children, tree, loops, roots = {}, {}, [], [], []
    seen = set()
    for occ in occurrences.values():
        if id(occ) not in adjacency or id(occ) in seen:
            continue
        roots.append(occ)
        seen.add(id(occ))
        queue = deque([occ])
        used = set()
        while queue:
            cur = queue.popleft()
            for e in adjacency[id(cur)]:
                if id(e) in used:
                    continue
                used.add(id(e))
                other = e["b"] if e["a"] is cur else e["a"]
                if id(other) in seen:
                    loops.append(e)
                    continue
                seen.add(id(other))
                tree.append((cur, other, e))
                queue.append(other)

    min_len = MIN_BONE * 1000.0 * scale
    arm = bpy.data.armatures.new(name)
    arm.display_type = 'STICK'
    obj = bpy.data.objects.new(name, arm)
    obj.show_in_front = True
    collection.objects.link(obj)
    prev_active = context.view_layer.objects.active
    context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode='EDIT')
    eb = arm.edit_bones
    part_bone, joint_bones, screws = {}, [], {}

    def add(bname, head, axis, length, parent, roll_to=None):
        b = eb.new(bname)
        b.head = head
        b.tail = head + axis.normalized() * length
        if roll_to is not None and roll_to.length > 1e-9:
            b.align_roll(roll_to)
        b.parent = parent
        b.use_connect = False
        b.use_deform = False
        return b

    for occ in roots:
        origin = occ.world.translation if occ.world is not None else next(
            e["world"].translation for e in adjacency[id(occ)])
        length = max(_size(occ.handles) * BONE_FRACTION, min_len)
        b = add("ROOT " + occ.name, origin.copy(), Vector((0, 0, 1)), length, None)
        part_bone[id(occ)] = b.name

    for parent, child, e in tree:
        j = e["joint"]
        if j["kind"] == 'RIGID':
            part_bone[id(child)] = part_bone[id(parent)]
            continue
        w = e["world"]
        length = max(_size(child.handles) * BONE_FRACTION, min_len)
        b = add(j["name"], w.translation.copy(), w.col[2].xyz, length, eb[part_bone[id(parent)]], w.col[0].xyz)
        part_bone[id(child)] = b.name
        if coupled(e):
            screw = add("SCREW " + j["name"], b.head.copy(), b.tail - b.head, length * 0.6, b, w.col[0].xyz)
            part_bone[id(child)] = screw.name
            screws[b.name] = screw.name
        joint_bones.append((b.name, j, e, child))
        info["joints"] += 1

    ik_pairs = []
    for e in loops:
        j = e["joint"]
        w = e["world"]
        a, b = e["a"], e["b"]
        # TIP an die Seite mit dem längeren Weg zur Wurzel: die IK-Kette soll möglichst viele Schleifengelenke enthalten
        if _depth(eb[part_bone[id(a)]]) > _depth(eb[part_bone[id(b)]]):
            a, b = b, a
        sliding = j["kind"] in ('CYLINDRICAL', 'SLIDER')
        length = max(min(_size(a.handles), _size(b.handles)) * BONE_FRACTION * 0.5, min_len)
        if sliding:                     # TIP dehnt/schrumpft beim Gleiten: Länge = Weg, den er schrumpfen kann
            length = max(_size(a.handles), _size(b.handles), min_len)
        axis = w.col[2].xyz
        pin = add("PIN " + j["name"], w.translation.copy(), axis, length, eb[part_bone[id(a)]])
        tip = eb.new("TIP " + j["name"])
        tip.tail = w.translation.copy()
        tip.head = w.translation - axis.normalized() * length
        tip.parent = eb[part_bone[id(b)]]
        tip.use_connect = False
        tip.use_deform = False
        # Kettenlänge: TIP bis vor den gemeinsamen Vorfahren
        ancestors = set()
        x = pin.parent
        while x is not None:
            ancestors.add(x.name)
            x = x.parent
        count, x = 1, tip.parent
        while x is not None and x.name not in ancestors:
            count += 1
            x = x.parent
        ik_pairs.append((tip.name, pin.name, count, sliding))
        info["loops"] += 1

    # IK-Steuerung an Kettenenden mit mindestens zwei Drehgelenken
    rotating = {name for name, j, _e, _c in joint_bones if j["kind"] in ROTATING}
    balls = {name for name, j, _e, _c in joint_bones if j["kind"] == 'BALL'}
    has_child = {b.parent.name for b in eb if b.parent is not None}
    controls = []
    for bname, j, e, child in joint_bones:
        if bname in has_child or bname not in rotating:
            continue
        axes, x = [], eb[bname]
        while x is not None and not x.name.startswith("ROOT "):
            if x.name in rotating:
                axes.append((x.head.copy(), (x.tail - x.head).normalized(), x.name in balls))
            x = x.parent
        if len(axes) < 2 or _collinear(axes):
            continue                    # eine Achse (Schraube auf Scheibe): IK bringt nichts
        src = eb[bname]
        ctrl = eb.new("IK " + bname)
        ctrl.head = src.tail.copy()
        ctrl.tail = src.tail + (src.tail - src.head) * 0.5
        ctrl.parent = eb[part_bone[id(roots[0])]] if roots else None
        ctrl.use_deform = False
        depth, x = 0, src
        while x is not None and not x.name.startswith("ROOT "):
            depth += 1
            x = x.parent
        controls.append((bname, ctrl.name, depth))
        info["ik_chains"] += 1

    bpy.ops.object.mode_set(mode='OBJECT')

    pose = obj.pose.bones
    for bname, j, e, child in joint_bones:
        pb = pose[bname]
        kind = j["kind"]
        pb["mlx_inventor_joint"] = kind
        pb.lock_location = (True, True, True)
        pb.lock_rotation = (True, True, True)
        pb.lock_rotation_w = False
        pb.lock_ik_x = pb.lock_ik_y = pb.lock_ik_z = True
        pb.rotation_mode = 'XYZ'
        if kind in ('REVOLUTE', 'CYLINDRICAL', 'PLANAR'):
            pb.lock_ik_y = False
            pb.lock_rotation = (True, False, True)
        if kind == 'BALL':
            pb.rotation_mode = 'QUATERNION'
            pb.lock_ik_x = pb.lock_ik_y = pb.lock_ik_z = False
            pb.lock_rotation = (False, False, False)
        if kind in ('SLIDER', 'CYLINDRICAL'):
            pb.lock_location = (True, False, True)
        if kind == 'PLANAR':
            pb.lock_location = (False, True, False)
        current, rng = _limits(j["values"], kind)
        if kind == 'REVOLUTE' and rng is not None:
            # Grenzen relativ zur Ruhelage; Vorzeichen je nachdem, welche Seite das bewegte Teil ist
            sign = 1.0 if child is e["b"] else -1.0
            if e["ref"] is e["b"]:
                sign = -sign            # Bone-Achse = Z der Seite B, gemessen antiparallel zu Z der Seite A
            lo, hi = sorted(((rng[0] - current) * sign, (rng[1] - current) * sign))
            lo, hi = max(lo, -math.pi), min(hi, math.pi)
            pb.use_ik_limit_y = True
            pb.ik_min_y, pb.ik_max_y = min(lo, 0.0), max(hi, 0.0)
            lim = pb.constraints.new('LIMIT_ROTATION')
            lim.owner_space = 'LOCAL'
            lim.use_limit_y = True
            lim.min_y, lim.max_y = pb.ik_min_y, pb.ik_max_y
            lim.name = "Inventor limits"

    for bname, j, e, child in joint_bones:
        if bname in screws:
            _couple(obj, pose[bname], pose[screws[bname]], couplings[frozenset((id(e["a"]), id(e["b"])))], scale)
            info["couplings"] += 1

    for tip, pin, count, sliding in ik_pairs:
        pb = pose[tip]
        pb.lock_ik_x = pb.lock_ik_y = pb.lock_ik_z = True
        ik = pb.constraints.new('IK')
        ik.target, ik.subtarget = obj, pin
        ik.chain_count = count
        ik.use_stretch = sliding
        if sliding:
            pb.ik_stretch = 1.0             # nur der TIP dehnt: Verschiebung längs der Gelenkachse
        ik.name = "Inventor loop"
    for bname, ctrl, depth in controls:
        ik = pose[bname].constraints.new('IK')
        ik.target, ik.subtarget = obj, ctrl
        ik.chain_count = depth
        ik.name = "Inventor IK"

    # Teile an ihre Bones hängen, Weltlage behalten
    context.view_layer.update()
    for occ in occurrences.values():
        bone = part_bone.get(id(occ))
        if bone is None:
            continue
        for h in occ.handles:
            world = h.matrix_world.copy()
            h.parent = obj
            h.parent_type = 'BONE'
            h.parent_bone = bone
            h.matrix_world = world
    context.view_layer.objects.active = prev_active
    return obj, info

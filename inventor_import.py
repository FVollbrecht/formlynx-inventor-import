# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Inventor-Import: Baugruppen (.iam) aus dem Grafik-Cache und Bauteile (.ipt) aus dem Anzeigenetz als Meshes,
ohne Inventor und ohne OpenCASCADE.

Lesen in inventor_data/inventor_part (ohne bpy). Teile werden in Blender-Einheiten gebacken, gleiche Körper
teilen ein Mesh. Bauteile: ein Objekt je Volumenkörper, Farben je Fläche.
"""
import functools
import math
import ntpath
import os
import re

import bpy
import numpy as np
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty
from bpy.types import OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix

from . import inventor_data, inventor_part, inventor_props, inventor_rig, inventor_zstd
from ._compat import CTX, mm_to_bu, rpt_


DEFAULT_COLOR = (0.8, 0.8, 0.8)
FIND_DEPTH = 2                  # Bauteil-Dateien: Unterordner unter der .iam
FIND_LIMIT = 20000              # höchstens so viele Dateien durchsehen


def _clean(name):
    """'Teil.ipt:2' -> 'Teil:2' (Dateiendung vor der Vorkommen-Nummer weg)"""
    return re.sub(r"\.(iam|ipt)(?=:\d+$)", "", name, flags=re.I)


def _strip_occurrence(name):
    return re.sub(r":\d+$", "", name)


def _material(info):
    """Material je Farbe; mit Darstellungsnamen (info["name"]) heißt es wie in Inventor, sonst IAM_<Farbcode>"""
    rgb = tuple(info["color"]) if info and info.get("color") else None
    metal = bool(info and info.get("metal"))
    code = "IAM_" + ("default" if rgb is None else "".join(f"{round(c * 255):02x}" for c in rgb)) + ("_metal" if metal else "")
    label = info.get("name") if info else None
    name = label or code
    material = bpy.data.materials.get(name)
    if material is not None and label and material.get("mlx_inventor_color") != code.removesuffix("_metal"):
        name = f"{label} {code[4:].removesuffix('_metal')}"           # gleicher Name, andere Farbe (z. B. Teil-Überschreibung)
        material = bpy.data.materials.get(name)
    if material is not None:
        return material
    color = (*(rgb or DEFAULT_COLOR), 1.0)
    material = bpy.data.materials.new(name)
    material["mlx_inventor_color"] = code.removesuffix("_metal")   # Farbe ohne Metall-Kennung
    material.diffuse_color = color
    material.metallic = 0.6 if metal else 0.0
    if material.node_tree is None:
        try:
            material.use_nodes = True
        except AttributeError:
            pass
    if material.node_tree:
        for node in material.node_tree.nodes:
            if node.type == 'BSDF_PRINCIPLED':
                node.inputs["Base Color"].default_value = color
                node.inputs["Metallic"].default_value = material.metallic
                node.inputs["Roughness"].default_value = 0.5
                break
    return material


def _fill(mesh, body, scale, mats, items, materials, color_names=None):
    verts, tris, loop_normals, mat_idx, _ = body
    n = len(tris)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", (verts * scale).astype(np.float32).ravel())
    mesh.loops.add(n * 3)
    mesh.loops.foreach_set("vertex_index", tris.astype(np.int32).ravel())
    mesh.polygons.add(n)
    mesh.polygons.foreach_set("loop_start", np.arange(0, n * 3, 3, dtype=np.int32))
    mesh.update(calc_edges=True)
    if materials:
        # ein Slot je Material (nicht je Teilnetz): gleiche Farben teilen sich den Slot
        slots, slot_of_item = {}, []
        for it in items:
            info = mats.get(it["mat"])
            if info and color_names and info.get("color"):
                label = color_names.get(inventor_part.color_key(info["color"]))
                if label:
                    info = dict(info, name=label)
            mat = _material(info)
            if mat.name not in slots:
                slots[mat.name] = len(slots)
                mesh.materials.append(mat)
            slot_of_item.append(slots[mat.name])
        mesh.polygons.foreach_set("material_index", np.array(slot_of_item, dtype=np.int32)[mat_idx])
    mesh.normals_split_custom_set(loop_normals.tolist())
    mesh.update()


def _set_props(block, props):
    """iProperties als Custom Properties (Anzeigenamen wie in Inventor)"""
    for key, value in props.items():
        if isinstance(value, int) and not isinstance(value, bool) and not -2**31 <= value < 2**31:
            value = str(value)                  # Custom Properties: Ganzzahlen nur 32 Bit
        block[key[:inventor_props.MAX_KEY]] = value


class _PartFiles:
    """Bauteil-Dateien einer Baugruppe finden: gespeicherter Pfad, sonst gleicher Dateiname neben der .iam
    oder in Unterordnern. Liest jede Datei einmal (iProperties, Darstellungsnamen)."""

    def __init__(self, iam_path, cfb):
        self.folder = os.path.dirname(os.path.abspath(iam_path))
        self.stored = {}
        for ref in inventor_props.referenced_files(cfb):
            self.stored.setdefault(ntpath.basename(ref).lower(), ref)
        self.index = None
        self.cache = {}

    def _local(self, filename):
        if self.index is None:
            self.index, count = {}, 0
            for dirpath, dirnames, filenames in os.walk(self.folder):
                if dirpath[len(self.folder):].count(os.sep) >= FIND_DEPTH:
                    dirnames[:] = []
                for f in filenames:
                    if f.lower().endswith((".ipt", ".iam")):
                        self.index.setdefault(f.lower(), os.path.join(dirpath, f))
                count += len(filenames)
                if count > FIND_LIMIT:
                    break
        return self.index.get(filename)

    def find(self, base_name, assembly):
        filename = (base_name + (".iam" if assembly else ".ipt")).lower()
        stored = self.stored.get(filename)
        if stored and os.path.isfile(stored):
            return stored
        return self._local(filename)

    def read(self, base_name, assembly):
        """-> (iProperties, {color_key: Darstellungsname}); leer, wenn die Datei fehlt oder nicht lesbar ist"""
        path = self.find(base_name, assembly)
        if path is None:
            return {}, {}
        if path not in self.cache:
            try:
                props = inventor_props.read(path)
                names = {} if assembly else inventor_part.color_names(path)
            except (OSError, inventor_data.InventorError, ValueError, IndexError):
                props, names = {}, {}
            self.cache[path] = (props, names)
        return self.cache[path]


def _up_matrix(up, file_up_z=True):
    """Z oben wie Blender: keine Drehung; Y oben: +90° um X"""
    y_up = up == 'Y' or (up == 'AUTO' and not file_up_z)
    return Matrix.Rotation(math.pi / 2, 4, 'X') if y_up else Matrix.Identity(4)


def _zstd_cached(fn):
    """Segmente holen mehrere Leser (Namen, iProperties, Joints, Parameter): je Import nur einmal entpacken"""
    @functools.wraps(fn)
    def run(*args, **kw):
        with inventor_zstd.cached():
            return fn(*args, **kw)
    return run


@_zstd_cached
def build(context, path, up='AUTO', hierarchy='COLLECTIONS', materials=True, iproperties=True, joints=True):
    """Liest die .iam und baut Objekte -> (Objektliste, Info-dict). Wirft inventor_data.InventorError.
    iproperties: Baugruppe an die Wurzel-Collection, Bauteile (wenn ihre Dateien auffindbar sind) an ihre Objekte.
    joints: Inventor-Joints als Armature mit IK (inventor_rig), nur wenn die Baugruppe welche hat."""
    result = inventor_data.read(path)
    nodes, meshes, mats, names = result["nodes"], result["meshes"], result["mats"], result["names"]
    scale = mm_to_bu(context.scene, 1.0)
    up_matrix = _up_matrix(up)
    stem = os.path.splitext(os.path.basename(path))[0]

    root = bpy.data.collections.new(stem)
    context.collection.children.link(root)
    part_files = cfb = None
    if iproperties or joints:
        with open(path, "rb") as f:
            cfb = inventor_data.Cfb(f.read())
    if iproperties:
        _set_props(root, inventor_props.read_cfb(cfb))
        part_files = _PartFiles(path, cfb)

    # Körper je Vorkommen der obersten Ebene sammeln
    groups, skipped = {}, 0
    for node in nodes:
        items = [it for it in node["items"] if "position" in meshes.get(it["mesh"], {})]
        skipped += len(node["items"]) - len(items)
        if items:
            top = node["path"].split(":")[0] if node["path"] else ""
            groups.setdefault(top, []).append((node, items))

    mesh_cache, created, occurrences = {}, [], {}
    for top, bodies in groups.items():
        info = names.get(top)
        occurrence = _clean(info["name"]) if info else (f"Component {top}" if top else stem)
        base_name = _strip_occurrence(occurrence)
        props, color_names = part_files.read(base_name, bool(info and info.get("assembly"))) if part_files else ({}, {})
        single = len(bodies) == 1 and len(bodies[0][0]["path"].split(":")) == 1
        container, parent = root, None
        if not single and hierarchy == 'COLLECTIONS':
            container = bpy.data.collections.new(occurrence)
            root.children.link(container)
            _set_props(container, props)
        elif not single and hierarchy == 'EMPTIES':
            parent = bpy.data.objects.new(occurrence, None)
            parent.empty_display_size = 10 * scale
            parent.matrix_basis = up_matrix
            root.objects.link(parent)
            _set_props(parent, props)
            created.append(parent)
        occ = inventor_rig.Occurrence(occurrence, [parent] if parent is not None else [], None)
        occurrences.setdefault(inventor_rig._key(info["name"] if info else occurrence), occ)
        for number, (node, items) in enumerate(bodies, 1):
            key = tuple(it["mesh"] for it in items)
            body = inventor_data.body_mesh(items, meshes)
            if body is None:
                continue
            name = base_name if single else f"{base_name} – Part {number}"
            mesh = mesh_cache.get(key)
            if mesh is None:
                mesh = bpy.data.meshes.new(name)
                _fill(mesh, body, scale, mats, items, materials, color_names)
                mesh["mlx_inventor_source"] = os.path.abspath(path)
                mesh_cache[key] = mesh
            obj = bpy.data.objects.new(name, mesh)
            obj["mlx_inventor_occurrence"] = node["path"]
            _set_props(obj, props)
            base = body[4].copy()
            base[:3, 3] *= scale
            world = up_matrix @ Matrix(base.tolist())
            container.objects.link(obj)
            if parent is not None:
                obj.parent = parent
                obj.matrix_basis = up_matrix.inverted() @ world
            else:
                obj.matrix_basis = world
            created.append(obj)
            if parent is None:
                occ.handles.append(obj)
            if single:
                occ.world = world.copy()
    found = sum(1 for v in part_files.cache.values() if v[0]) if part_files else 0
    rig_info = {}
    if joints and result["names_ok"]:
        rig, rig_info = inventor_rig.build(context, cfb, occurrences, root, f"{stem} Rig", scale)
        if rig is not None:
            created.append(rig)
    return created, dict(skipped=skipped, names_ok=result["names_ok"], occurrences=len(groups), part_files=found,
                         joints=rig_info.get("joints", 0), loops=rig_info.get("loops", 0),
                         constraints=rig_info.get("constraints", 0))


@_zstd_cached
def build_part(context, path, up='AUTO', hierarchy='COLLECTIONS', materials=True, iproperties=True):
    """Liest die .ipt und baut ein Objekt je Körper -> (Objektliste, Info-dict). Wirft inventor_data.InventorError"""
    result = inventor_part.read(path)
    scale = mm_to_bu(context.scene, 1.0)
    up_matrix = _up_matrix(up)
    stem = os.path.splitext(os.path.basename(path))[0]
    root = bpy.data.collections.new(stem)
    context.collection.children.link(root)
    props = result["props"] if iproperties else {}
    _set_props(root, props)
    bodies = result["bodies"]
    parent, created = None, []
    if len(bodies) > 1 and hierarchy == 'EMPTIES':
        parent = bpy.data.objects.new(stem, None)
        parent.empty_display_size = 10 * scale
        parent.matrix_basis = up_matrix
        root.objects.link(parent)
        _set_props(parent, props)
        created.append(parent)
    for number, body in enumerate(bodies, 1):
        name = stem if len(bodies) == 1 else f"{stem} – Body {number}"
        mesh = bpy.data.meshes.new(name)
        tris = body["tris"]
        n = len(tris)
        mesh.vertices.add(len(body["verts"]))
        mesh.vertices.foreach_set("co", (body["verts"] * scale).astype(np.float32).ravel())
        mesh.loops.add(n * 3)
        mesh.loops.foreach_set("vertex_index", tris.astype(np.int32).ravel())
        mesh.polygons.add(n)
        mesh.polygons.foreach_set("loop_start", np.arange(0, n * 3, 3, dtype=np.int32))
        mesh.update(calc_edges=True)
        if materials:
            for rgba, label in zip(body["colors"], body["names"]):
                mesh.materials.append(_material({"color": rgba[:3], "name": label} if rgba else None))
            mesh.polygons.foreach_set("material_index", body["color_idx"])
        mesh.normals_split_custom_set(body["normals"][tris].reshape(-1, 3).tolist())
        mesh.update()
        mesh["mlx_inventor_source"] = os.path.abspath(path)
        obj = bpy.data.objects.new(name, mesh)
        _set_props(obj, props)
        root.objects.link(obj)
        if parent is not None:
            obj.parent = parent
        else:
            obj.matrix_basis = up_matrix
        created.append(obj)
    return created, dict(skipped=0, names_ok=True, occurrences=1)


class IVX_OT_import_inventor(bpy.types.Operator, ImportHelper):
    """Import Autodesk Inventor assemblies (.iam) and parts (.ipt) as meshes, no Inventor needed"""
    bl_idname = "ivx.import_inventor"
    bl_label = "Import Inventor"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}
    filename_ext = ".iam"

    filter_glob: StringProperty(translation_context=CTX, default="*.iam;*.ipt",
                                options={'HIDDEN'})
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    hierarchy: EnumProperty(translation_context=CTX, name="Assemblies", default='COLLECTIONS', items=[
        ('COLLECTIONS', "Collections", "One collection per assembly"),
        ('EMPTIES', "Empties", "Parts parented to one empty per assembly"),
        ('FLAT', "Flat", "All parts in one collection"),
    ])
    up: EnumProperty(translation_context=CTX, name="Up axis of file", default='AUTO', items=[
        ('AUTO', "Auto", "Z-up like Inventor"),
        ('Z', "Z", "File is Z-up, like Blender"),
        ('Y', "Y", "File is Y-up, parts are rotated to Z-up"),
    ])
    materials: BoolProperty(translation_context=CTX, name="Materials", default=True,
                            description="Create materials from the colors in the file")
    iproperties: BoolProperty(translation_context=CTX, name="iProperties", default=True,
                              description="Copy the Inventor iProperties to custom properties. Assemblies also read "
                                          "their part files when they are found next to the assembly or at the saved path")
    joints: BoolProperty(translation_context=CTX, name="Joints as Rig", default=True,
                         description="Turn the Inventor joints of an assembly into an armature: one bone per joint "
                                     "with IK locks and limits for its degrees of freedom")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        for key in ("hierarchy", "up", "materials", "iproperties", "joints"):
            layout.prop(self, key)

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        folder = self.directory or os.path.dirname(self.filepath)
        names = [f.name for f in self.files if f.name] or [os.path.basename(self.filepath)]
        created, failed, parts_only, skipped, joints = [], [], [], 0, 0
        for name in names:
            path = os.path.join(folder, name)
            try:
                if name.lower().endswith(".ipt"):
                    objs, info = build_part(context, path, self.up, self.hierarchy, self.materials, self.iproperties)
                else:
                    objs, info = build(context, path, self.up, self.hierarchy, self.materials, self.iproperties,
                                       self.joints)
            except inventor_data.PartFileError:
                parts_only.append(name)
                continue
            except (inventor_data.InventorError, OSError) as error:
                failed.append(f"{name} ({error})")
                continue
            created += objs
            skipped += info["skipped"]
            joints += info.get("joints", 0)

        if parts_only and not created and not failed:
            self.report({'ERROR'}, rpt_("Inventor parts (.ipt) without display mesh: open and save them in Inventor, "
                                        "or export them as STEP"))
            return {'CANCELLED'}
        if not created:
            self.report({'ERROR'}, rpt_("Could not read Inventor file: {name}").format(name=", ".join(failed + parts_only)))
            return {'CANCELLED'}

        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in created:
            obj.select_set(True)
        context.view_layer.objects.active = next((o for o in created if o.type == 'MESH'), created[0])
        parts = sum(1 for o in created if o.type == 'MESH')
        problems = failed + [rpt_("{name} (no display mesh)").format(name=n) for n in parts_only]
        if problems:
            self.report({'WARNING'}, rpt_("Imported {parts} parts, could not read: {name}").format(parts=parts, name=", ".join(problems)))
        elif skipped:
            self.report({'WARNING'}, rpt_("Imported {parts} parts, {count} incomplete meshes skipped").format(parts=parts, count=skipped))
        elif joints:
            self.report({'INFO'}, rpt_("Imported {parts} parts, {count} joints as rig").format(parts=parts, count=joints))
        else:
            self.report({'INFO'}, rpt_("Imported {parts} parts").format(parts=parts))
        return {'FINISHED'}


def menu_import(self, context):
    self.layout.operator("ivx.import_inventor", text_ctxt=CTX, text="Inventor (.iam, .ipt)")


classes = (IVX_OT_import_inventor,)


def register():
    bpy.types.TOPBAR_MT_file_import.append(menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)

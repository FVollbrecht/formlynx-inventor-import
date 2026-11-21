# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Inventor-Import: Baugruppen (.iam) aus dem Grafik-Cache und Bauteile (.ipt) aus dem Anzeigenetz als Meshes,
ohne Inventor und ohne OpenCASCADE.
Freie Extension (FORMLYNX Inventor Import), gebaut aus MODULYNX – ohne DWF.

Lesen in inventor_data/inventor_part/dwf_data (ohne bpy). Teile werden in Blender-Einheiten gebacken, gleiche Körper
teilen ein Mesh. Bauteile: ein Objekt je Volumenkörper, Farben je Fläche.
"""
import math
import os
import re

import bpy
import numpy as np
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, StringProperty
from bpy.types import OperatorFileListElement
from bpy_extras.io_utils import ImportHelper
from mathutils import Matrix

from . import inventor_data, inventor_part
from ._compat import CTX, mm_to_bu, rpt_

dwf_data = dwf_hsf = None               # DWF gehört nicht zur freien Extension

DEFAULT_COLOR = (0.8, 0.8, 0.8)


def _clean(name):
    """'Teil.ipt:2' -> 'Teil:2' (Dateiendung vor der Vorkommen-Nummer weg)"""
    return re.sub(r"\.(iam|ipt)(?=:\d+$)", "", name, flags=re.I)


def _strip_occurrence(name):
    return re.sub(r":\d+$", "", name)


def _material(info):
    rgb = tuple(info["color"]) if info and info.get("color") else None
    metal = bool(info and info.get("metal"))
    name = "IAM_" + ("default" if rgb is None else "".join(f"{round(c * 255):02x}" for c in rgb)) + ("_metal" if metal else "")
    material = bpy.data.materials.get(name)
    if material is not None:
        return material
    color = (*(rgb or DEFAULT_COLOR), 1.0)
    material = bpy.data.materials.new(name)
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


def _fill(mesh, body, scale, mats, items, materials):
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
            mat = _material(mats.get(it["mat"]))
            if mat.name not in slots:
                slots[mat.name] = len(slots)
                mesh.materials.append(mat)
            slot_of_item.append(slots[mat.name])
        mesh.polygons.foreach_set("material_index", np.array(slot_of_item, dtype=np.int32)[mat_idx])
    mesh.normals_split_custom_set(loop_normals.tolist())
    mesh.update()


def _up_matrix(up, file_up_z=True):
    """Z oben wie Blender: keine Drehung; Y oben: +90° um X"""
    y_up = up == 'Y' or (up == 'AUTO' and not file_up_z)
    return Matrix.Rotation(math.pi / 2, 4, 'X') if y_up else Matrix.Identity(4)


def build(context, path, up='AUTO', hierarchy='COLLECTIONS', materials=True):
    """Liest die .iam und baut Objekte -> (Objektliste, Info-dict). Wirft inventor_data.InventorError"""
    result = inventor_data.read(path)
    nodes, meshes, mats, names = result["nodes"], result["meshes"], result["mats"], result["names"]
    scale = mm_to_bu(context.scene, 1.0)
    up_matrix = _up_matrix(up)
    stem = os.path.splitext(os.path.basename(path))[0]

    root = bpy.data.collections.new(stem)
    context.collection.children.link(root)

    # Körper je Vorkommen der obersten Ebene sammeln
    groups, skipped = {}, 0
    for node in nodes:
        items = [it for it in node["items"] if "position" in meshes.get(it["mesh"], {})]
        skipped += len(node["items"]) - len(items)
        if items:
            top = node["path"].split(":")[0] if node["path"] else ""
            groups.setdefault(top, []).append((node, items))

    mesh_cache, created = {}, []
    for top, bodies in groups.items():
        info = names.get(top)
        occurrence = _clean(info["name"]) if info else (f"Component {top}" if top else stem)
        base_name = _strip_occurrence(occurrence)
        single = len(bodies) == 1 and len(bodies[0][0]["path"].split(":")) == 1
        container, parent = root, None
        if not single and hierarchy == 'COLLECTIONS':
            container = bpy.data.collections.new(occurrence)
            root.children.link(container)
        elif not single and hierarchy == 'EMPTIES':
            parent = bpy.data.objects.new(occurrence, None)
            parent.empty_display_size = 10 * scale
            parent.matrix_basis = up_matrix
            root.objects.link(parent)
            created.append(parent)
        for number, (node, items) in enumerate(bodies, 1):
            key = tuple(it["mesh"] for it in items)
            body = inventor_data.body_mesh(items, meshes)
            if body is None:
                continue
            name = base_name if single else f"{base_name} – Part {number}"
            mesh = mesh_cache.get(key)
            if mesh is None:
                mesh = bpy.data.meshes.new(name)
                _fill(mesh, body, scale, mats, items, materials)
                mesh["mlx_inventor_source"] = os.path.abspath(path)
                mesh_cache[key] = mesh
            obj = bpy.data.objects.new(name, mesh)
            obj["mlx_inventor_occurrence"] = node["path"]
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
    return created, dict(skipped=skipped, names_ok=result["names_ok"], occurrences=len(groups))


def build_part(context, path, up='AUTO', hierarchy='COLLECTIONS', materials=True):
    """Liest die .ipt und baut ein Objekt je Körper -> (Objektliste, Info-dict). Wirft inventor_data.InventorError"""
    result = inventor_part.read(path)
    scale = mm_to_bu(context.scene, 1.0)
    up_matrix = _up_matrix(up)
    stem = os.path.splitext(os.path.basename(path))[0]
    root = bpy.data.collections.new(stem)
    context.collection.children.link(root)
    bodies = result["bodies"]
    parent, created = None, []
    if len(bodies) > 1 and hierarchy == 'EMPTIES':
        parent = bpy.data.objects.new(stem, None)
        parent.empty_display_size = 10 * scale
        parent.matrix_basis = up_matrix
        root.objects.link(parent)
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
            for rgba in body["colors"]:
                mesh.materials.append(_material({"color": rgba[:3]} if rgba else None))
            mesh.polygons.foreach_set("material_index", body["color_idx"])
        mesh.normals_split_custom_set(body["normals"][tris].reshape(-1, 3).tolist())
        mesh.update()
        mesh["mlx_inventor_source"] = os.path.abspath(path)
        obj = bpy.data.objects.new(name, mesh)
        root.objects.link(obj)
        if parent is not None:
            obj.parent = parent
        else:
            obj.matrix_basis = up_matrix
        created.append(obj)
    return created, dict(skipped=0, names_ok=True, occurrences=1)


def _fill_dwf(mesh, data, scale, materials):
    verts, tris, loop_normals, col_idx, colors = data
    n = len(tris)
    mesh.vertices.add(len(verts))
    mesh.vertices.foreach_set("co", (verts * scale).astype(np.float32).ravel())
    mesh.loops.add(n * 3)
    mesh.loops.foreach_set("vertex_index", tris.astype(np.int32).ravel())
    mesh.polygons.add(n)
    mesh.polygons.foreach_set("loop_start", np.arange(0, n * 3, 3, dtype=np.int32))
    mesh.update(calc_edges=True)
    if materials:
        for color in colors:
            mesh.materials.append(_material({"color": color}))
        mesh.polygons.foreach_set("material_index", col_idx.astype(np.int32))
    if loop_normals is not None:
        mesh.normals_split_custom_set(loop_normals.tolist())
    mesh.update()


def build_dwf(context, path, up='AUTO', hierarchy='COLLECTIONS', materials=True):
    """Liest .dwf/.dwfx (nur 3D) und baut Objekte -> (Objektliste, Info-dict). Wirft dwf_data.DwfError, dwf_hsf.HsfError"""
    models = dwf_data.read(path)
    bu = mm_to_bu(context.scene, 1.0)
    stem = re.sub(r"\.(iam|ipt|ipn|idw|dwg)$", "", os.path.splitext(os.path.basename(path))[0], flags=re.I)
    top = bpy.data.collections.new(stem)
    context.collection.children.link(top)
    created, mesh_cache, missing = [], {}, 0
    for model in models:
        root = top
        if len(models) > 1:
            root = bpy.data.collections.new(model["title"] or stem)
            top.children.link(root)
        to_bu = np.array(_up_matrix(up, model["up_z"])) @ np.diag([bu, bu, bu, 1.0]) @ model["model"]
        names = model["names"]
        missing += model["missing"]
        containers = {(): (root, None)}

        def container(keys):
            """Collection bzw. Empty je Baugruppen-Pfad (Kette der HSF-Schlüssel)"""
            if keys in containers:
                return containers[keys]
            parent_coll, parent_obj = container(keys[:-1])
            label = names.get(keys[-1])
            if not label:                                   # unbenannte Zwischenebene: keine eigene Gruppe
                entry = (parent_coll, parent_obj)
            elif hierarchy == 'COLLECTIONS':
                coll = bpy.data.collections.new(label)
                parent_coll.children.link(coll)
                entry = (coll, None)
            elif hierarchy == 'EMPTIES':
                empty = bpy.data.objects.new(label, None)
                empty.empty_display_size = 10 * bu
                parent_coll.objects.link(empty)
                empty.parent = parent_obj
                created.append(empty)
                entry = (parent_coll, empty)
            else:
                entry = (root, None)
            containers[keys] = entry
            return entry

        for part in model["parts"]:
            world = to_bu @ part.world
            det = np.linalg.det(world[:3, :3])
            if abs(det) < 1e-30:
                continue
            s = abs(det) ** (1 / 3)              # Maßstab ins Mesh backen, Objekt behält Maßstab 1
            key = (round(s, 9),) + tuple((id(shell), m.tobytes(), color) for shell, m, color in part.items)
            name = part.name or (names.get(part.path[-1]) if part.path else "") or stem
            mesh = mesh_cache.get(key)
            if mesh is None:
                data = dwf_data.part_mesh(part)
                if data is None:
                    continue
                mesh = bpy.data.meshes.new(name)
                _fill_dwf(mesh, data, s, materials)
                mesh["mlx_inventor_source"] = os.path.abspath(path)
                mesh_cache[key] = mesh
            obj = bpy.data.objects.new(name, mesh)
            matrix = world.copy()
            matrix[:3, :3] /= s
            coll, parent = container(part.path) if hierarchy != 'FLAT' else (root, None)
            coll.objects.link(obj)
            obj.matrix_basis = Matrix(matrix.tolist())     # Empties liegen im Ursprung: Weltlage = Basis
            obj.parent = parent
            created.append(obj)
    return created, dict(skipped=0, missing=missing, names_ok=True, occurrences=len(created))


class IVX_OT_import_inventor(bpy.types.Operator, ImportHelper):
    """Import Autodesk Inventor assemblies (.iam) and parts (.ipt) as meshes, no Inventor needed"""
    bl_idname = "ivx.import_inventor"
    bl_label = "Import Inventor"
    bl_options = {'REGISTER', 'UNDO', 'PRESET'}
    filename_ext = ".iam"

    filter_glob: StringProperty(translation_context=CTX, default="*.iam;*.ipt;*.dwf;*.dwfx" if dwf_data else "*.iam;*.ipt",
                                options={'HIDDEN'})
    files: CollectionProperty(type=OperatorFileListElement, options={'HIDDEN', 'SKIP_SAVE'})
    directory: StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    hierarchy: EnumProperty(translation_context=CTX, name="Assemblies", default='COLLECTIONS', items=[
        ('COLLECTIONS', "Collections", "One collection per assembly"),
        ('EMPTIES', "Empties", "Parts parented to one empty per assembly"),
        ('FLAT', "Flat", "All parts in one collection"),
    ])
    up: EnumProperty(translation_context=CTX, name="Up axis of file", default='AUTO', items=[
        ('AUTO', "Auto", "Z-up like Inventor; DWF files state their own up axis"),
        ('Z', "Z", "File is Z-up, like Blender"),
        ('Y', "Y", "File is Y-up, parts are rotated to Z-up"),
    ])
    materials: BoolProperty(translation_context=CTX, name="Materials", default=True,
                            description="Create materials from the colors in the file")

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        for key in ("hierarchy", "up", "materials"):
            layout.prop(self, key)

    def execute(self, context):
        if context.mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT')
        folder = self.directory or os.path.dirname(self.filepath)
        names = [f.name for f in self.files if f.name] or [os.path.basename(self.filepath)]
        created, failed, parts_only, skipped = [], [], [], 0
        for name in names:
            path = os.path.join(folder, name)
            is_dwf = name.lower().endswith((".dwf", ".dwfx"))
            errors = (inventor_data.InventorError, OSError) + ((dwf_data.DwfError, dwf_hsf.HsfError) if dwf_data else ())
            try:
                if is_dwf and dwf_data is None:
                    raise inventor_data.InventorError("DWF import is not part of this build")
                if is_dwf:
                    objs, info = build_dwf(context, path, self.up, self.hierarchy, self.materials)
                elif name.lower().endswith(".ipt"):
                    objs, info = build_part(context, path, self.up, self.hierarchy, self.materials)
                else:
                    objs, info = build(context, path, self.up, self.hierarchy, self.materials)
            except inventor_data.PartFileError:
                parts_only.append(name)
                continue
            except errors as error:
                failed.append(f"{name} ({error})")
                continue
            created += objs
            skipped += info["skipped"]

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
        else:
            self.report({'INFO'}, rpt_("Imported {parts} parts").format(parts=parts))
        return {'FINISHED'}


def menu_import(self, context):
    if hasattr(bpy.types, "MLX_OT_import_inventor"):   # MODULYNX bringt denselben Import mit
        return
    if dwf_data:
        self.layout.operator("ivx.import_inventor", text_ctxt=CTX, text="Inventor (.iam, .ipt, .dwf, .dwfx)")
    else:
        self.layout.operator("ivx.import_inventor", text_ctxt=CTX, text="Inventor (.iam, .ipt)")


classes = (IVX_OT_import_inventor,)


def register():
    bpy.types.TOPBAR_MT_file_import.append(menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_import)

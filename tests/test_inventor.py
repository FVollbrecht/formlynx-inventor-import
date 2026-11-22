"""FORMLYNX Inventor Import: echt installiert in einem abgeschotteten Profil – Operator, Menü, Übersetzung, Import.

BLENDER_USER_RESOURCES=<leerer ordner> blender -b --factory-startup --python tests/test_inventor.py -- <ext.zip>
(t.sh inventor baut das Paket und ruft das auf). Testdateien nur lokal in I:/GIT/FORMLYNX/testdaten/inventor.
"""
import json
import math
import os
import re
import sys
import traceback

import bpy
import numpy as np
from mathutils import Matrix, Vector

EXT_ZIP = sys.argv[sys.argv.index("--") + 1]
PKG = "bl_ext.user_default.formlynx_inventor_import"
FAILS = []
ctx = bpy.context
sc = ctx.scene
sc.unit_settings.system = 'METRIC'
sc.unit_settings.scale_length = 0.1     # 1 BU = 100 mm, wie in der Praxis


def check(name, ok, info=""):
    if not ok:
        FAILS.append(name)
        print(f"FAIL {name}: {info}")


class _Layout:
    """Nimmt die operator()-Aufrufe der Menüfunktion auf."""
    def __init__(self):
        self.calls = []

    def operator(self, idname, **kw):
        self.calls.append(idname)


def menu_entries():
    found = []
    for fn in getattr(bpy.types.TOPBAR_MT_file_import.draw, "_draw_funcs", []):
        if getattr(fn, "__module__", "").endswith("inventor_import"):
            fake = type("M", (), {})()
            fake.layout = _Layout()
            fn(fake, bpy.context)
            found += fake.layout.calls
    return found


bpy.ops.extensions.package_install_files(filepath=EXT_ZIP, repo="user_default", enable_on_install=True)
INV = sys.modules.get(PKG + ".inventor_import")


def test_extension():
    check("Extension aktiv", INV is not None and hasattr(bpy.types, "IVX_OT_import_inventor"))
    check("Menü", menu_entries() == ["ivx.import_inventor"], menu_entries())
    folder = "I:/GIT/FORMLYNX/testdaten/inventor"
    if os.path.isdir(folder):
        for name, bodies in (("iam/00029-007-000 OBD Stecker Satrotec.iam", 18), ("ipt/test mb color.ipt", 3)):
            before = {o.name for o in bpy.data.objects}
            sub, base = os.path.split(name)
            res = bpy.ops.ivx.import_inventor(filepath=os.path.join(folder, name), files=[{"name": base}],
                                              directory=os.path.join(folder, sub))
            new = [o for o in bpy.data.objects if o.name not in before]
            check(f"Operator {name}", res == {'FINISHED'} and sum(o.type == 'MESH' for o in new) == bodies, res)
            for o in new:
                bpy.data.objects.remove(o)
    prefs = bpy.context.preferences.view
    prefs.language = 'de_DE'
    prefs.use_translate_reports = True
    msg = INV.rpt_("Imported {parts} parts")
    check("Deutsch", msg == "{parts} Teile importiert", msg)
    prefs.language = 'en_US'


def test_inventor_import():
    """Inventor-Baugruppen aus dem Grafik-Cache (Beispieldateien nur lokal in testdaten/inventor)"""
    folder = "I:/GIT/FORMLYNX/testdaten/inventor"
    if not os.path.isdir(folder):
        print("testdaten/inventor fehlt – Inventor-Test übersprungen")
        return
    # Zahlen wie im Prototyp (AssemblyHTML): Körper, Dreiecke grob, Namen vollständig
    for name, bodies, tris in (("iam/00029-007-000 OBD Stecker Satrotec.iam", 18, 14978), ("iam/IR18081-00.iam", 273, 128659)):
        before = set(bpy.data.collections)
        objs, info = INV.build(ctx, os.path.join(folder, name))
        meshes = [o for o in objs if o.type == 'MESH']
        check(f"inventor {name} Körper", len(meshes) == bodies, len(meshes))
        check(f"inventor {name} Namen", info["names_ok"] and not any(o.name.startswith("Component") for o in meshes))
        count = sum(len(o.data.polygons) for o in meshes)
        check(f"inventor {name} Dreiecke", count <= tris and count > tris * 0.99, count)
        check(f"inventor {name} Materialien", all(o.data.materials for o in meshes))
        size = max(max(o.dimensions) for o in meshes)
        check(f"inventor {name} Größe", 0 < size < INV.mm_to_bu(sc, 500), size)
        for o in objs:
            bpy.data.objects.remove(o)
        for c in set(bpy.data.collections) - before:
            bpy.data.collections.remove(c)
    # iProperties: Baugruppe an der Wurzel, Bauteil-Datei neben der .iam gefunden -> iProperties + Darstellungsname
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for name in ("iam/00029-007-000 OBD Stecker Satrotec.iam", "ipt/00029-007-002.ipt"):
            shutil.copy(os.path.join(folder, name), tmp)
        before = set(bpy.data.collections)
        objs, info = INV.build(ctx, os.path.join(tmp, "00029-007-000 OBD Stecker Satrotec.iam"))
        root = next(c for c in set(bpy.data.collections) - before if c.name.startswith("00029-007-000"))
        check("iam iProperties Baugruppe", root.get("Part Number") == "00029-007-000" and root.get("Description") == "PKW",
              dict(root.items()))
        pins = [o for o in objs if o.get("Part Number") == "00029-007-002"]
        check("iam iProperties Bauteil", pins and info["part_files"] == 1 and pins[0].get("Material") ==
              "Nickel-Kupfer-Legierung 400", (len(pins), info["part_files"]))
        names = {m.name for o in pins for m in o.data.materials}
        check("iam Darstellungsname", "Chrom - poliert" in names, names)
        for o in objs:
            bpy.data.objects.remove(o)
        for c in set(bpy.data.collections) - before:
            bpy.data.collections.remove(c)
    objs, info = INV.build(ctx, os.path.join(folder, "iam/00029-007-000 OBD Stecker Satrotec.iam"), iproperties=False)
    check("iam ohne iProperties", not any(o.get("Part Number") for o in objs))
    for o in objs:
        bpy.data.objects.remove(o)
    try:
        INV.build(ctx, os.path.join(folder, "ipt/00029-007-002.ipt"))
        check("inventor ipt erkannt", False, "kein Fehler")
    except INV.inventor_data.PartFileError:
        check("inventor ipt erkannt", True)
    # Bauteile (.ipt) aus dem Anzeigenetz: Körper, Dreiecke, Farben je Fläche (Sollfarben vom Nutzer belegt)
    blue, cyan, beige, alu = (0.094, 0.145, 0.973), (0.188, 0.529, 0.89), (0.905, 0.839, 0.675), (0.957, 0.957, 0.957)
    for name, bodies, tris, colors in (("ipt/00029-007-002.ipt", 1, 464, {alu}), ("ipt/simple0002.ipt", 1, 634, None),
                                       ("ipt/test mb color.ipt", 3, 6528, {blue, cyan, beige, alu}),
                                       # neuere Datei: Normalen-Array t+6, Teilfarbe hinter den Flächen
                                       ("ipt/ATEMITECH_rugged usb c female.ipt", 6, 9561, {(0.376, 0.376, 0.376)})):
        before = set(bpy.data.collections)
        objs, info = INV.build_part(ctx, os.path.join(folder, name))
        meshes = [o for o in objs if o.type == 'MESH']
        check(f"ipt {name} Körper", len(meshes) == bodies, len(meshes))
        count = sum(len(o.data.polygons) for o in meshes)
        check(f"ipt {name} Dreiecke", count == tris, count)
        size = max(max(o.dimensions) for o in meshes)
        check(f"ipt {name} Größe", INV.mm_to_bu(sc, 1) < size < INV.mm_to_bu(sc, 500), size)
        found = {tuple(round(c, 3) for c in m.diffuse_color[:3]) for o in meshes for m in o.data.materials}
        if colors:
            check(f"ipt {name} Farben", colors <= found, found)
        check(f"ipt {name} Normalen", all(o.data.has_custom_normals for o in meshes))
        number = {"ipt/ATEMITECH_rugged usb c female.ipt": "USB_C"}.get(name, os.path.splitext(os.path.basename(name))[0])
        check(f"ipt {name} iProperties", all(o.get("Part Number") == number for o in meshes),
              [o.get("Part Number") for o in meshes])
        labels = {m.name for o in meshes for m in o.data.materials}
        expect = {"ipt/00029-007-002.ipt": {"Chrom - poliert"},
                  "ipt/test mb color.ipt": {"Aluminium - poliert", "Cyan", "Beige"}}.get(name, set())
        check(f"ipt {name} Materialnamen", expect <= labels, labels)
        for o in objs:
            bpy.data.objects.remove(o)
        for c in set(bpy.data.collections) - before:
            bpy.data.collections.remove(c)


ZSTD_SAMPLE = bytes.fromhex(
    "28b52ffd60e802850600c6193113b02585030d0c98207840672b2222302b48ca09300028002900b6b3f68c9486f513fdcd2e2b426a29af"
    "31efbf2f639117ffa369a67cdc23c846141e078541803030202014868082012067425574237c847bfeded17e963b336283d682b6f49965"
    "17bdf3dca89344ed70f6f871fbf2d5e35f5c44d615aa9d9d57c9c4ef709e87333dd3cdbabfe539945094258ebc6c1e59398974a2b3f2b0"
    "53240195a4cf59b3952c4546ae4830be303d3e9ab22f3d768b086d5f7ce775df7d70d41c89511b8a9c5dca0302003e00222352cd3a")


def _vals(rig, M):
    return INV.inventor_live._read(rig, M)


def _loop_mm(M, q, mm):
    """Größter Lagefehler aller Schleifen in mm"""
    return max((p / mm for _n, p, _a in M.loop_errors(q)), default=0.0)


def test_inventor_rig():
    """Inventor-Joints -> Armature (testdaten/inventor/iam/joints: eigene Testbaugruppe, Inventor 2026, Zstandard)"""
    Z = sys.modules[PKG + ".inventor_zstd"]
    out = Z.decompress(ZSTD_SAMPLE)        # mit Python 3.14 compression.zstd (Stufe 19) erzeugt: Huffman + FSE
    check("zstd Probe", len(out) == 1000 and out.startswith(b"Inventor joint " * 40) and set(out[600:]) <= set(b"ABCDEFGH"),
          len(out))
    RIG, J = INV.inventor_rig, INV.inventor_rig.inventor_joints
    folder = "I:/GIT/FORMLYNX/testdaten/inventor/iam"

    # Künstliches Viergelenk (Steg, Kurbel, Koppel, Schwinge), alle Achsen Z: Schleife muss per IK schließen
    pts = {"Steg:1": (0, 0), "Kurbel:1": (0, 0), "Koppel:1": (10, 30), "Schwinge:1": (40, 0)}
    joint_pts = [("J1", "Steg:1", "Kurbel:1", (0, 0)), ("J2", "Kurbel:1", "Koppel:1", (10, 30)),
                 ("J3", "Koppel:1", "Schwinge:1", (50, 35)), ("J4", "Schwinge:1", "Steg:1", (40, 0))]
    sc_mm = INV.mm_to_bu(sc, 1.0)
    occs, objs = {}, []
    for name, (x, y) in pts.items():
        o = bpy.data.objects.new(name, bpy.data.meshes.new(name))
        sc.collection.objects.link(o)
        o.matrix_world = Matrix.Translation((x * sc_mm, y * sc_mm, 0))
        objs.append(o)
        occs[RIG._key(name)] = RIG.Occurrence(name, [o], o.matrix_world.copy())

    def frame(at, occ):
        m = np.eye(4)
        m[:2, 3] = (at[0] - pts[occ][0], at[1] - pts[occ][1])
        return m
    fake = [dict(name=n, kind='REVOLUTE', style=0x0E, values=[0.0, 0.0],
                 sides=[(a, frame(at, a)), (b, frame(at, b))]) for n, a, b, at in joint_pts]
    real_read = J.read
    J.read = lambda cfb: fake
    try:
        rig, info = RIG.build(ctx, None, occs, sc.collection, "Viergelenk Rig", sc_mm)
    finally:
        J.read = real_read
    check("rig Schleife erkannt", info["loops"] == 1 and info["joints"] == 3, info)
    LIVE = INV.inventor_live
    mm = INV.mm_to_bu(sc, 1.0)
    M = LIVE.inventor_solver.Model(json.loads(rig["inventor_solver"]))
    check("rig Modell vorhanden", len(M.loops) == 1 and len(M.vars) == 3 and
          not any(b.name.startswith(("TIP ", "PIN ")) for b in rig.pose.bones), (len(M.loops), len(M.vars)))
    # Antrieb (erste Variable, die die Schleife berührt) um 15 drehen, Schleife muss geschlossen bleiben
    driver = rig.pose.bones[M.bones[M.vars[0][0]]["name"]]
    before = _vals(rig, M)
    driver.rotation_euler = (0, math.radians(15), 0)
    LIVE.solve_rig(rig, sc)
    q = _vals(rig, M)
    check("rig Schleife geschlossen", _loop_mm(M, q, sc_mm) < 0.05, _loop_mm(M, q, sc_mm))
    check("rig Schleife bewegt", max(abs(q[n] - before[n]) for n in range(1, len(q))) > math.radians(1), (before, q))
    # Live per Handler: Wert setzen + Depsgraph-Update löst die Schleife ohne Aufruf von solve_rig
    LIVE._last[rig.as_pointer()] = _vals(rig, M)
    driver.rotation_euler = (0, math.radians(30), 0)
    ctx.view_layer.update()
    q2 = _vals(rig, M)
    check("rig Handler schließt Schleife", _loop_mm(M, q2, sc_mm) < 0.05 and abs(q2[0] - math.radians(30)) < 1e-5 and
          max(abs(q2[n] - q[n]) for n in range(1, len(q2))) > math.radians(1), (_loop_mm(M, q2, sc_mm), q, q2))
    for o in objs + [rig]:
        bpy.data.objects.remove(o)

    if not os.path.isdir(os.path.join(folder, "joints")):
        print("testdaten/inventor/iam/joints fehlt – Joint-Test übersprungen")
        return
    path = os.path.join(folder, "joints/Baugruppe2.iam")
    cfb = INV.inventor_data.Cfb(open(path, "rb").read())
    found = {j["name"]: j for j in J.read(cfb)}
    expect = {"Drehbar:1": 'REVOLUTE', "Zylindrisch:1": 'CYLINDRICAL', "Passend:1": 'MATE', "Fluchtend:1": 'FLUSH',
              "Tangential:1": 'TANGENT', "Planar:1": 'PLANAR', "Test_Drehen_90": 'REVOLUTE', "Kugelförmig:1": 'BALL',
              "Starr:1": 'RIGID', "Symmetrie:1": 'SYMMETRY'}
    check("joints Arten", all(found.get(n, {}).get("kind") == k for n, k in expect.items()),
          {n: j["kind"] for n, j in found.items()})
    check("joints Werte", abs(found["Zylindrisch:1"]["values"][1] - 2.6021) < 1e-3 and
          abs(found["Test_Drehen_90"]["values"][2] - math.pi / 2) < 1e-6, found["Zylindrisch:1"]["values"])
    before = set(bpy.data.collections)
    objs, info = INV.build(ctx, path)
    check("rig Namen 2026", info["names_ok"], info)
    rig = next((o for o in objs if o.type == 'ARMATURE'), None)
    check("rig angelegt", rig is not None and info["joints"] == 5 and info["constraints"] == 5, info)
    pb = rig.pose.bones
    check("rig Drehgelenk Sperren", (pb["Drehbar:1"].lock_ik_x, pb["Drehbar:1"].lock_ik_y, pb["Drehbar:1"].lock_ik_z) ==
          (True, False, True) and tuple(pb["Drehbar:1"].lock_location) == (True, True, True))
    check("rig Zylinder schiebt längs", tuple(pb["Zylindrisch:1"].lock_location) == (True, False, True))
    check("rig Planar schiebt in der Ebene", tuple(pb["Planar:1"].lock_location) == (False, True, False))
    check("rig Kugel frei", not any((pb["Kugelförmig:1"].lock_ik_x, pb["Kugelförmig:1"].lock_ik_y, pb["Kugelförmig:1"].lock_ik_z)))
    check("rig Starr ohne Bone", "Starr:1" not in pb)
    lim = pb["Test_Drehen_90"]
    check("rig Grenzen", lim.use_ik_limit_y and abs(lim.ik_min_y + math.pi / 2) < 1e-5 and abs(lim.ik_max_y) < 1e-6,
          (lim.ik_min_y, lim.ik_max_y))
    # Ohne Schleife: kein Löser-Modell, die IK-Steuerung am Kettenende bleibt (Schleifen-Ketten haben keine, siehe Viergelenk)
    check("rig IK-Kette", [c.chain_count for c in pb["Kugelförmig:1"].constraints if c.type == 'IK'] == [3])
    check("rig ohne Schleife kein Modell", "inventor_solver" not in rig and info["loops"] == 0, info)
    # Ohne IK: Bone um -45° -> Inventor-Winkel +45°, Gelenkursprünge bleiben deckungsgleich
    for c in (c for b in pb for c in b.constraints if c.type == 'IK'):
        c.mute = True
    lim.rotation_euler = (0, math.radians(-45), 0)
    ctx.view_layer.update()
    j = found["Test_Drehen_90"]
    by_occ = {o.get("mlx_inventor_occurrence"): o for o in objs if o.type == 'MESH'}
    names = INV.inventor_data.read(path)["names"]
    world = {}
    for top, entry in names.items():
        world[RIG._key(entry["name"])] = by_occ[top].matrix_world
    (na, fa), (nb, fb) = j["sides"]
    wa = world[RIG._key(na)] @ RIG._scaled(fa, sc_mm)
    wb = world[RIG._key(nb)] @ RIG._scaled(fb, sc_mm)
    xa, xb, za = wa.col[0].xyz, wb.col[0].xyz, wa.col[2].xyz
    angle = math.degrees(math.atan2(xa.cross(xb).dot(za), xa.dot(xb)))
    check("rig Drehsinn wie Inventor", abs(angle - 45) < 1e-3, angle)
    check("rig Gelenk bleibt zusammen", (wa.translation - wb.translation).length < INV.mm_to_bu(sc, 1e-4))
    for o in objs:
        bpy.data.objects.remove(o)
    for c in set(bpy.data.collections) - before:
        bpy.data.collections.remove(c)
    # Ohne Joints: kein Rig
    objs, info = INV.build(ctx, path, joints=False)
    check("rig abschaltbar", not any(o.type == 'ARMATURE' for o in objs))
    for o in objs:
        bpy.data.objects.remove(o)
    for c in set(bpy.data.collections) - before:
        bpy.data.collections.remove(c)

    # Parameter: Blechteil gegen Inventors Parameter-Export (XML, gleiche Reihenfolge und Werte)
    blech = "I:/GIT/FORMLYNX/testdaten/inventor/ipt/blech"
    if not os.path.isdir(blech):
        print("testdaten/inventor/ipt/blech fehlt – Parameter-Test übersprungen")
        return
    P = sys.modules[PKG + ".inventor_params"]
    params = P.read_cfb(INV.inventor_data.Cfb(open(os.path.join(blech, "Bauteil5.ipt"), "rb").read()))
    xml = re.findall(r"<name>(.*?)</name>\s*<typeCode>.*?</typeCode>\s*<value>(.*?)</value>",
                     open(os.path.join(blech, "Bauteil5-params.xml"), encoding="utf-8").read(), re.S)
    check("param Namen wie Inventor", [p["name"] for p in params] == [n for n, _v in xml], [p["name"] for p in params])
    wrong = []
    for p, (n, v) in zip(params, xml):
        num = re.match(r"([-\d.]+) (mm|grd)$", v)
        if (abs(p["value"] - float(num.group(1))) > 1e-6) if num else p["expression"] != v:
            wrong.append((n, p["value"], p["expression"], v))
    check("param Werte und Formeln", not wrong, wrong)
    by_name = {p["name"]: p for p in params}
    check("param Formel mit Faktor", by_name["TiefeBiegungsfreistellung"]["expression"] == "Stärke * 0.5")
    check("param Winkel in Grad", by_name["d8"]["unit"] == "deg" and abs(by_name["d8"]["value"] - 90) < 1e-9)
    before = set(bpy.data.collections)
    objs, info = INV.build_part(ctx, os.path.join(blech, "Bauteil5.ipt"))
    group = objs[0].get("Inventor Parameters")
    check("param am Objekt", group is not None and abs(group["Stärke"] - 2.0) < 1e-9 and
          objs[0]["Inventor Parameter Formulas"]["Biegeradius"] == "Stärke", dict(group or {}))
    for o in objs:
        bpy.data.objects.remove(o)
    for c in set(bpy.data.collections) - before:
        bpy.data.collections.remove(c)


def test_inventor_rottrans():
    """Bewegungs-Abhängigkeit Drehung/Translation und Materialien (testdaten/inventor/iam/rottrans: GrabCAD, nur lokal)"""
    path = "I:/GIT/FORMLYNX/testdaten/inventor/iam/rottrans/Ensamblaje1.iam"
    if not os.path.isfile(path):
        print("testdaten/inventor/iam/rottrans fehlt – Drehung/Translation-Test übersprungen")
        return
    J = INV.inventor_rig.inventor_joints
    found = J.read(INV.inventor_data.Cfb(open(path, "rb").read()))
    rt = [j for j in found if j["kind"] == 'ROTATE_TRANSLATE']
    check("rottrans erkannt", len(rt) == 1 and rt[0]["name"] == "Drehung/Translation:1",
          [(j["name"], j["kind"]) for j in found])
    # Inventor speichert cm: 1 mm Weg je Umdrehung
    check("rottrans 1 mm je Umdrehung", rt and len(rt[0]["values"]) == 1 and abs(rt[0]["values"][0] * 10 - 1.0) < 1e-9,
          rt and rt[0]["values"])
    check("rottrans Teile", rt and sorted(s[0] for s in rt[0]["sides"]) == ["Perno:1", "Tuerca:1"], rt and rt[0]["sides"])
    check("rottrans keine unbekannte Art", not any(j["kind"] == 'UNKNOWN' for j in found))
    before = set(bpy.data.collections)
    objs, info = INV.build(ctx, path)
    check("rottrans Import", info["names_ok"] and info["occurrences"] == 8 and info["part_files"] == 5, info)
    mats = {o.get("Material") for o in objs if o.type == 'MESH'}
    check("rottrans Materialien", mats == {"Stahl", "Stahl, Legierung", "Messing, weicher Gelbton"}, mats)
    # Cache ohne Farbe für die Teile: Teildarstellung aus der .ipt statt IAM_default
    slots = {o.name.split(" – ")[0].split(".")[0]: o.data.materials[0] for o in objs if o.type == 'MESH'}
    brass = slots.get("Pasador")
    check("rottrans Messing gelb", brass is not None and brass.name == "Messing - satiniert" and
          abs(brass.diffuse_color[0] - 0.796) < 1e-3 and brass.diffuse_color[2] < 0.3, brass and brass.name)
    check("rottrans keine Standardfarbe", not any(m.name.startswith("IAM_default") for m in slots.values()),
          {k: m.name for k, m in slots.items()})
    rig = next((o for o in objs if o.type == 'ARMATURE'), None)
    check("rottrans Rig", rig is not None and info["couplings"] == 1 and info["loops"] == 1, info)
    # Mutter/Bolzen: Teile mit Arbeitsebene als Nebenkörper bekommen trotzdem eine Weltlage -> Joint Zylindrisch:14
    pb = rig.pose.bones
    check("rottrans Kopplung am Joint", "SCREW Zylindrisch:14" in pb and
          abs(pb["Zylindrisch:14"].get("Inventor pitch mm", 0) - 1.0) < 1e-9, [b.name for b in pb])
    # Eine Umdrehung (FK, Schleifen-IK stumm): Bolzen wandert 1 mm längs der Gelenkachse gegenüber der Mutter
    for c in (c for b in pb for c in b.constraints if c.type == 'IK'):
        c.mute = True
    ctx.view_layer.update()
    bolt = next(o for o in objs if o.type == 'MESH' and o.parent_bone == "SCREW Zylindrisch:14")
    joint = pb["Zylindrisch:14"]
    axis = (rig.matrix_world @ joint.tail - rig.matrix_world @ joint.head).normalized()
    p0 = bolt.matrix_world.translation.copy()
    joint.rotation_euler = (0, 2 * math.pi, 0)
    ctx.view_layer.update()
    shift = (bolt.matrix_world.translation - p0).dot(axis) / INV.mm_to_bu(sc, 1.0)
    check("rottrans 1 mm je Umdrehung im Rig", abs(abs(shift) - 1.0) < 1e-4, shift)
    joint.rotation_euler = (0, math.pi, 0)
    ctx.view_layer.update()
    half = (bolt.matrix_world.translation - p0).dot(axis) / INV.mm_to_bu(sc, 1.0)
    check("rottrans halbe Umdrehung", abs(half - shift / 2) < 1e-4, half)
    # Schleifen-Löser: Zylindrisch:14 schrittweise auf 1890 Grad (5,25 Umdrehungen) drehen -> alle Schleifen
    # schließen, Schraubengelenk 14 wandert +5,25 mm (float32 beim Drehwinkel)
    LIVE = INV.inventor_live
    mm = INV.mm_to_bu(sc, 1.0)
    M = LIVE.inventor_solver.Model(json.loads(rig["inventor_solver"]))
    joint.rotation_euler = (0, 0, 0)
    drive = pb["Zylindrisch:14"]
    drive.rotation_mode = 'XYZ'
    LIVE._last.pop(rig.as_pointer(), None)
    for deg in range(30, 1891, 30):
        drive.rotation_euler = (0, math.radians(deg), 0)
        LIVE.solve_rig(rig, sc)
    q = _vals(rig, M)
    errs = M.loop_errors(q)
    check("rottrans Schleifen geschlossen", errs and all(p / mm < 0.05 and a < 0.1 for _n, p, a in errs),
          [(n, p / mm, a) for n, p, a in errs])
    ctx.view_layer.update()
    shift = pb["SCREW Zylindrisch:14"].location[1] / mm
    check("rottrans Schraube 5,25 mm", abs(shift - 5.25) < 0.02, shift)
    check("rottrans Antrieb gehalten", abs(math.degrees(drive.rotation_euler[1]) - 1890) < 0.01, math.degrees(drive.rotation_euler[1]))
    for o in objs:
        bpy.data.objects.remove(o)
    for c in set(bpy.data.collections) - before:
        bpy.data.collections.remove(c)


def test_inventor_loops():
    """Geschlossene Kette mit Schraubenantrieb (testdaten/inventor/iam/rottrans2: rottrans mit Gelenken statt
    Abhängigkeiten, vom Nutzer umgebaut): Perno drehen -> Mutter wandert, Arme folgen, alle Joints bleiben geschlossen"""
    path = "I:/GIT/FORMLYNX/testdaten/inventor/iam/rottrans2/Ensamblaje1.iam"
    if not os.path.isfile(path):
        print("testdaten/inventor/iam/rottrans2 fehlt – Schleifen-Test übersprungen")
        return
    before = set(bpy.data.collections)
    objs, info = INV.build(ctx, path)
    rig = next((o for o in objs if o.type == 'ARMATURE'), None)
    check("loops Arbeitsebenen nicht als unvollständig gemeldet", info["skipped"] == 0, info["skipped"])
    check("loops Rig", rig is not None and info["loops"] == 4 and info["couplings"] == 1 and info["joints"] == 7, info)
    pb = rig.pose.bones
    check("loops Perno als Bone", "Zylindrisch:13" in pb and "SCREW Zylindrisch:14" in pb, [b.name for b in pb])
    LIVE = INV.inventor_live
    mm = INV.mm_to_bu(sc, 1.0)
    M = LIVE.inventor_solver.Model(json.loads(rig["inventor_solver"]))
    LIVE._last.pop(rig.as_pointer(), None)
    drive = pb["Zylindrisch:13"]
    for deg in range(30, 3781, 30):         # 10,5 Umdrehungen in Schritten wie beim Ziehen
        drive.rotation_euler = (0, math.radians(deg), 0)
        LIVE.solve_rig(rig, sc)
    LIVE._set_pitch(rig, M, sc)
    errs = M.loop_errors(_vals(rig, M))
    check("loops geschlossen", errs and all(p / mm < 0.05 and a < 0.1 for _n, p, a in errs),
          [(n, p / mm, a) for n, p, a in errs])
    ctx.view_layer.update()
    shift = pb["SCREW Zylindrisch:14"].location[1] / mm
    check("loops Mutter 10,5 mm", abs(abs(shift) - 10.5) < 0.05, shift)
    arm = pb["Drehbar:7"].rotation_euler[1]
    check("loops Arm schwenkt", abs(arm) > math.radians(1), math.degrees(arm))
    for o in objs:
        bpy.data.objects.remove(o)
    for c in set(bpy.data.collections) - before:
        bpy.data.collections.remove(c)


# Admin-Dashboard: FLX_ONLY=test_a,test_b wählt einzelne Tests; @@TEST-Zeilen melden den Fortschritt live
FLX_ONLY = set(filter(None, os.environ.get("FLX_ONLY", "").split(",")))


def flx_run(name, func):
    if FLX_ONLY and name not in FLX_ONLY:
        return
    import time as _t
    n, t0 = len(FAILS), _t.time()
    print(f"@@TEST {name} START", flush=True)
    try:
        func()
    except Exception:
        check(name, False, traceback.format_exc())
    bad = len(FAILS) - n
    print(f"@@TEST {name} {'FAIL' if bad else 'OK'} {_t.time() - t0:.1f} 0 {bad}", flush=True)


try:
    for test in (test_extension, test_inventor_import, test_inventor_rig, test_inventor_rottrans, test_inventor_loops):
        flx_run(test.__name__, test)
    bpy.ops.preferences.addon_disable(module=PKG)
    check("Extension sauber aus", not hasattr(bpy.types, "IVX_OT_import_inventor") and not menu_entries(), menu_entries())
except Exception:
    traceback.print_exc()
    FAILS.append("Ausnahme")
print(f"ERGEBNIS {'OK' if not FAILS else 'FEHLER ' + str(len(FAILS))}")
sys.stdout.flush()
os._exit(1 if FAILS else 0)

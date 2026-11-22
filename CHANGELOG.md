# Changelog

## Unreleased

**New**
- **Rotation-translation** constraints (e.g. screw and nut) move the rig: turning the joint bone slides the part by the pitch from Inventor. The pitch is a property of the joint bone and can be changed or reversed there.

**Fixed**
- Joints on parts that carry a work plane or a second small body were skipped. They now become bones.
- A closed loop that ends in a cylindrical or slider joint now lets the part slide along that joint instead of holding it at one point.
- Parts whose colour the assembly does not store (e.g. brass, steel) got the default grey. They now get the appearance from their part file.

## 1.1.0 – 2026-11-22

**New**
- **Joints as rig**: Inventor joints of an assembly become an armature with one bone per joint. Revolute, cylindrical, planar, ball and rigid joints get their degrees of freedom as bone locks, joint limits as IK limits. Closed loops are kept closed by an IK target. Option *Joints as Rig*, on by default.
- **iProperties** as custom properties, for assemblies also from their part files.
- **Parameters** of parts (model, user and sheet metal) as custom properties with units and formulas.
- Material names from the Inventor appearances.

**Fixed**
- After an import only the top object is selected. Thousands of selected parts made switching workspaces slow.
- Files saved by **Inventor 2026** now import with part names. Inventor 2026 compresses its data with Zstandard, which the extension now unpacks itself.

**Known limits**
- Slider joints and distance limits of slider and cylindrical joints are not confirmed against real files yet.
- Assembly constraints (mate, flush, tangent, symmetry) are read but do not restrict the rig yet.

## 1.0.0

First release: Inventor assemblies (.iam) and parts (.ipt) as meshes from the display data, part names from the model browser, collections, empties or flat, materials from part colours, English and German.

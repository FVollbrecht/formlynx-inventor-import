# Changelog

## 1.1.0 – unreleased

**New**
- **Joints as rig**: Inventor joints of an assembly become an armature with one bone per joint. Revolute, cylindrical, planar, ball and rigid joints get their degrees of freedom as bone locks, joint limits as IK limits. Closed loops are kept closed by an IK target. Option *Joints as Rig*, on by default.
- **iProperties** as custom properties, for assemblies also from their part files.
- **Parameters** of parts (model, user and sheet metal) as custom properties with units and formulas.
- Material names from the Inventor appearances.

**Fixed**
- Files saved by **Inventor 2026** now import with part names. Inventor 2026 compresses its data with Zstandard, which the extension now unpacks itself.

**Known limits**
- Slider joints and distance limits of slider and cylindrical joints are not confirmed against real files yet.
- Assembly constraints (mate, flush, tangent, symmetry) are read but do not restrict the rig yet.

## 1.0.0

First release: Inventor assemblies (.iam) and parts (.ipt) as meshes from the display data, part names from the model browser, collections, empties or flat, materials from part colours, English and German.

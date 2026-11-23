# Changelog

## 1.1.1 – 2026-11-23

**New**
- **Closed loops are solved**: mechanisms with closed chains (linkages, screw and nut with arms) now move as a whole. Turning any joint bone moves the dependent joints so that every joint stays closed, like in Inventor. Duplicate joints between the same two parts are allowed. *Bake Inventor Rig* (Pose menu) writes the motion to keyframes for export.
- **Sketches and work features of parts**: importing a part (.ipt) also brings its 2D and 3D sketches as curves (lines, arcs, splines, curves projected onto faces), work planes, the origin planes and axes and the center point, in their own collection and only for viewing (not rendered). *Reference Geometry* chooses what: what is visible in Inventor (default), all (hidden ones come in hidden) or none. Parts made only of sketches and surfaces can now be imported.
- **Decals on parts**: images placed on a part (.ipt) in Inventor come in as textured patches on the faces, on flat faces and wrapped around cylinders. The image embedded in the part file is packed into the .blend (PNG and GIF).
- **Rotation-translation** constraints (e.g. screw and nut) move the rig: turning the joint bone slides the part by the pitch from Inventor. The pitch is a property of the joint bone and can be changed or reversed there.

**Fixed**
- Joints on parts that carry a work plane or a second small body were skipped. They now become bones.
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

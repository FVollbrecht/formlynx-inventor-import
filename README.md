# FORMLYNX Inventor Import

A Blender extension that imports **Autodesk Inventor assemblies (.iam) and parts (.ipt)** as meshes – no Inventor installation, no OpenCASCADE, no internet connection.

The importer reads the display data that Inventor saves inside every file. You get the shapes and colours you saw in Inventor: assemblies with the part names from the model browser, parts with one object per solid body and the colours of single faces.

## Features
- **File › Import › Inventor (.iam, .ipt)**, several files at once
- Part names taken from the Inventor model browser (e.g. `Bracket:2`)
- Assembly structure as **collections**, as **empties** (parts parented per assembly) or **flat** in one collection
- **Materials** from the part colours (optional)
- Parts (.ipt): one object per solid or surface body, colours per face, per feature and per part
- Identical parts share one mesh – large assemblies stay light
- Correct scale in millimetres, respecting the scene unit scale
- Up axis: automatic (Z-up like Inventor) or Y-up files rotated to Z-up
- Smooth shading kept via custom normals
- Pure Python, works offline, reads only the files you pick
- English and German interface

## Limits
- A part without saved display data cannot be imported; the import names it and continues with the other files.
- Geometry comes from the display cache, so it is a mesh, not an editable CAD model. The level of detail is the one Inventor used for display.
- Tested with files from Inventor 2023 and 2025. The file format is not documented; if a file cannot be read, the import names it and continues with the other files.

## Requirements
Blender 4.5 or newer.

## Installation
1. Download the zip from the [Releases](../../releases) page.
2. In Blender: **Edit › Preferences › Get Extensions**, menu **⌄ › Install from Disk…**, pick the zip.

## How to use
1. **File › Import › Inventor (.iam, .ipt)**
2. Pick one or more `.iam` or `.ipt` files and choose how assemblies should be organised.

## Problems and questions
Please open an [issue](../../issues). Name the Inventor version that saved the file and what went wrong. Attach a file only if you are allowed to share it.

## Source
This repository holds the source of the released extension. It is generated from the author's main code base, so pull requests are welcome, but changes are carried over by hand.

## License
GPL-3.0-or-later, see [LICENSE](LICENSE).

Autodesk and Inventor are registered trademarks of Autodesk, Inc. This extension is not affiliated with or endorsed by Autodesk.

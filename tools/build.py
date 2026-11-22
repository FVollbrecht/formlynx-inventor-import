"""Paket der Extension bauen: python -X utf8 tools/build.py [--tag]

-> dist/formlynx_inventor_import-<version>.zip (über `blender --command extension build`, Blender aus FLX_BLENDER
   oder Standardpfad 5.2). tests/, tools/ und dist/ bleiben laut Manifest draußen.
--tag: Tag v<version> auf den aktuellen Commit setzen (Arbeitskopie muss sauber sein). Push, GitHub-Release und
       Upload bei extensions.blender.org bleiben Handarbeit.
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIST = os.path.join(ROOT, "dist")
BLENDER = os.environ.get("FLX_BLENDER") or r"I:\Program Files\Blender Foundation\Blender 5.2\blender.exe"


def version():
    text = open(os.path.join(ROOT, "blender_manifest.toml"), encoding="utf-8").read()
    return re.search(r'^version = "(.*)"$', text, re.M).group(1)


def git(*args):
    r = subprocess.run(["git", "-C", ROOT, *args], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        sys.exit(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def main():
    os.makedirs(DIST, exist_ok=True)
    r = subprocess.run([BLENDER, "-b", "--factory-startup", "--command", "extension", "build",
                        "--source-dir", ROOT, "--output-dir", DIST], capture_output=True, text=True, timeout=120)
    out = (r.stdout + r.stderr).strip().splitlines()
    print("\n".join(line for line in out if "ERROR" in line or "created" in line.lower()) or (out[-1] if out else ""))
    if r.returncode or any("FATAL_ERROR" in line for line in out):
        sys.exit(r.returncode or 1)
    if "--tag" in sys.argv:
        if git("status", "--porcelain"):
            sys.exit("Arbeitskopie hat ungesicherte Änderungen – erst committen")
        tag = f"v{version()}"
        if git("tag", "--list", tag):
            sys.exit(f"{tag} gibt es schon – Version im Manifest erhöhen")
        git("tag", "-a", tag, "-m", f"FORMLYNX Inventor Import {version()}")
        print(f"Tag {tag} gesetzt – noch nicht gepusht (git push --follow-tags)")


if __name__ == "__main__":
    main()

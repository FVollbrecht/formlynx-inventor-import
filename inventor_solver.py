# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
"""Schleifen-Löser für Inventor-Rigs – ohne bpy.

Wie in Robotik-Werkzeugen (MuJoCo-Equality, onshape-to-robot): Gelenke bilden einen Baum (die Armature), jede
Schleife wird eine Schließ-Bedingung. Unbekannte sind die Gelenkwerte der Bones (reduzierte Koordinaten), nicht
freie Teil-Lagen. Gelöst mit Levenberg-Marquardt (kleinste Quadrate): doppelte Joints zwischen denselben Teilen
(überbestimmt) sind damit kein Fehler, solange sie verträglich sind.

Modell (JSON-fähig, liegt als Text an der Armature):
  bones: [dict(name, parent (Index oder -1), rel (16 Floats: Ruhelage relativ zum Eltern-Bone), vars, screw, lim)]
         vars: Gelenkwerte des Bones aus 'ry', 'lx', 'ly', 'lz', 'qx', 'qy', 'qz' (Kugel: Drehvektor)
         screw: Index des Bones, dessen Drehung diesen Bone längs Y schiebt (Drehung/Translation), sonst -1
  loops: [dict(name, kind, a, la, b, lb, t0)] – Rahmen je Seite im Bone (16 Floats), t0 = Fa⁻¹·Fb in Ruhe
  length: Bezugslänge (Blender-Einheiten), macht Längen- und Winkelfehler vergleichbar
"""
import math

import numpy as np

SLOTS = ('ry', 'lx', 'ly', 'lz', 'qx', 'qy', 'qz')
ANGULAR = {'ry', 'qx', 'qy', 'qz'}
MAX_ITER = 60
TOL = 1e-9                  # Quadratsumme der normierten Residuen


def _m(values):
    return np.array(values, dtype=float).reshape(4, 4)


def flat(m):
    return [float(v) for v in np.asarray(m, dtype=float).reshape(16)]


def _rot_y(a):
    c, s = math.cos(a), math.sin(a)
    return np.array(((c, 0.0, s), (0.0, 1.0, 0.0), (-s, 0.0, c)))


def _rot_vec(v):
    """Drehvektor -> Matrix (Rodrigues)"""
    a = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    if a < 1e-12:
        return np.eye(3)
    k = np.asarray(v, dtype=float) / a
    kx = np.array(((0.0, -k[2], k[1]), (k[2], 0.0, -k[0]), (-k[1], k[0], 0.0)))
    return np.eye(3) + math.sin(a) * kx + (1.0 - math.cos(a)) * kx @ kx


class Model:
    def __init__(self, data):
        self.bones = data["bones"]
        self.loops = data["loops"]
        self.length = float(data.get("length") or 1.0)
        self.rel = [_m(b["rel"]) for b in self.bones]
        self.parent = [b["parent"] for b in self.bones]
        self.screw = [b.get("screw", -1) for b in self.bones]
        self.pitch = [0.0] * len(self.bones)          # Blender-Einheiten je rad, von außen gesetzt
        # Variablen: (Bone-Index, Slot)
        self.vars = [(i, s) for i, b in enumerate(self.bones) for s in b["vars"]]
        self.index = {v: n for n, v in enumerate(self.vars)}
        self.bounds = []
        for i, s in self.vars:
            lim = self.bones[i].get("lim")
            self.bounds.append(tuple(lim) if (lim and s == 'ry') else (-math.inf, math.inf))
        for lp in self.loops:
            lp["_la"], lp["_lb"], lp["_t0"] = _m(lp["la"]), _m(lp["lb"]), _m(lp["t0"])
        # Variablen, die eine Schleife beeinflussen: Bones auf den Wegen bis zum gemeinsamen Vorfahren
        involved = set()
        for lp in self.loops:
            pa, pb = self._path(lp["a"]), self._path(lp["b"])
            common = set(pa) & set(pb)
            involved |= (set(pa) | set(pb)) - common
        self.involved = np.array([i in involved for i, _s in self.vars], dtype=bool)

    def _path(self, i):
        out = []
        while i >= 0:
            out.append(i)
            i = self.parent[i]
        return out

    def poses(self, q):
        """Pose-Matrizen aller Bones im Armature-Raum (Wurzeln in Ruhelage)"""
        out = []
        val = {}
        for n, (i, s) in enumerate(self.vars):
            val.setdefault(i, {})[s] = q[n]
        for i, rel in enumerate(self.rel):
            v = val.get(i, {})
            basis = np.eye(4)
            if 'qx' in v:
                basis[:3, :3] = _rot_vec((v['qx'], v['qy'], v['qz']))
            elif 'ry' in v:
                basis[:3, :3] = _rot_y(v['ry'])
            basis[:3, 3] = (v.get('lx', 0.0), v.get('ly', 0.0), v.get('lz', 0.0))
            if self.screw[i] >= 0:
                basis[1, 3] += val.get(self.screw[i], {}).get('ry', 0.0) * self.pitch[i]
            p = self.parent[i]
            out.append((out[p] if p >= 0 else np.eye(4)) @ rel @ basis)
        return out

    def residual(self, q):
        P = self.poses(q)
        res = []
        inv_len = 1.0 / self.length
        for lp in self.loops:
            fa = P[lp["a"]] @ lp["_la"]
            fb = P[lp["b"]] @ lp["_lb"]
            t = np.linalg.solve(fa, fb)
            t0 = lp["_t0"]
            dt = (t[:3, 3] - t0[:3, 3]) * inv_len
            dz = t[:3, 2] - t0[:3, 2]
            dx = t[:3, 0] - t0[:3, 0]
            kind = lp["kind"]
            if kind == 'REVOLUTE':
                res += [dt, dz]
            elif kind == 'CYLINDRICAL':
                res += [dt[:2], dz]
            elif kind == 'SLIDER':
                res += [dt[:2], dz, dx]
            elif kind == 'PLANAR':
                res += [dt[2:], dz]
            elif kind == 'BALL':
                res += [dt]
            else:                       # RIGID
                res += [dt, dz, dx]
        return np.concatenate(res) if res else np.zeros(0)

    def loop_errors(self, q):
        """Je Schleife (Name, Lagefehler in Blender-Einheiten, Achsfehler in Grad)"""
        out = []
        r = self.residual(q)
        k = 0
        sizes = dict(REVOLUTE=(3, 3), CYLINDRICAL=(2, 3), SLIDER=(2, 6), PLANAR=(1, 3), BALL=(3, 0), RIGID=(3, 6))
        for lp in self.loops:
            nt, na = sizes.get(lp["kind"], (3, 6))
            pos = float(np.linalg.norm(r[k:k + nt])) * self.length
            ang = math.degrees(min(1.0, float(np.linalg.norm(r[k + nt:k + nt + na])))) if na else 0.0
            out.append((lp["name"], pos, ang))
            k += nt + na
        return out

    def solve(self, q0, held):
        """Gelenkwerte so setzen, dass alle Schleifen schließen. held: Maske der vorgegebenen Werte (vom Nutzer
        bewegt oder animiert). -> (q, Rest-Quadratsumme). Startet bei q0 -> nächstgelegene Lösung."""
        q = np.array(q0, dtype=float)
        free = np.flatnonzero(self.involved & ~np.asarray(held, dtype=bool))
        r = self.residual(q)
        cost = float(r @ r)
        if not len(free) or cost < TOL:
            return q, cost
        lo = np.array([self.bounds[n][0] for n in free])
        hi = np.array([self.bounds[n][1] for n in free])
        eps = np.array([1e-7 if self.vars[n][1] in ANGULAR else 1e-7 * self.length for n in free])
        lam = 1e-3
        for _ in range(MAX_ITER):
            J = np.empty((len(r), len(free)))
            for c, n in enumerate(free):
                qq = q.copy()
                qq[n] += eps[c]
                J[:, c] = (self.residual(qq) - r) / eps[c]
            g = J.T @ r
            H = J.T @ J
            improved = False
            for _try in range(12):
                step = np.linalg.solve(H + lam * (np.diag(np.diag(H)) + 1e-9 * np.eye(len(free))), -g)
                qn = q.copy()
                qn[free] = np.clip(q[free] + step, lo, hi)
                rn = self.residual(qn)
                cn = float(rn @ rn)
                if cn < cost:
                    q, r, cost = qn, rn, cn
                    lam = max(lam * 0.3, 1e-12)
                    improved = True
                    break
                lam *= 10.0
            if not improved or cost < TOL:
                break
        return q, cost

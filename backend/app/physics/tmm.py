"""Thin-film transfer-matrix (characteristic-matrix / Abeles) solver.

Computes reflectance ``R``, transmittance ``T`` and absorptance ``A`` of a
stratified isotropic multilayer coating on a semi-infinite substrate, for any
angle of incidence and s/p polarization, with dispersive (and optionally lossy)
layer indices.

Physics reference: Born & Wolf, *Principles of Optics*, and H. A. Macleod,
*Thin-Film Optical Filters* (characteristic matrix in tilted-admittance form).
Clean-room implementation from the published formulation.

Sign convention: complex index ``n - i k`` with ``k >= 0`` for loss; the
physical branch of ``cos(theta)`` is enforced so evanescent/lossy waves decay
into the medium.

Length units: layer thicknesses and wavelength in **nanometres** (they appear
only as the ratio ``d/lambda`` inside the phase, so any consistent unit works).
"""
from __future__ import annotations

import cmath
import math
from dataclasses import dataclass, field

import numpy as np

from .materials import index_of


@dataclass
class Layer:
    """One coating layer.

    Provide either a material name (resolved through the dispersion engine at the
    query wavelength) or a fixed complex index ``n`` (n - i k).  ``thickness_nm``
    is the physical thickness.
    """

    thickness_nm: float
    material: str | None = None
    n: complex | None = None

    def index_at(self, wavelength_nm: float) -> complex:
        if self.material is not None:
            return index_of(self.material, wavelength_nm)
        if self.n is not None:
            return complex(self.n)
        raise ValueError("Layer needs either a material name or an explicit index n")


@dataclass
class Stack:
    """A coating: incident medium, ordered layers (incident side first), substrate."""

    layers: list[Layer] = field(default_factory=list)
    incident: str | complex = "air"
    substrate: str | complex = "fused_silica"

    def incident_index(self, wavelength_nm: float) -> complex:
        return index_of(self.incident, wavelength_nm) if isinstance(self.incident, str) else complex(self.incident)

    def substrate_index(self, wavelength_nm: float) -> complex:
        return index_of(self.substrate, wavelength_nm) if isinstance(self.substrate, str) else complex(self.substrate)


def _cos_theta(n: complex, n0_sin0: complex) -> complex:
    """Physical-branch cos(theta) in a medium of index ``n = n' - i n''``.

    ``n0_sin0 = n_incident * sin(theta_incident)`` is conserved (Snell).  With
    the ``n - ik`` convention the physical (forward) branch is fixed by
    **forward power flow**: ``Re(n*cos) >= 0``. For a purely evanescent wave,
    the forward factor exp(-i kz z) decays when ``Im(n*cos) <= 0``.
    """
    n = complex(n)
    sin2 = (n0_sin0 / n) ** 2
    cos = cmath.sqrt(1.0 - sin2)
    ncos = n * cos
    if ncos.real < 0 or (ncos.real == 0 and ncos.imag > 0):
        cos = -cos
    return cos


def _admittance(n: complex, cos_theta: complex, pol: str) -> complex:
    """Tilted optical admittance in free-space-cancelled units (n*cos or n/cos)."""
    if pol == "s":
        return n * cos_theta
    if pol == "p":
        return n / cos_theta
    raise ValueError("pol must be 's' or 'p'")


def solve_RT(
    layers_n: np.ndarray,
    layers_d_nm: np.ndarray,
    wavelength_nm: float,
    aoi_deg: float,
    pol: str,
    n_incident: complex,
    n_substrate: complex,
) -> tuple[float, float, float]:
    """Core single-wavelength single-polarization solve.

    Returns ``(R, T, A)`` with ``A = 1 - R - T``.

    Assumes a **lossless incident medium** (the closed-form ``R = |r|^2`` and
    ``T = 4 Re(eta0) Re(eta_s)/|eta0 B + C|^2`` are exact only then). The substrate and layers may be
    lossy.  At exactly grazing incidence (90 deg) the physical limit ``R=1,
    T=0`` is returned to avoid the ``n/cos`` singularity for p-polarization.

    Parameters use raw arrays so this stays a tight numeric kernel; higher-level
    callers use :func:`spectrum` / the coating tool.
    """
    if pol not in ("s", "p"):
        raise ValueError("pol must be 's' or 'p'")

    n0 = complex(n_incident)
    ns = complex(n_substrate)
    theta0 = math.radians(aoi_deg)
    if abs(math.cos(theta0)) < 1e-12:
        return 1.0, 0.0, 0.0  # grazing incidence: total reflection in the paraxial limit
    n0_sin0 = n0 * math.sin(theta0)

    cos0 = _cos_theta(n0, n0_sin0)
    coss = _cos_theta(ns, n0_sin0)
    eta0 = _admittance(n0, cos0, pol)
    # A scaled forward substrate field keeps the p admittance finite at the
    # critical angle. Its normal flux is Re(conj(E) H) in both polarizations.
    exit_e, exit_h = (1.0, ns * coss) if pol == "s" else (coss, ns)

    # Characteristic matrix of the stack (product of layer matrices).
    m11, m12, m21, m22 = 1.0, 0.0, 0.0, 1.0
    log_scale = 0.0
    wavenumber = math.tau / float(wavelength_nm)
    for n_layer, d_nm in zip(layers_n, layers_d_nm):
        n_layer = complex(n_layer)
        cosj = _cos_theta(n_layer, n0_sin0)
        phase_scale = wavenumber * float(d_nm)
        delta = phase_scale * n_layer * cosj
        attenuation = abs(delta.imag)
        if attenuation > 1:
            # Evaluate exp(±iδ) after removing their common positive scale.
            # This retains the reflected field even when transmission underflows.
            forward = cmath.exp(complex(-delta.imag - attenuation, delta.real))
            backward = cmath.exp(complex(delta.imag - attenuation, -delta.real))
            cd, sd = (forward + backward) / 2, (forward - backward) / (2j)
            log_scale += attenuation
        else:
            cd, sd = cmath.cos(delta), cmath.sin(delta)
        # sin(delta)/delta approaches 1 at normal wavevector cutoff.
        factor = phase_scale * (sd / delta if delta != 0 else 1.0)
        if pol == "s":
            b_layer, c_layer = factor, factor * (n_layer * cosj) ** 2
        else:
            b_layer, c_layer = factor * cosj ** 2, factor * n_layer ** 2
        upper, lower = 1j * b_layer, 1j * c_layer
        # Right-multiply the cumulative 2×2 characteristic matrix by this layer.
        m11, m12 = m11 * cd + m12 * lower, m11 * upper + m12 * cd
        m21, m22 = m21 * cd + m22 * lower, m21 * upper + m22 * cd
        # Lossless reflectors can also produce an exponentially large product.
        magnitude = max(abs(m11), abs(m12), abs(m21), abs(m22))
        if magnitude > 1e100:
            m11, m12 = m11 / magnitude, m12 / magnitude
            m21, m22 = m21 / magnitude, m22 / magnitude
            log_scale += math.log(magnitude)

    b = m11 * exit_e + m12 * exit_h
    c = m21 * exit_e + m22 * exit_h

    denom = eta0 * b + c
    r = (eta0 * b - c) / denom
    R = abs(r) ** 2
    # Transmittance into a (possibly lossy) substrate.
    exit_flux = (exit_e.conjugate() * exit_h).real
    T = 4.0 * eta0.real * exit_flux * math.exp(-2 * log_scale) / abs(denom) ** 2
    A = 1.0 - R - T
    return R, T, A


def _resolve_indices(stack: Stack, wavelength_nm: float) -> tuple[np.ndarray, np.ndarray, complex, complex]:
    layers_n = np.array([ly.index_at(wavelength_nm) for ly in stack.layers], dtype=complex)
    layers_d = np.array([ly.thickness_nm for ly in stack.layers], dtype=float)
    n0 = stack.incident_index(wavelength_nm)
    ns = stack.substrate_index(wavelength_nm)
    return layers_n, layers_d, n0, ns


def solve_stack(stack: Stack, wavelength_nm: float, aoi_deg: float = 0.0, pol: str = "avg") -> dict:
    """Solve one wavelength for a :class:`Stack`.

    ``pol`` may be ``'s'``, ``'p'`` or ``'avg'`` (unpolarized mean).  Returns a
    dict with ``R``, ``T``, ``A`` (and per-polarization values when ``avg``).
    """
    layers_n, layers_d, n0, ns = _resolve_indices(stack, wavelength_nm)

    def one(p: str) -> tuple[float, float, float]:
        return solve_RT(layers_n, layers_d, wavelength_nm, aoi_deg, p, n0, ns)

    if pol in ("s", "p"):
        R, T, A = one(pol)
        return {"R": R, "T": T, "A": A, "pol": pol}
    if pol == "avg":
        rs, ts, as_ = one("s")
        rp, tp, ap = one("p")
        return {
            "R": 0.5 * (rs + rp),
            "T": 0.5 * (ts + tp),
            "A": 0.5 * (as_ + ap),
            "R_s": rs,
            "R_p": rp,
            "T_s": ts,
            "T_p": tp,
            "pol": "avg",
        }
    raise ValueError("pol must be 's', 'p' or 'avg'")


def spectrum(
    stack: Stack,
    wl_grid_nm: np.ndarray,
    aoi_deg: float = 0.0,
    pol: str = "avg",
) -> dict:
    """R/T/A over a wavelength grid.  Returns arrays keyed ``wl_nm``, ``R``, ``T``, ``A``."""
    wl = np.asarray(wl_grid_nm, dtype=float)
    R = np.empty_like(wl)
    T = np.empty_like(wl)
    A = np.empty_like(wl)
    for i, w in enumerate(wl):
        res = solve_stack(stack, float(w), aoi_deg=aoi_deg, pol=pol)
        R[i], T[i], A[i] = res["R"], res["T"], res["A"]
    return {"wl_nm": wl, "R": R, "T": T, "A": A}

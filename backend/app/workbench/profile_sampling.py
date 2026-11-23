"""Sampling of continuous Gaussian envelopes inside homogeneous and quadratic-index media."""
import math

import numpy as np


# Resolve each radius cycle and its narrow waist. Budget applies per axis and element.
MAX_PROFILE_POINTS = 1024
WAIST_OFFSETS = (-5, -2, -1, -.5, -.25, .25, .5, 1, 2, 5)


def uniform_depths(q, n, length, count):
    depths = set(np.linspace(0, length, count))
    focus, rayleigh = -q.real * n, q.imag * n
    # The waist can lie just outside this component while its strongly
    # curved near field remains inside (e.g. on the other side of a face).
    depths.update(focus + offset * rayleigh for offset in WAIST_OFFSETS
                  if 0 < focus + offset * rayleigh < length)
    if 0 <= focus <= length:
        depths.add(focus)
        return sorted(depths), [focus]
    return sorted(depths), []


def grin_depths(q, n, g, length, count):
    """Return depths, internal minima and optional radius bounds relative to the input.

    w(phi)/w(0) = |cos(phi) + u*sin(phi)|, where phi=g*z and u=1/(n*g*q).
    Its extrema are the singular values of [[1, Re(u)], [0, Im(u)]].
    """
    # Use v = 1/u to keep vanishing gradients well-conditioned. Normalizing
    # v*cos(phi) + sin(phi) preserves both extrema phases and their ratio.
    v = q * (n*g)
    if v == 0:
        depths, minima = uniform_depths(q,n,length,count)
        return depths,minima,None
    scale = max(1.,abs(v))
    a, b, d = v.real/scale, v.imag/scale, 1/scale
    maximum = .5 * (math.hypot(a, b+d) + math.hypot(a, b-d))
    minimum = abs(b)*d/maximum
    ratio = minimum/maximum
    if math.isclose(ratio, 1., rel_tol=1e-13):
        return [0., length], [], None
    phase_end = g * length
    cycles = phase_end / math.pi
    grid_size = max(count, math.ceil(12 * cycles) + 1)
    # Compute both phases directly: subtracting two nearly equal multiples of
    # pi loses the physical waist of a weak-gradient medium as g tends to zero.
    sine, cosine = 2*a*d, a*a+b*b-d*d
    peak = .5 * math.atan2(sine, cosine)
    trough = .5 * math.atan2(-sine, -cosine)
    def bounds():
        entrance = math.hypot(a,b)
        first = trough if trough >= 0 else trough + math.pi
        return minimum/entrance, maximum/entrance, first/g
    if grid_size > MAX_PROFILE_POINTS:
        return [0., length], [], bounds()
    phases = {0., phase_end}
    minima = []
    for phase, is_minimum in ((peak, False), (trough, True)):
        for index in range(-1, math.ceil(cycles) + 2):
            center = phase + index * math.pi
            if 0 <= center <= phase_end:
                phases.add(center)
                if is_minimum:
                    minima.append(center / g)
            if is_minimum:
                for offset in WAIST_OFFSETS:
                    neighbor = center + math.atan(offset * ratio)
                    if 0 < neighbor < phase_end:
                        phases.add(neighbor)
    if len(phases) + grid_size > MAX_PROFILE_POINTS:
        return [0., length], [], bounds()
    depths = {phase/g for phase in phases if 0 < phase < phase_end}
    depths.update(np.linspace(0, length, grid_size))
    return sorted(depths), minima, None

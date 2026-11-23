"""Rigid changes of transverse coordinates after both axes are calibrated in millimetres."""

import math

import numpy as np


def transverse_matrix(angle_deg, flip_y):
    """Rotate positive x towards positive y, reflecting the input y axis first if requested."""
    angle = angle_deg % 360.0
    if angle % 90 == 0:
        c, s = ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(angle // 90)]
    else:
        c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    mirror = -1.0 if flip_y else 1.0
    return np.array([[c, -s * mirror], [s, c * mirror]])

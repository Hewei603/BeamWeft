"""Two independent devices for the ordered and multiport representations."""
import cmath
import math

from pydantic import Field

from app.study_api import ElementLaw, StrictInput, Study


class CylindricalPair(StrictInput):
    wavelength_nm: float = Field(gt=0, title="真空波长 / nm")
    spacing_mm: float = Field(25, ge=0, title="两片柱面透镜间距 / mm")
    first_mm: float = Field(50, title="第一片柱面透镜焦距 / mm")
    second_mm: float = Field(-25, title="第二片柱面透镜焦距 / mm")


def pair_response(p):
    if not p.first_mm or not p.second_mm:
        raise ValueError("柱面透镜焦距须为非零数值。")
    a = 1 - p.spacing_mm/p.first_mm
    return {"transfer":{"matrix_t":[a, p.spacing_mm, -1/p.first_mm-a/p.second_mm, 1-p.spacing_mm/p.second_mm],
                        "matrix_s":[1, p.spacing_mm, 0, 1]}}


pair = Study("cylindrical_pair", "柱面透镜对", "1",
    "空气中共轴的两片理想薄柱面透镜，仅切向聚焦。参考面为首末镜面。焦距采用给定常量，忽略色散、截光和像差。",
    CylindricalPair, pair_response, element_law=ElementLaw(representation="ray_matrix",
        output="transfer", wavelength_input="wavelength_nm", length_input="spacing_mm"))


class Retarder(StrictInput):
    wavelength_nm: float = Field(gt=0, title="真空波长 / nm")
    retardance_nm: float = Field(160, title="本征轴光程差 / nm")
    angle_deg: float = Field(0, title="快轴相对 H 方向的角度 / °")


ports = ("in-H","in-V","out-H","out-V")


def retarder_response(p):
    angle = math.radians(p.angle_deg)
    c, s = math.cos(angle), math.sin(angle)
    slow = cmath.exp(2j*math.pi*p.retardance_nm/p.wavelength_nm)
    hh, vv, hv = c*c + s*s*slow, s*s + c*c*slow, c*s*(1-slow)
    # The backward block is the transpose in the same transverse port basis.
    matrix = [[0,0,hh,hv],[0,0,hv,vv],[hh,hv,0,0],[hv,vv,0,0]]
    return {"transfer":{"ports":list(ports),"matrix":[[[z.real,z.imag] for z in row] for row in matrix]}}


retarder = Study("linear_retarder", "线性延迟器", "1",
    "固定实验室 H/V 基底的无损、无反射、互易波片。各端口代表一个偏振模式，正交端口的功率可分别读出。"
    "慢轴传递相位为 +2π·光程差/真空波长，快轴公共相位设为零。忽略色散和空间失配。",
    Retarder, retarder_response, element_law=ElementLaw(representation="scattering",
        output="transfer", wavelength_input="wavelength_nm", ports=ports))

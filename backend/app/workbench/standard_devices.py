"""Built-in device laws using the public element contract."""
import cmath
import math

from pydantic import Field

from ..study_api import ElementLaw, StrictInput, Study


class AstigmaticPower(StrictInput):
    wavelength_nm: float = Field(gt=0, title="真空波长 / nm")
    tangential_dpt: float = Field(10, title="切向屈光力 / m⁻¹")
    sagittal_dpt: float = Field(0, title="弧矢屈光力 / m⁻¹")


def axis_lens(p):
    return {"transfer":{"matrix_t":[1,0,-p.tangential_dpt/1000,1],
                        "matrix_s":[1,0,-p.sagittal_dpt/1000,1]}}


class PrincipalAxes(StrictInput):
    wavelength_nm: float = Field(gt=0, title="真空波长 / nm")
    angle_deg: float = Field(0, title="第一主轴相对 H 的角度 / °")
    first_transmission: float = Field(1, ge=0, le=1, title="第一主轴功率透过率")
    second_transmission: float = Field(0, ge=0, le=1, title="第二主轴功率透过率")
    delay_nm: float = Field(0, title="第二轴相对光程 / nm")
    phase_deg: float = Field(0, title="第一主轴的传递相位 / °")


_PORTS = ("L-H", "L-V", "R-H", "R-V")


def principal_transmission(p):
    theta = math.radians(math.remainder(p.angle_deg,360))
    c, s = math.cos(theta), math.sin(theta)
    phase = cmath.rect(1, math.radians(math.remainder(p.phase_deg,360)))
    delta = math.tau*(math.remainder(p.delay_nm,p.wavelength_nm)/p.wavelength_nm)
    a = phase*math.sqrt(p.first_transmission)
    b = phase*cmath.rect(math.sqrt(p.second_transmission),delta)
    hh, vv, hv = a*c*c+b*s*s, a*s*s+b*c*c, (a-b)*c*s
    matrix = [[0,0,hh,hv],[0,0,hv,vv],[hh,hv,0,0],[hv,vv,0,0]]
    return {"transfer":{"ports":list(_PORTS),"matrix":[[[z.real,z.imag] for z in row] for row in matrix]}}


STANDARD_DEVICES = (
    Study("aligned_lens_power", "双轴薄透镜", "1",
          "零厚度近轴元件。两条主轴与光路 T/S 对齐，各自设置带符号的屈光力。零表示该轴无聚焦。屈光力取当前波长下的给定值。",
          AstigmaticPower, axis_lens, element_law=ElementLaw(representation="ray_matrix", scalar_transfer="quadratic_phase",
                                                           wavelength_input="wavelength_nm", output="transfer")),
    Study("principal_axis_transmission", "线性偏振元件", "1",
          "在固定实验室 H/V 基底内计算具有两条正交线性主轴的互易 Jones 透射。每轴有独立功率透过率，两轴可有光程差。"
          "假定匹配端口、零反射、无退偏和无空间失配。端口 L/R 表示器件两侧。用于干涉时须核对公共传递相位。",
          PrincipalAxes, principal_transmission, element_law=ElementLaw(representation="scattering", wavelength_input="wavelength_nm", output="transfer", ports=_PORTS)),
)

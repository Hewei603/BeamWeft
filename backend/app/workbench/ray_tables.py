"""Build a registered forward ray population from corresponding rows of a source file."""

from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator

from ..physics.measurement_data import POWER_SCALE
from ..study_api import Port, StrictInput, Study
from .project_outputs import WAVELENGTH, at_wavelength
from .ray_source import MAX_RAYS, RAY_BUNDLE, RayBundle, ray_output
from .transverse_registration import POSITION


Column = Annotated[list[float], Field(min_length=1, max_length=MAX_RAYS)]
DIRECTION = Port(quantity="optics.direction-component", label="方向分量", shape="series", unit="1", scale=1)
POWER = Port(quantity="optics.power", label="辐射功率", unit="W", scale=1, alternate_units=POWER_SCALE)


class RayTable(StrictInput):
    plane: str = Field(min_length=1, max_length=150, pattern=r"\S", title="光线登记面")
    basis: str = Field(
        min_length=1,
        max_length=1600,
        pattern=r"\S",
        title="坐标、逐行对应与权重依据",
        description="说明参考面、朝前的轴向、横向 T/S 轴、介质，以及文件怎样表示每条光线携带的份额。",
    )
    quality: Literal["conditional", "documented"] = Field(
        default="conditional",
        title="数据依据",
        description="conditional 表示本次采用的模型或假设，documented 表示数据及其物理解释有记录。",
    )
    medium_n: float = Field(gt=0, title="登记面所在介质的折射率")
    wavelength_nm: float | None = Field(default=None, gt=0, title="共同的真空波长 / nm")
    x_mm: Column = Field(title="各光线的 T 位置 / mm")
    y_mm: Column = Field(title="各光线的 S 位置 / mm")
    directions: Literal["direction_cosines", "slopes"] = Field(
        title="方向列的定义",
        description="direction_cosines 需要单位向量 l、m、n_z；slopes 需要两项几何斜率 dx/dz、dy/dz。",
    )
    direction_x: Column = Field(title="各光线的横向第一方向分量")
    direction_y: Column = Field(title="各光线的横向第二方向分量")
    direction_z: Column | None = Field(default=None, title="各光线的轴向方向余弦")
    z_mm: Column | None = Field(
        default=None,
        title="各光线的轴向起点 / mm",
        description="非共面记录可在已知均匀介质中换算到同一参考面。留空表示位置已经在登记面上。",
    )
    reference_z_mm: float | None = Field(
        default=None,
        title="上述坐标系中的登记面 z / mm",
        description="换面段须在同一均匀介质内且没有其他光学表面、遮挡或损耗。",
    )
    weighting: Literal["relative_flux", "radiant_power", "equal_flux"] = Field(
        title="每条光线的权重依据", description="选择相对通量、逐光线辐射功率，或明确每条记录携带相同通量。"
    )
    relative_flux: Column | None = Field(default=None, title="各光线相对通量")
    power_w: Column | None = Field(default=None, title="各光线辐射功率 / W")
    ray_wavelength_nm: Column | None = Field(
        default=None, title="各光线真空波长 / nm", description="可用原件波长列核验单色条件。不同波长分组后分别建束。"
    )

    @model_validator(mode="after")
    def corresponding_rays(self):
        n = len(self.x_mm)
        for field in RAY_COLUMNS:
            values = getattr(self, field)
            if values is not None and len(values) != n:
                raise ValueError(
                    f"{type(self).model_fields[field].title}的行数与位置列不一致，请保持同一光线的逐行对应。"
                )
        if (self.directions == "direction_cosines") != (self.direction_z is not None):
            raise ValueError("方向余弦需要三列 l、m、n_z；几何斜率只需 dx/dz 和 dy/dz。")
        if (self.z_mm is None) != (self.reference_z_mm is None):
            raise ValueError("跨面换算需要原始逐行 z 坐标和共同的登记面 z。")
        if (self.weighting == "relative_flux") != (self.relative_flux is not None) or (
            (self.weighting == "radiant_power") != (self.power_w is not None)
        ):
            raise ValueError("请为所选权重提供对应的唯一通量列。采用等通量时应在依据中说明其来源。")
        return self


def _assemble(a):
    x, y = np.asarray(a.x_mm), np.asarray(a.y_mm)
    tx, ty = np.asarray(a.direction_x), np.asarray(a.direction_y)
    if a.direction_z is not None:
        axial = np.asarray(a.direction_z)
        invalid = (axial <= 0) | (np.abs(np.hypot(np.hypot(tx, ty), axial) - 1) > 2e-5)
        if invalid.any():
            raise ValueError(
                f"光线序列第 {int(np.flatnonzero(invalid)[0]) + 1} 项的三个方向余弦需要构成朝前的单位向量。"
            )
        tx, ty = tx / axial, ty / axial
    if a.z_mm is not None:
        distance = a.reference_z_mm - np.asarray(a.z_mm)
        x, y = x + distance * tx, y + distance * ty
    wavelength = a.wavelength_nm
    if a.ray_wavelength_nm is not None:
        wavelengths = np.asarray(a.ray_wavelength_nm)
        wavelength = wavelength if wavelength is not None else float(wavelengths[0])
        if np.any(wavelengths <= 0) or not np.allclose(wavelengths, wavelength, rtol=1e-8, atol=0):
            raise ValueError("此束需要共同的正真空波长，请按原始波长分组并核对登记值和单位。")
    if a.power_w is not None:
        weights = np.asarray(a.power_w)
    elif a.relative_flux is not None:
        weights = np.asarray(a.relative_flux)
    else:
        weights = np.ones(len(x))
    if (weights < 0).any() or weights.max() <= 0:
        raise ValueError("通量权重须非负，且至少一条光线携带正通量。请核对物理量与原始列。")
    relative = weights / weights.max()
    relative /= relative.sum()
    rows = np.column_stack((x, y, a.medium_n * tx, a.medium_n * ty, relative))
    state = RayBundle(
        plane=a.plane,
        basis=a.basis,
        medium_n=a.medium_n,
        wavelength_nm=wavelength,
        quality=a.quality,
        rays=rows.tolist(),
        replicate_split=None,
    )
    result = {
        "status": "completed",
        "bundle": state.model_dump(mode="json"),
        "ray_count": len(rows),
        "wavelength_nm": wavelength,
        "assumptions": [
            "每行携带的通量在射线间归一化。位置与方向保持逐行配对，传播时可保留其相关性。",
            "登记面沿正 z 方向传播。若原始位置不在同一面，已沿给定方向在均匀介质中投影，未处理两面之间的光学元件。",
        ],
    }
    if a.power_w is not None:
        result["launched_power_w"] = float(weights.sum())
    return result


def assemble_ray_bundle(args):
    try:
        with np.errstate(over="raise", invalid="raise", divide="raise"):
            return _assemble(args)
    except FloatingPointError as exc:
        raise ValueError("位置、方向或通量超出数值范围。请核对原件的长度和功率单位。") from exc


RAY_COLUMNS = {
    "x_mm": POSITION,
    "y_mm": POSITION,
    "z_mm": POSITION.model_copy(update={"quantity": "optics.axial-position", "label": "轴向位置"}),
    "direction_x": DIRECTION,
    "direction_y": DIRECTION,
    "direction_z": DIRECTION,
    "power_w": POWER.model_copy(update={"shape": "series"}),
    "relative_flux": Port(
        quantity="optics.ray-relative-flux", label="逐光线相对通量", shape="series", unit="1", scale=1
    ),
    "ray_wavelength_nm": WAVELENGTH.model_copy(
        update={"shape": "series", "alternate_units": {"um": 1e-6, "µm": 1e-6, "μm": 1e-6, "m": 1}}
    ),
}

RAY_TABLE_STUDY = Study(
    "assemble_ray_bundle",
    "从逐行位置、方向与通量建立光线束",
    "1",
    "读取同一记录中的横向位置、方向余弦或几何斜率，以及每条光线的相对通量或辐射功率。"
    "可从仪器、供应商或其他仿真的 CSV、表格和文本列绑定原件，保留位置与方向的关联及其来源。",
    RayTable,
    assemble_ray_bundle,
    input_ports={**RAY_COLUMNS, "wavelength_nm": WAVELENGTH},
    output_ports={"bundle": ray_output(RAY_BUNDLE, "bundle"), "launched_power_w": at_wavelength(POWER), "wavelength_nm": WAVELENGTH},
    paired_inputs=tuple(("x_mm", key) for key in RAY_COLUMNS if key != "x_mm"),
    metrics={"ray_count": "光线条数", "launched_power_w": "原始逐行辐射功率之和 / W", "wavelength_nm": "真空波长 / nm"},
    keywords=("ray file", "direction cosines", "flux", "射线表", "方向余弦", "展度", "非相干", "光线束"),
)

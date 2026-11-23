"""Exchange a sampled intensity distribution without obscuring pixel integration or orientation."""

from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator

from ..study_api import Port, StrictInput


INTENSITY_MAP = Port(
    quantity="optics.transverse-intensity-map.v1",
    label="有物理坐标的横向光强图",
    unit="rectangular-cell-integrals.mm.v1",
    shape="state",
    scale=1,
    registration_label="说明来源参考面、横向坐标以及模拟或采集工况",
)
Edges = Annotated[list[float], Field(min_length=2, max_length=513)]
Cells = Annotated[list[float], Field(min_length=1, max_length=512)]
Caption = Annotated[str, Field(min_length=1, max_length=120, pattern=r"\S")]


class IntensityMap(StrictInput):
    x_edges_mm: Edges = Field(title="横向单元边界 / mm")
    y_edges_mm: Edges = Field(title="纵向单元边界 / mm")
    values: list[Cells] = Field(
        min_length=1,
        max_length=512,
        title="各单元积分信号",
        description="每行沿 x 排列，行序沿 y 增加。数值代表该单元全部信号，保留背景扣除后的负噪声。",
    )
    signal_unit: Caption = Field(title="单元积分信号单位")
    x_label: Caption = Field(title="横轴名称")
    y_label: Caption = Field(title="纵轴名称")
    basis: str = Field(min_length=1, max_length=4000, pattern=r"\S", title="测量或计算依据")
    y_down: bool = Field(default=False, title="绘图时纵轴向下")
    plane: str | None = Field(default=None, max_length=500, title="参考面")
    wavelength_nm: float | None = Field(default=None, gt=0, title="已知真空波长 / nm")
    quality: Literal["unassessed", "usable", "conditional", "limited"] = Field(
        default="unassessed",
        title="来源质量",
        description="由生产者判定。conditional 表示在注明的条件下可以使用，limited 表示有已知影响检验的缺陷，unassessed 表示尚无评定。",
    )
    warnings: list[str] = Field(default_factory=list, title="来源适用条件与警告")

    @model_validator(mode="after")
    def rectangular(self):
        for axis in (self.x_edges_mm, self.y_edges_mm):
            if any(a >= b for a, b in zip(axis, axis[1:])):
                raise ValueError("光强图单元边界须按本地坐标严格递增，翻转请在坐标配准中声明。")
        if len(self.values) != len(self.y_edges_mm) - 1 or any(
            len(row) != len(self.x_edges_mm) - 1 for row in self.values
        ):
            raise ValueError("光强图数值矩阵的行数、列数须对应纵横两组物理边界。")
        return self


def density_map(raster, *, unit, plane, wavelength_nm, basis, warnings, quality):
    """The solver raster stores mean density per cell. Publish its area integral in the declared unit."""
    edges = np.asarray(raster["edges_mm"])
    values = np.asarray(raster["density"]) * np.outer(np.diff(edges), np.diff(edges))
    return IntensityMap(
        x_edges_mm=edges.tolist(),
        y_edges_mm=edges.tolist(),
        values=values.tolist(),
        signal_unit=unit,
        x_label="切向 T",
        y_label="弧矢 S",
        basis=basis,
        plane=plane,
        wavelength_nm=wavelength_nm,
        warnings=warnings,
        quality=quality,
    ).model_dump(mode="json", exclude_none=True)

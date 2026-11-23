"""Typed, user-reviewable crystal sections and manual component records.

An absorber's initial transmission is a bulk material property, never a coating.
The geometry adapter deliberately uses real host indices only; dynamic laser
predictions require separate physics which is not implemented by ABCD.
"""

from __future__ import annotations

import math
import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..physics.materials import MaterialError, index_info, resolve_material


class StrictSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)


def material_key(value):
    """Normalize spelling only for exact model/role lookup. Preserve the text on the record."""
    return unicodedata.normalize("NFKC", value or "").casefold().replace(" ", "")


_HOST_APPROXIMATIONS = {"nd:yag": "yag", "cr:yag": "yag", "cr4+:yag": "yag",
                        "yvo4": "yvo4_e", "nd:yvo4": "yvo4_e"}
_KNOWN_ROLES = {"yag":"undoped","nd:yag":"gain","cr:yag":"saturable_absorber",
                "cr4+:yag":"saturable_absorber","yvo4":"undoped","nd:yvo4":"gain"}


def crystal_medium(material):
    key = material_key(material)
    try:
        return resolve_material(_HOST_APPROXIMATIONS.get(key, key))
    except MaterialError:
        return None


class CrystalSegment(StrictSpec):
    material: str | None = Field(default=None, min_length=1, max_length=100, description="Original material designation when known. An unrecognized name is still valid inventory data.")
    role: Literal["undoped", "gain", "saturable_absorber"] | None = None
    length_mm: float | None = Field(default=None, gt=0, le=1000)
    doping_pct: float | None = Field(default=None, ge=0, lt=100)
    uncertain: bool = False
    note: str = Field(default="", max_length=3000)
    initial_transmission_pct: float | None = Field(default=None, gt=0, le=100)
    initial_transmission_wavelength_nm: float | None = Field(default=None, gt=0, le=100000)

    @model_validator(mode="after")
    def material_role(self):
        expected = _KNOWN_ROLES.get(material_key(self.material))
        if self.role is not None and expected is not None and self.role != expected:
            raise ValueError(f"{self.material} 必须记录为 {expected}，不能把吸收体记成端帽或增益段。")
        if self.role == "undoped" and self.doping_pct not in (None, 0):
            raise ValueError("无掺杂段不能填写非零掺杂浓度。")
        if self.role != "saturable_absorber" and (
            self.initial_transmission_pct is not None or self.initial_transmission_wavelength_nm is not None
        ):
            raise ValueError("初始透过率只能记录在可饱和吸收段，不是镜片透过率。")
        if self.initial_transmission_wavelength_nm is not None and self.initial_transmission_pct is None:
            raise ValueError("填写初始透过率波长时，也需填写透过率数值。")
        return self


class CrystalSpec(StrictSpec):
    width_mm: float | None = Field(default=None, gt=0, le=1000)
    height_mm: float | None = Field(default=None, gt=0, le=1000)
    overall_length_mm: float | None = Field(default=None, gt=0, le=100000, exclude_if=lambda v: v is None,
        description="Independently reported end-to-end thickness in mm. Omit when only section lengths are known.")
    material_label: str | None = Field(default=None, min_length=1, max_length=100, exclude_if=lambda v: v is None,
        description="Material inscription on the whole part, independent from the identification of any one section.")
    segments: list[CrystalSegment] = Field(default_factory=list, max_length=24)
    coating_note: str = Field(default="未知", max_length=3000)
    notes: str = Field(default="", max_length=5000)

    def check_gain_role(self):
        if self.segments and all(s.role not in (None,"gain") for s in self.segments):
            raise ValueError("增益晶体至少需要一段增益材料。纯被动介质可登记为窗口片或其他晶体。")

    @property
    def total_length_mm(self):
        if self.segments and all(s.length_mm is not None for s in self.segments):
            return math.fsum(s.length_mm for s in self.segments)
        return None

    @property
    def material_summary(self):
        if self.segments and all(s.material for s in self.segments):
            summary = " + ".join(dict.fromkeys(s.material for s in self.segments))
            if len(summary) <= 100:
                return summary
        return None

    def dimension_summary(self, thickness):
        dimensions = [self.width_mm, self.height_mm, thickness]
        if any(value is not None for value in dimensions):
            return "×".join("未知" if value is None else f"{value:g}" for value in dimensions) + " mm"
        return None

    @property
    def gain_doping(self):
        gains = [s for s in self.segments if s.role == "gain"]
        return gains[0].doping_pct if len(gains) == 1 and all(s.role for s in self.segments) else None


class ManualCoating(StrictSpec):
    surface: Literal["S1", "S2", "both"] = "both"
    wl_min_nm: float = Field(gt=0, le=100000)
    wl_max_nm: float = Field(gt=0, le=100000)
    function: Literal["HR", "AR", "HT", "PR"]
    value_type: Literal["R", "T"] | None = None
    comparator: Literal[">", "<", "="] | None = None
    value_pct: float | None = Field(default=None, ge=0, le=100)
    raw_fragment: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def band(self):
        if self.wl_max_nm < self.wl_min_nm:
            raise ValueError("镀膜波长上限不能小于下限。")
        if self.value_pct is not None and self.value_type is None:
            raise ValueError("镀膜数值需明确是R还是T；未知时留空。")
        return self


Category = Literal["mirror", "lens", "gain_crystal", "nonlinear_crystal", "crystal_other", "window", "other"]


class ManualItemCreate(StrictSpec):
    name: str = Field(min_length=1, max_length=255)
    category: Category = Field(default="other", description="Recorded type of the physical item. When the type is unknown, omit it or use other.")
    vendor: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=100)
    keeper: str | None = Field(default=None, max_length=255)
    quantity: float = Field(default=1, ge=0, le=100000)
    raw_spec: str | None = Field(default=None, max_length=30000)
    material: str | None = Field(default=None, max_length=100)
    diameter_mm: float | None = Field(default=None, gt=0, le=100000)
    roc_mm: float | None = Field(default=None, allow_inf_nan=False)
    roc_is_flat: bool = False
    focal_length_mm: float | None = Field(default=None, allow_inf_nan=False)
    thickness_mm: float | None = Field(default=None, gt=0, le=100000)
    dimensions: str | None = Field(default=None, max_length=100)
    cut_axis: str | None = Field(default=None, max_length=50)
    cut_angle_theta_deg: float | None = Field(default=None, description="Reported crystal cut theta in degrees, in its source coordinate convention.")
    cut_angle_phi_deg: float | None = Field(default=None, description="Reported crystal cut phi in degrees, in its source coordinate convention.")
    doping_pct: float | None = Field(default=None, ge=0, lt=100)
    crystal_spec: CrystalSpec | None = None
    coatings: list[ManualCoating] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def geometry(self):
        if self.roc_mm == 0 or self.focal_length_mm == 0:
            raise ValueError("曲率半径/焦距不能填写0；平镜请明确选择平面，未知值留空。")
        if self.roc_is_flat and self.roc_mm is not None:
            raise ValueError("平面镜不能同时填写有限曲率半径。")
        if self.crystal_spec:
            if self.category in {"mirror","lens"}:
                raise ValueError("此登记表按介质截面与分段记录实物。镜片和透镜须保留其镜面或透镜模型。")
            if self.category == "gain_crystal":
                self.crystal_spec.check_gain_role()
            for top, source, label in ((self.material,self.crystal_spec.material_label,"整件材料标注"),
                                       (self.thickness_mm,self.crystal_spec.overall_length_mm,"独立总厚度")):
                if top is not None and source is not None and top != source:
                    raise ValueError(f"{label}在元件与分段记录中填写不一致，请核对后保存。")
        return self


class ManualItemPatch(StrictSpec):
    name: str = Field(default=None, min_length=1, max_length=255)
    category: Category = Field(default=None)
    vendor: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=100)
    keeper: str | None = Field(default=None, max_length=255)
    quantity: float = Field(default=None, ge=0, le=100000)
    raw_spec: str | None = Field(default=None, max_length=30000)
    material: str | None = Field(default=None, max_length=100)
    diameter_mm: float | None = Field(default=None, gt=0, le=100000)
    roc_mm: float | None = Field(default=None, allow_inf_nan=False)
    roc_is_flat: bool = Field(default=None)
    focal_length_mm: float | None = Field(default=None, allow_inf_nan=False)
    thickness_mm: float | None = Field(default=None, gt=0, le=100000)
    dimensions: str | None = Field(default=None, max_length=100)
    cut_axis: str | None = Field(default=None, max_length=50)
    cut_angle_theta_deg: float | None = None
    cut_angle_phi_deg: float | None = None
    doping_pct: float | None = Field(default=None, ge=0, lt=100)
    crystal_spec: CrystalSpec | None = None
    coatings: list[ManualCoating] = Field(default=None, max_length=100)

    def supplied_changes(self):
        changes = self.model_dump(exclude_unset=True)
        if self.crystal_spec is not None:
            for field in ("material_label","overall_length_mm"):
                # The stored record omits empty optional source fields. A PATCH
                # must still distinguish an explicit withdrawal from omission.
                if field in self.crystal_spec.model_fields_set:
                    changes["crystal_spec"][field] = getattr(self.crystal_spec, field)
        return changes


def normalized_manual_values(payload: ManualItemCreate) -> dict:
    values = payload.model_dump()
    if payload.crystal_spec:
        spec = payload.crystal_spec
        total = spec.total_length_mm
        observed = spec.overall_length_mm if spec.overall_length_mm is not None else payload.thickness_mm
        if observed is not None:
            values["crystal_spec"]["overall_length_mm"] = observed
        values["thickness_mm"] = observed if observed is not None else total
        values["dimensions"] = payload.dimensions or spec.dimension_summary(values["thickness_mm"])
        label = spec.material_label or payload.material
        if label:
            values["material"] = values["crystal_spec"]["material_label"] = label
        else:
            values["material"] = spec.material_summary
        values["doping_pct"] = spec.gain_doping if spec.gain_doping is not None else payload.doping_pct
    return values


def crystal_geometry(spec: CrystalSpec, wavelength_nm: float | None, orientation="as_listed", *, single_pass=False, gain_mount=True):
    if gain_mount:
        spec.check_gain_role()
    segments = list(spec.segments)
    if not segments and spec.overall_length_mm is not None:
        # The outside faces have a known separation. No internal material or boundary is implied.
        segments = [CrystalSegment(length_mm=spec.overall_length_mm)]
    if not segments or any(s.length_mm is None for s in segments):
        missing = "、".join(str(i) for i, s in enumerate(segments, 1) if s.length_mm is None)
        raise ValueError(f"第 {missing} 段的长度待补，无法定位其界面。" if missing else "请登记整件厚度或各段的长度后再装入光路。")
    if spec.overall_length_mm is not None and not math.isclose(sum(s.length_mm for s in segments), spec.overall_length_mm, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(f"独立登记的总厚度 {spec.overall_length_mm:g} mm 与分段长度之和 {spec.total_length_mm:g} mm 不一致。请核实尺寸再装入光路。")
    if orientation == "reversed":
        segments.reverse()
    warnings = ["单程模型按当前波长下的分段折射率计算近轴传播。" if single_pass else
                "当前结果只计算冷腔近轴几何本征模，不预测起振阈值、输出功率、效率或实际热态。"]
    if not gain_mount:
        warnings = ["按正入射、平行平面界面和各段的标量实折射率计算近轴传播。实际面形、楔角、偏振走离、增益和非线性转换需相应处方或独立模型。"]
    elif wavelength_nm is None:
        warnings[0] = "已登记晶体各段的实际长度。补充工作波长后，可从材料色散模型取得各段折射率。"
    if not gain_mount and not spec.segments and spec.material_label:
        warnings.append(f"「{spec.material_label}」是整件标注，尚未确认各段材料。确认实物为均匀单段后，可登记该段材料。")
    if not gain_mount:
        warnings.append("传播模型采用各段的轴向长度。计入截光还需独立登记有效通光孔径。")
    elif spec.width_mm is None or spec.height_mm is None:
        warnings.append("晶体横截面尺寸未完整确认；几何传播可计算，实际通光孔径和截光仍需核对。")
    if any(s.role == "saturable_absorber" for s in segments):
        warnings.append(
            "含可饱和吸收体：按宿主实折射率计算近轴传播。吸收漂白及增益—光子—吸收体动力学需要专用模型，调Q脉宽、重复频率、单脉冲能量或平均功率尚未计算。"
        )
        warnings.append("吸收体的初始透过率仅描述其标定条件。输出镜透过率与运行时损耗需各自的资料。")
    elements, segment_info = [], []
    for index, segment in enumerate(segments):
        host = crystal_medium(segment.material)
        if _HOST_APPROXIMATIONS.get(material_key(segment.material)) == "yvo4_e":
            # A crystal label and its use as a gain medium do not select its optical polarization.
            host = None
        info = index_info(host, wavelength_nm) if wavelength_nm is not None and host else None
        unknowns = []
        if segment.material is None:
            unknowns.append("材料未确认，工作波长处的折射率待补。")
        elif host is None:
            unknowns.append(f"「{segment.material}」没有可用的标量色散模型，请提供该光路工作波长与偏振下的折射率。")
        elif wavelength_nm is None:
            unknowns.append("工作波长待补，尚未确定该波长处的折射率。")
        elif not info.in_range or not math.isfinite(info.n):
            unknowns.append(f"{wavelength_nm:g} nm 超出「{segment.material}」模型的可用范围，折射率待补。")
            info = None
        if gain_mount and segment.role is None:
            unknowns.append("该段在器件中的作用待确认。")
        if segment.uncertain:
            unknowns.append("该段标签或物性待确认；当前模型以登记内容为条件。")
        warnings.extend(f"第{index + 1}段：{note}" for note in unknowns)
        label = {"undoped": "无掺杂段", "gain": "增益段", "saturable_absorber": "可饱和吸收段"}.get(segment.role, "介质段")
        material = segment.material or "材料待确认"
        elements.append(
            {
                "name": f"{material} {label} {segment.length_mm:g} mm"[:100],
                "kind": "space",
                "medium_role": segment.role or (None if gain_mount else "medium"),
                "length_mm": segment.length_mm,
                "n": info.n if info else None,
            }
        )
        segment_info.append(
            {**segment.model_dump(), "section": index + 1, "model_index": info.n if info else None,
             "index_source": info.source if info else None, "index_model":host, "unknowns":unknowns}
        )
        if segment.role == "gain" and segment.doping_pct is None:
            warnings.append(
                f"第{index + 1}段{material}掺杂浓度未知；吸收与增益仍需相应参数。"
            )
        if segment.initial_transmission_pct is not None and segment.initial_transmission_wavelength_nm is None:
            warnings.append(f"第{index + 1}段的初始透过率测量波长未确认，保留记录，不推定为1064 nm或808 nm。")
    if any(info["index_model"] and material_key(info["material"]) in _HOST_APPROXIMATIONS for info in segment_info):
        warnings.append("有宿主近似的材料按相应宿主的标量实折射率计算。")
    warnings.append("端面镀膜、相干复合腔反馈和实际损耗未求解。")
    return elements, segment_info, warnings

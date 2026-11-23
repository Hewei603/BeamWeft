"""Register an extended emitter's measured bounds separately from a model of how it fills them."""

import math
from typing import Annotated, Literal

import numpy as np
from pydantic import Field, model_validator
from scipy.stats import qmc
from ..physics.transverse_frames import transverse_matrix

from ..study_api import Condition, Port, StrictInput, Study
from .intensity_map import INTENSITY_MAP, IntensityMap


MAX_RAYS = 65536
_Name = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
_Evidence = Annotated[str, Field(min_length=1, max_length=4000, pattern=r"\S")]
_Weight = Annotated[float, Field(ge=0, le=1)]
_Row = tuple[float, float, float, float, _Weight]
_Pair = tuple[float, float]


class RayFrame(StrictInput):
    plane: _Name
    basis: _Evidence
    medium_n: float = Field(gt=0)
    wavelength_nm: float | None = Field(default=None, gt=0)
    warnings: list[str] = Field(default_factory=list, description="随光线在后续计算中保留的适用限制。")


class RayBundle(RayFrame):
    rays: list[_Row] = Field(
        max_length=MAX_RAYS, description="每行是横向 T/S 位置（mm）、折射率乘两轴几何斜率、相对初始发射功率的权重。"
    )
    quality: Literal["conditional", "documented", "limited"]
    replicate_split: int | None = Field(
        default=None, ge=0, description="两组独立采样交界的行号，各组最初代表一半发射功率。"
    )

    @model_validator(mode="after")
    def power_budget(self):
        weights = [r[4] for r in self.rays]
        if self.replicate_split is None:
            if math.fsum(weights) > 1 + 1e-12:
                raise ValueError("光线权重超过初始发射功率。请核对归一化基准。")
        else:
            split = self.replicate_split
            if split > len(weights) or max(math.fsum(weights[:split]), math.fsum(weights[split:])) > 0.5 + 1e-12:
                raise ValueError("两组独立采样须保留各自相对于一半初始发射功率的权重及行序。")
        return self


class RaySupport(RayFrame):
    x_mm: _Pair
    y_mm: _Pair
    p_t: _Pair
    p_s: _Pair
    throughput_bounds: tuple[_Weight, _Weight] = (1.0, 1.0)
    quality: Literal["conditional", "documented", "limited"] = "documented"

    @model_validator(mode="after")
    def increasing_bounds(self):
        if any(lo > hi for lo, hi in (self.x_mm, self.y_mm, self.p_t, self.p_s, self.throughput_bounds)):
            raise ValueError("相空间支持区间与功率比例区间都需要下界小于或等于上界。")
        return self


RAY_BUNDLE = Port(
    quantity="optics.weighted-geometric-rays.v1",
    label="已配准的非相干加权光线",
    unit="transverse-mm.reduced-slope.power-fraction.v1",
    scale=1,
    shape="state",
    registration_label="说明几何光线的参考面、介质、横轴、方向和功率基准。",
)
RAY_SUPPORT = Port(
    quantity="optics.transverse-ray-support.v1",
    label="几何光线的相空间外界",
    unit="transverse-mm.reduced-slope.bounds.v1",
    scale=1,
    shape="state",
    available_on_partial=True,
    registration_label="说明光线外界在实验中的参考面、介质与横轴方向。",
)


def ray_output(port, path, *, available_on_partial=True):
    """Carry source-state metadata in the catalog without loading individual ray rows."""
    conditions = {
        "ray.plane": Condition(path=path + ".plane", label="光线参考面", value_type="text"),
        "ray.basis": Condition(path=path + ".basis", label="来源与工况", value_type="text"),
        "ray.quality": Condition(path=path + ".quality", label="依据类型", value_type="text"),
        "ray.medium_n": Condition(path=path + ".medium_n", label="参考介质折射率"),
        "optics.vacuum-wavelength.nm": Condition(path=path + ".wavelength_nm", label="真空波长 / nm"),
    }
    return port.model_copy(update={"conditions": conditions, "available_on_partial": available_on_partial})


_Footprint = Literal["ellipse", "rectangle"]
_SHAPES = {"x-enum-labels": {"ellipse": "椭圆（等轴为圆）", "rectangle": "矩形"}}


class ExtendedEmitter(StrictInput):
    plane: _Name = Field(title="源所在参考面")
    basis: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"\S",
        title="尺寸、角范围及填充依据",
        description="说明资料来源、工作条件及是否有测量支撑位置与方向的联合分布。引用强度图时同时登记参考面和坐标对应。",
    )
    medium_n: float = Field(
        gt=0, title="发射侧介质折射率", description="射出光学元件后在哪一种介质中定义光线。以空气为基准时填 1。"
    )
    wavelength_nm: float | None = Field(default=None, gt=0, title="真空波长 / nm")
    position_shape: _Footprint | None = Field(default=None, title="发射面的支持区域形状", json_schema_extra=_SHAPES)
    width_t_mm: float | None = Field(default=None, ge=0, title="T 向发射区域全宽 / mm")
    width_s_mm: float | None = Field(default=None, ge=0, title="S 向发射区域全宽 / mm", description="留空与 T 相同。")
    angular_shape: _Footprint = Field(title="方向余弦平面中的角支持区域", json_schema_extra=_SHAPES)
    na_t: float = Field(
        ge=0, title="T 向数值孔径半宽", description="以 n 乘方向余弦定义。圆锥时等于 n·sin(最大离轴半角)。"
    )
    na_s: float | None = Field(default=None, ge=0, title="S 向数值孔径半宽", description="留空与 T 相同。")
    position_map: IntensityMap | None = Field(
        default=None, title="已登记的横向强度分布", description="可从相机或计算导入横向形状，仅代表所给窗口内的光。"
    )
    map_rotation_deg: float | None = Field(
        default=None,
        title="来源图横轴相对工程 T 轴的角度 / °",
        description="从工程 +T 向 +S 为正角，给出来源图 +x 指向；平行时填 0。",
    )
    map_flip_y: bool | None = Field(
        default=None,
        title="物理配准时镜像来源图纵轴",
        description="先镜像，后旋转和平移。按实际装调标定选择，图像在屏幕上向下显示仅是显示约定。",
    )
    offset_t_mm: float = Field(
        default=0,
        title="源在工程 T 向的偏移 / mm",
        description="几何支持区域的中心，或来源图自身 (0,0) 在工程中的位置。",
    )
    offset_s_mm: float = Field(default=0, title="源在工程 S 向的偏移 / mm")
    steer_t_mrad: float = Field(
        default=0,
        gt=-math.pi * 500,
        lt=math.pi * 500,
        title="T 向主光线偏角 / mrad",
        description="在近轴坐标中给各光线共同加上主光线斜率。默认为沿源面法线。",
    )
    steer_s_mrad: float = Field(default=0, gt=-math.pi * 500, lt=math.pi * 500, title="S 向主光线偏角 / mrad")
    fill: Literal["unspecified", "uniform_radiance", "map_independent_angle"] = Field(
        default="unspecified",
        title="位置与角度的功率分布",
        json_schema_extra={
            "x-enum-labels": {
                "unspecified": "仅掌握边界",
                "uniform_radiance": "按位置、投影方向独立且均匀的辐亮度试算",
                "map_independent_angle": "采用来源图的空间分布，另假设投影方向独立且均匀",
            }
        },
    )
    samples: int = Field(
        default=4096, ge=32, le=MAX_RAYS, title="两组采样的总光线数", description="取 32 到 65536 的 2 的整数次幂。"
    )

    @model_validator(mode="after")
    def accepted_directions(self):
        if self.position_map is None:
            if self.position_shape is None or self.width_t_mm is None:
                raise ValueError("请填写发射面的几何形状和全宽，或引用横向强度图。")
            if self.fill == "map_independent_angle" or self.map_rotation_deg is not None or self.map_flip_y is not None:
                raise ValueError("采用实测横向分布或图像配准参数需要来源强度图。")
        else:
            if (
                any(v is not None for v in (self.position_shape, self.width_t_mm, self.width_s_mm))
                or self.fill == "uniform_radiance"
            ):
                raise ValueError("已引用强度图。请采用图上范围，位置填充可选按该图试算或仅使用边界。")
            if self.map_rotation_deg is None or self.map_flip_y is None:
                raise ValueError("请明确来源图相对于工程 T/S 的转角和镜像关系，并核对图中原点的平移量。")
            wl = self.position_map.wavelength_nm
            if wl and self.wavelength_nm and not math.isclose(wl, self.wavelength_nm, rel_tol=1e-10):
                raise ValueError("源波长与导入的强度图不符。请核对实验工况和波长。")
        a, b = self.na_t, self.na_t if self.na_s is None else self.na_s
        if (max(a, b) if self.angular_shape == "ellipse" else math.hypot(a, b)) >= self.medium_n:
            raise ValueError("所给角区域触及或超出发射介质中的前向半球。请核对角度口径、两轴半宽和折射率。")
        if self.samples & (self.samples - 1):
            raise ValueError("两组可比较的 Sobol 采样需要总数为 2 的整数次幂。")
        return self


def _planar_draws(points, shape, half_widths):
    if shape == "rectangle":
        return (2 * points - 1) * half_widths
    radius = np.sqrt(points[:, 0])
    azimuth = 2 * np.pi * points[:, 1]
    return np.column_stack((radius * np.cos(azimuth), radius * np.sin(azimuth))) * half_widths


def sample_emitter(args):
    try:
        with np.errstate(over="raise", divide="raise", invalid="raise"):
            return _sample(args)
    except (FloatingPointError, OverflowError) as exc:
        raise ValueError("光源尺寸、角度或功率超出计算精度范围，请核对输入单位。") from exc


def _sample(args):
    na = np.array((args.na_t, args.na_t if args.na_s is None else args.na_s))
    center = np.array((args.offset_t_mm, args.offset_s_mm))
    n = args.medium_n
    image = args.position_map
    warnings = []
    if image is None:
        half_size = np.array((args.width_t_mm, args.width_t_mm if args.width_s_mm is None else args.width_s_mm)) / 2
        limits = np.stack((center - half_size, center + half_size))
        area = (np.pi if args.position_shape == "ellipse" else 4.0) * half_size.prod()
    else:
        x_edges, y_edges = np.array(image.x_edges_mm), np.array(image.y_edges_mm)
        rotation = transverse_matrix(args.map_rotation_deg, args.map_flip_y)
        vertices = np.array([(x, y) for x in x_edges[[0, -1]] for y in y_edges[[0, -1]]]) @ rotation.T + center
        limits = np.stack((vertices.min(axis=0), vertices.max(axis=0)))
        area = (x_edges[-1] - x_edges[0]) * (y_edges[-1] - y_edges[0])
        warnings = [*image.warnings, "发射功率基准仅包括已登记图窗内的光。窗外光量与光线方向不能从这张强度图测得。"]
        if image.quality == "unassessed":
            warnings.append("来源图没有记录适用性评定，按其格中积分代表相对光功率的条件计算。")
    steer = n * np.tan(np.array((args.steer_t_mrad, args.steer_s_mrad)) * 0.001)
    # For an axis-aligned ellipse the maximum of u_i²/(1-|u/n|²) is attained at that axis tip.
    # For a rectangle both coordinates may reach their limits at the same corner.
    radial = na / n if args.angular_shape == "ellipse" else np.hypot(*na) / n
    slopes = na / np.sqrt((1 - radial) * (1 + radial))
    frame = {
        "plane": args.plane,
        "basis": args.basis,
        "medium_n": n,
        "wavelength_nm": args.wavelength_nm or (image.wavelength_nm if image else None),
        "warnings": warnings,
    }
    support = RaySupport(
        **frame,
        x_mm=limits[:, 0],
        y_mm=limits[:, 1],
        quality="conditional" if image else "documented",
        p_t=(steer[0] - slopes[0], steer[0] + slopes[0]),
        p_s=(steer[1] - slopes[1], steer[1] + slopes[1]),
    )
    angular_area = (np.pi if args.angular_shape == "ellipse" else 4.0) * na.prod()
    result = {
        "status": "completed",
        "ray_support": support.model_dump(mode="json"),
        "source_area_mm2": float(area),
        "fill": args.fill,
        "assumptions": [
            "光线位置取源面 T/S 坐标，方向用折射率乘几何斜率。独立区间给出包围所有允许光线的外界，角点可能无法实际同时达到。"
        ],
    }
    if not np.any(steer):
        result["etendue_limit_mm2_sr"] = float(area * angular_area)
    if args.fill == "unspecified":
        result["warnings"] = [
            *warnings,
            "资料给出的位置和角范围只能约束几何包络。数值通过率、二维光斑和最优方案还需要联合光线分布，或明确选用一种填充假设。",
        ]
        return result
    if image:
        weights = np.asarray(image.values).ravel()
        if image.quality == "limited" or np.any(weights < 0) or not weights.max() > 0:
            result["status"] = "partial"
            result["warnings"] = [
                *warnings,
                "图中带有影响光强的采集限制、负积分格或缺少正信号，无法按这些值抽取物理光功率。请提供经独立标定的非负分布，或继续使用已发布的位置与角边界。",
            ]
            return result
        cdf = np.cumsum(weights / weights.max())
        cdf /= cdf[-1]
    half_count = args.samples // 2
    blocks = []
    for seed in (7351, 20357):
        unit = qmc.Sobol(5 if image else 4, scramble=True, seed=seed).random_base2(half_count.bit_length() - 1)
        if image:
            bins = np.searchsorted(cdf, unit[:, 0], side="right")
            iy, ix = np.divmod(bins, len(x_edges) - 1)
            positions = (
                np.column_stack(
                    (x_edges[ix] + unit[:, 1] * np.diff(x_edges)[ix], y_edges[iy] + unit[:, 2] * np.diff(y_edges)[iy])
                )
                @ rotation.T
                + center
            )
        else:
            positions = center + _planar_draws(unit[:, :2], args.position_shape, half_size)
        projected = _planar_draws(unit[:, -2:], args.angular_shape, na)
        cosine_z = np.sqrt(1 - np.square(projected / n).sum(axis=1))
        slopes_drawn = steer + projected / cosine_z[:, None]
        blocks.append(np.column_stack((positions, slopes_drawn, np.full(half_count, 1 / args.samples))))
    distribution = (
        "按来源图每个格的积分信号分配功率，格内按均匀密度取样。投影方向的独立均匀分布为额外假设，功率相对于图窗内的光。"
        if image
        else "按发射面积与投影方向余弦面积分别均匀且互相独立取样，功率相对于本源的全部发射功率。"
    )
    result["warnings"] = warnings
    if np.any(steer):
        result["assumptions"].append("采用近轴主光线偏转模型，把共同的横向斜率偏移叠加到源的每条光线上。")
    result["assumptions"].append(distribution)
    result["ray_bundle"] = RayBundle(
        **{**frame, "basis": args.basis + " " + distribution},
        rays=np.vstack(blocks).tolist(),
        quality="conditional",
        replicate_split=half_count,
    ).model_dump(mode="json")
    result["ray_count"] = args.samples
    return result


GENERATE_RAY_SOURCE = Study(
    "sample_phase_space_source",
    "扩展光源的相空间边界与采样",
    "1",
    "从发射面尺寸、数值孔径和介质求几何光线外界。保留未知填充；按明确的位置与方向模型可生成非相干加权光线，供多模光纤、LED、光阑照明或其他扩展源的光路比较。",
    ExtendedEmitter,
    sample_emitter,
    input_ports={"position_map": INTENSITY_MAP},
    registration_inputs={"position_map": "basis"},
    output_ports={
        "ray_bundle": ray_output(RAY_BUNDLE, "ray_bundle"),
        "ray_support": ray_output(RAY_SUPPORT, "ray_support"),
    },
    metrics={
        "source_area_mm2": "名义发射区域面积 / mm²",
        "etendue_limit_mm2_sr": "位置与角域可容纳的展度上限 / mm²·sr",
        "ray_count": "条件模型光线数",
    },
    keywords=(
        "数值孔径",
        "芯径",
        "多模",
        "泵浦",
        "radiance",
        "NA",
        "multimode",
        "geometrical étendue",
        "emitter",
        "ray source",
    ),
)

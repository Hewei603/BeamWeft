from io import BytesIO
from typing import Annotated, Literal

from numpy.lib import format as npy
from pydantic import Field, model_validator
from PIL import Image

from ..physics.image_moments import image_moments
from ..physics.numeric_grid import npy_header
from ..study_api import OriginalFile, Port, RasterView, StrictInput, Study
from .intensity_map import INTENSITY_MAP
from .centroid_state import DETECTOR_CENTROID


class ImageOriginal(OriginalFile):
    @classmethod
    def recognizes(cls, prefix, *, complete):
        if prefix.startswith(npy.MAGIC_PREFIX):
            try:
                shape, _, dtype = npy_header(BytesIO(prefix))
            except ValueError:
                return False if complete else None
            return len(shape) == 2 and min(shape) > 0 and dtype.kind in "buif"
        try:
            with Image.open(BytesIO(prefix)):
                return True
        except (OSError, SyntaxError, EOFError):
            return False if complete else None


class ImageMoments(StrictInput):
    image: ImageOriginal = Field(title="信号原图")
    frame: int = Field(default=1, ge=1, title="信号帧号", description="多帧文件从 1 起算。")
    dark: ImageOriginal | None = Field(default=None, title="暗场原图")
    dark_frame: int = Field(default=1, ge=1, title="暗场帧号")
    dark_units_note: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"\S",
        exclude_if=lambda value: value is None, title="暗场与信号读数单位的对应依据",
        description="跨存储类型与 NPY 暗场配准时，请说明两次采集的相机增益、读数单位及同一像元阵列。两份读数应已处于同一标度。")
    background_level: float | None = Field(default=None, title="均匀背景读数", description="按原件读数单位填写，可单独使用均匀背景或配准的暗场。")
    window: tuple[int, int, int, int] | None = Field(default=None, title="像素选区", description="左列、上行、宽、高，零起算。留空取整帧。")
    pitch_x_um: Annotated[float, Field(gt=0, le=1e9)] | None = Field(default=None, title="列方向每像素 / µm")
    pitch_y_um: Annotated[float, Field(gt=0, le=1e9)] | None = Field(default=None, title="行方向每像素 / µm")
    saturation_level: float | None = Field(default=None, title="已知饱和读数", description="填写仪器实际饱和值以统计达到上限的像素。")
    window_coverage: Literal["unverified", "contains_beam", "truncated"] = Field(default="unverified", title="所选像素窗对光斑的覆盖",
        description="若通过扩大选区、不同曝光或独立测量核对过光斑收敛，请在采集依据中说明。整帧也可能截断光斑。",
        json_schema_extra={"x-enum-labels":{"unverified":"尚未核对", "contains_beam":"已核对覆盖光斑", "truncated":"已知截断光斑"}})
    response: Literal['unverified', 'linear', 'assumed_linear', 'nonlinear'] = Field(default='unverified', title="读数与入射光强的关系",
        description="需要用读数代表光强时，请核对仪器响应，或明确选用暂定的线性假设，并在采集依据中说明。",
        json_schema_extra={"x-enum-labels": {"unverified":"尚未核实", "linear":"已知线性响应", "assumed_linear":"本次暂按线性分析", "nonlinear":"已知非线性响应"}})
    wavelength_nm: float | None = Field(default=None, gt=0, title="已知真空波长 / nm", description="多波长或未知时留空。")
    plane: str | None = Field(default=None, min_length=1, max_length=500, pattern=r"\S", title="采集参考面", description="如已知，请填写探测器所在平面及定位基准。")
    basis: str = Field(min_length=1, max_length=2000, pattern=r"\S", title="采集与标定依据",
                       description="说明图像的线性响应、背景依据以及已知的成像倍率或物方像素尺度。未知项可以注明。")

    @model_validator(mode="after")
    def consistent_calibration(self):
        if self.dark is not None and self.background_level is not None:
            raise ValueError("请选择暗场或均匀背景，两者会重复扣除背景。")
        if self.dark is None and (self.dark_frame != 1 or self.dark_units_note is not None):
            raise ValueError("指定暗场帧号或配准依据需要选择暗场原件。")
        return self


diameter = Port(quantity="measurement.windowed-intensity.d4sigma-diameter", label="所选像素窗的二阶矩直径", unit="µm", scale=1e-6,
                available_on_partial=True, publish_when={"response": ("linear", "assumed_linear")})
IMAGE_MOMENTS = Study(
    "measure_image_moments", "图像强度与二阶矩", "3",
    "读取单通道图像或二维实数 NPY 原件，在所选像素窗内保留扣背景后的带符号读数，求质心与 4σ 直径。标定两轴尺度并说明线性响应后，可分别发布原始样本质心和二维强度图，用于观察面位置或光斑形状比较。",
    ImageMoments, image_moments,
    metrics={"center_x_px":"质心列 / px", "center_y_px":"质心行 / px",
             "diameter_x_px":"列向直径 / px", "diameter_y_px":"行向直径 / px",
             "axes_px.major":"长轴直径 / px", "axes_px.minor":"短轴直径 / px", "axes_px.angle_deg":"像素平面长轴角 / °",
             "diameter_x_um":"列向直径 / µm", "diameter_y_um":"行向直径 / µm",
             "axes_um.major":"标定长轴直径 / µm","axes_um.minor":"标定短轴直径 / µm","axes_um.angle_deg":"标定长轴角 / °",
             "sum":"选区净读数总和","negative_pixels":"负读数像素","saturated_pixels":"达饱和值像素"},
    output_ports={"intensity_map": INTENSITY_MAP.model_copy(update={"label":"相机坐标中的标定强度图", "available_on_partial":True}),
                  "centroid":DETECTOR_CENTROID.model_copy(update={"available_on_partial":True}),
                  **{key:diameter.model_copy(update={"label":label}) for key,label in (
        ("diameter_x_um","列向二阶矩直径"),("diameter_y_um","行向二阶矩直径"),
        ("axes_um.major","长轴二阶矩直径"),("axes_um.minor","短轴二阶矩直径"))}},
    output_requirements={"intensity_map": ("pitch_x_um", "pitch_y_um"), "centroid":("pitch_x_um","pitch_y_um")},
    chart=("原件列 / px","原件行 / px",[("二阶矩椭圆","ellipse.x","ellipse.y","lines")]),
    raster=RasterView(x="grid.x_edges",y="grid.y_edges",z="grid.means",label="平均像素读数",
                      x_label="原件列 / px",y_label="原件行 / px",y_down=True,equal_scale=True,pixel_window_input="window"),
    accepts_inputs_from=(("BeamWeft", "1"),("BeamWeft", "2")),
    keywords=("camera", "detector", "intensity", "calibration", "相机", "光斑", "强度图", "标定"),
)

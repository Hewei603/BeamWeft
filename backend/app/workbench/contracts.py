from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from .optics import StrictInput
from ..inventory.crystals import ManualItemCreate, ManualItemPatch


class InventoryEvidence(StrictInput):
    source_quote: str = Field(min_length=1, max_length=2000, description="Literal inventory creation/correction instruction from this run's human request, not a generated plan or old chat.")
    source_attachment_ids: list[int] = Field(default_factory=list, max_length=20, description="Actual case attachment IDs supporting the component specification.")
    unverified_fields: list[str] = Field(default_factory=list, max_length=30, description="Top-level inventory fields to withdraw. Within a partially known crystal, store each reported section in order, leave its unknown properties null and retain its other measurements. Mark crystal_spec here only when the entire structure has no evidentiary basis.")
    uncertainty_notes: list[str] = Field(default_factory=list, max_length=30, description="Uncertain readings and hypotheses, never confirmed physical specifications. Preserve literal label text in raw_spec.")
    stock_confirmed: bool = Field(default=False, description="True only when current user evidence establishes actual owned stock/quantity. Otherwise a new record has quantity 0; it is not an available laboratory part.")


class AgentInventoryItem(ManualItemCreate):
    """Keep the published tool schema reference while using the shared inventory fields."""


class CreateInventoryItem(InventoryEvidence):
    item: AgentInventoryItem


class UpdateInventoryItem(InventoryEvidence):
    inventory_id: int = Field(gt=0)
    patch: ManualItemPatch


class Question(StrictInput):
    question: str = Field(min_length=1)
    why: str = Field(min_length=1, description="Which decision or calculation this unlocks")
    how_to_obtain: str = Field(min_length=1, description="Practical measurement, label, datasheet, or observation to obtain the information")
    expected_format: str = Field(min_length=1, description="Units and an example reply")


class Fact(StrictInput):
    statement: str
    source: str = Field(description="User message, attachment id, inventory id, or actual tool call id; never fabricated")
    kind: Literal["user_stated", "document", "calculated", "hypothesis"]


class Finish(StrictInput):
    status: Literal["completed", "partial", "needs_input", "unsupported"]
    summary: str = Field(min_length=1, max_length=16000)
    findings: list[str] = Field(default_factory=list, max_length=30)
    facts: list[Fact] = Field(default_factory=list, max_length=50)
    assumptions: list[str] = Field(default_factory=list, max_length=30)
    questions: list[Question] = Field(default_factory=list, max_length=6)
    next_steps: list[str] = Field(default_factory=list, max_length=20)
    evidence_tool_call_ids: list[int] = Field(default_factory=list, description="Persisted call ids returned by executed tools, not model call ids")
    diagnostic_tool_call_ids: list[int] = Field(default_factory=list, exclude_if=lambda ids: not ids,
        description="Failed or needs-input calls that establish why an input or calculation could not be verified.")

    @model_validator(mode="after")
    def outcome(self):
        if self.status == "needs_input" and not self.questions:
            raise ValueError("needs_input requires an actionable question including how to obtain the missing information")
        if self.status == "completed" and self.questions:
            raise ValueError("Required unanswered questions mean partial or needs_input, not completed")
        if self.status == "unsupported" and not self.next_steps:
            raise ValueError("unsupported requires a concrete alternative or next step")
        return self


class HandoffRecord(StrictInput):
    tool_call_id: int
    summary: str


class HandoffProgress(HandoffRecord):
    kind: Literal["discovery", "source", "snapshot", "calculation", "operation"]


class HandoffAction(StrictInput):
    actor: str
    action: str
    kind: Literal["review_settings", "inspect_record", "supply_information", "check_owner", "continue_after_fix", "external_action"]


class FailureHandoff(StrictInput):
    category: str
    reason: str
    basis: str
    progress: list[HandoffProgress]
    progress_summary: str
    diagnostics: list[HandoffRecord]
    remaining: str
    next_steps: list[HandoffAction]


class InterruptedResult(Finish):
    """Outcome persisted by the host when there is no final model answer."""
    status: Literal["partial", "failed", "cancelled", "unverified"]
    failure_handoff: FailureHandoff


class InventoryQuery(StrictInput):
    query: str = ""
    category: str | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=40, ge=1, le=100)


class AttachmentQuery(StrictInput):
    attachment_id: int | None = Field(None, gt=0, description="Read this original. Omit to browse archived files.")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=12000, ge=100, le=20000)
    query: str = Field("", max_length=255, description="Match all space-separated text fragments in the archived path or filename.")
    before_id: int | None = Field(None, gt=0, description="Continue a directory listing before this ID.")
    page_size: int = Field(16, ge=1, le=24)

    @model_validator(mode="after")
    def read_or_browse(self):
        if self.attachment_id is not None and (self.query.strip() or self.before_id is not None):
            raise ValueError("按编号读取原件时无需填写目录搜索条件。")
        if self.attachment_id is None and self.offset:
            raise ValueError("读取原件内容的偏移量需要原件编号，目录翻页请用 before_id。")
        return self


class ArtifactQuery(StrictInput):
    content_id: int | None = Field(default=None, gt=0)
    path: str | list[str | int] = Field(default="", description="Path within the stored content. Dot-separated shorthand or an exact array of keys and zero-based indices. Use an array for port names containing dots.")
    offset: int | None = Field(default=None, ge=0, description="Read a page of a stored list or object. For scientific studies, path=['available_outputs'] enumerates the exact connectable port keys.")
    limit: int = Field(default=24, ge=1, le=24, description="Page size when offset is supplied.")
    content_type: str | None = Field(default=None, max_length=50, description="When listing case records, optionally filter by kind, e.g. scientific_study.")
    before_id: int | None = Field(default=None, gt=0, description="Cursor for earlier case records")

    @model_validator(mode="after")
    def lookup_or_list(self):
        if (self.path or self.offset is not None) and self.content_id is None:
            raise ValueError("读取结果字段时请指定记录编号。")
        if self.content_id is not None and (self.content_type is not None or self.before_id is not None):
            raise ValueError("请按编号读取，或通过筛选条件列出记录。")
        return self


class ToolResultQuery(StrictInput):
    tool_call_id: int
    path: str | list[str | int] = Field(default="", description="Path within saved output. Copy the path from read_original, or give dot-separated keys/indices. An array preserves literal keys containing dots. The message wrapper result is outside the saved output.")
    offset: int | None = Field(default=None, ge=0, description="Read a page of a saved list, or of an object's fields in sorted key order, starting at this zero-based index.")
    limit: int = Field(default=24, ge=1, le=24, description="Number of original items when offset is supplied.")


class HistoryQuery(StrictInput):
    before_id: int | None = None
    limit: int = Field(default=20, ge=1, le=50)


class KnowledgeQuery(StrictInput):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)


class LiteratureQuery(StrictInput):
    query: str = Field(min_length=1)
    max_results: int = Field(default=8, ge=1, le=20)
    year_from: int | None = None
    sort: Literal["relevance", "citations", "year"] = "relevance"


class PowerCurve(StrictInput):
    pump_w: list[float] = Field(title="泵浦功率 / W", min_length=3, max_length=3000)
    output_w: list[float] = Field(title="输出功率 / W", min_length=3, max_length=3000)
    pump_basis: Literal["incident", "absorbed", "diode_nominal"] = Field(title="泵浦功率口径", description="incident 指入射功率，absorbed 指吸收功率，diode_nominal 指二极管标称功率。")
    data_source: str = Field(title="数据来源", min_length=1, description="填写测量来源。使用合成数据时请注明。")


class CausticPlaneRegistration(StrictInput):
    z_mm: float = Field(title="参考面在实测坐标中的位置 / mm")
    axis: Literal["tangential", "sagittal"] = Field(title="实测横轴对应的模型轴")
    width_basis: Literal["second_moment", "equivalent"] = Field(title="实测半径口径",
        description="second_moment 表示二阶矩半径 2σ；其他按等效高斯解释的宽度选 equivalent。")
    note: str = Field(min_length=1, max_length=500, pattern=r"\S", title="坐标与介质依据",
        description="说明宽度口径、光束横轴、轴向正方向和到参考面为止的连续均匀介质。")


class CausticData(StrictInput):
    loss: Literal["radius", "squared_radius"] = Field(default="radius", title="拟合误差口径", description="radius 按半径差拟合，各位置等权。squared_radius 按半径平方差拟合，可用于复核采用该口径的旧分析。")
    z_mm: list[float] = Field(title="轴向位置 / mm", min_length=1, max_length=3000)
    radius_mm: list[float] = Field(title="光束半径 / mm", min_length=1, max_length=3000)
    holdout_positions_mm: list[float] | None = Field(default=None, min_length=1, max_length=3000,
        title="留作检验的位置 / mm", description="这些测点只用来检查预测误差。相同位置的重复读数一起留出。")
    acquisition_ids: list[Annotated[str, Field(min_length=1, max_length=500)]] | None = Field(
        default=None, min_length=1, max_length=3000, title="每条读数的采集标识",
        description="若多条记录来自同一次采集，依输入顺序填写对应标识。留出时同一次采集的记录一起分组。")
    wavelength_nm: float | None = Field(default=None, title="波长 / nm", gt=0, description="真空波长，用于换算 M²。")
    medium_n: float | None = Field(default=None, title="介质折射率", gt=0, description="求 M² 时填写该波长下的折射率。若本次按空气近似，填 1。")
    data_source: str = Field(title="数据来源及宽度口径", min_length=1)
    register_plane: CausticPlaneRegistration | None = Field(default=None, title="用于后续传播的参考面",
        description="需要将这次实测用于后续光路时，登记一个同介质截面和测量方向。")

    @model_validator(mode="after")
    def paired_observations(self):
        if len(self.z_mm) != len(self.radius_mm) or (self.acquisition_ids is not None and len(self.acquisition_ids) != len(self.z_mm)):
            raise ValueError("轴向位置、半径以及已填写的采集标识须逐条对应。")
        if any(radius <= 0 for radius in self.radius_mm):
            raise ValueError("每条半径都须大于零，请核对输入以及直径到半径的换算。")
        return self


class DesignSelection(StrictInput):
    target: Literal["cold_cavity", "pump_path", "coupled_layout"] = Field(default="cold_cavity",
        description="Editable model to open. pump_path opens the recorded single-pass pump. coupled_layout registers it on the selected cold cavity. cold_cavity also opens historical cavity candidates.")
    candidate: int = Field(default=1, ge=1, description="One-based saved candidate or cold-cavity row. The independent evidence-only pump has one path.")


class ImportDesignProject(DesignSelection):
    content_id: int = Field(gt=0)
    base_revision_id: int | None = None


class PhaseMatch(StrictInput):
    crystal: Literal["bbo", "lbo", "ktp", "bibo"] = Field(title="非线性晶体")
    lambda1_nm: float = Field(title="第一束波长 / nm", gt=0)
    lambda2_nm: float | None = Field(title="第二束波长 / nm", default=None, gt=0, description="倍频留空，和频填写第二束波长。")
    pm_type: Literal["I", "II"] = Field(default="I", title="匹配类型")


class ComponentMatch(StrictInput):
    requirement: dict[str, Any] = Field(description="Inventory evaluator requirement: category, quantity, roc_mm ('flat' or signed mm), roc_tol_pct, min_diameter_mm, surfaces=[{wavelength_nm:1064, function:'HR', min_R_pct:99.8}]. Unknown properties remain unverified, not automatically matched.")


class Coating(StrictInput):
    config: dict[str, Any] = Field(description="给出 query_wavelengths_nm 与研究输入。stack 需明确 incident、substrate（材料名或折射率）和 layers（裸界面填 []）。每层给 thickness_nm 以及 material 或 n。实测谱使用 vendor_curve 的 wl_nm 和 R/T 分数。")


class LinearDesign(StrictInput):
    message: str = Field(default="", description="Optional purpose of this call. The user's original request supplies the evidence.")
    assumption_policy: Literal["evidence_only", "exploratory"] = Field(
        default="evidence_only",
        description="Use evidence_only for calculations on documented inputs. Choose exploratory when the goal calls for proposing and comparing conditional designs with stated engineering assumptions.",
    )


class SavedAnalysis(StrictInput):
    content_id: int
    candidate: int = Field(default=1, ge=1, le=5)
    mode: Literal["cavity", "pump"] = "cavity"
    output_gap_mm: float | None = Field(default=None, ge=.001, le=2000)
    thermal_f_mm: float | None = Field(default=None, ge=1, le=1e7, description="Physical focal length inside crystal; general analyze_optical_system uses air-equivalent f=thermal_f_mm/n instead.")
    samples: int = Field(default=900, ge=100, le=5000)


class ToolHelp(StrictInput):
    name: str


Positive = Annotated[float, Field(gt=0)]
Nonnegative = Annotated[float, Field(ge=0)]
Share = Annotated[float, Field(ge=0, le=1)]


class RadialHeating(StrictInput):
    absorbed_w: tuple[Nonnegative, Nonnegative] = Field(title="介质体吸收功率 / W", description="本次热源覆盖的介质及光路。区间按下限、上限排列。")
    heat_fraction: tuple[Share, Share] = Field(title="吸收功率的热转化比例", description="填写该工况下的比例或上下限，范围 0–1。")
    length_mm: Positive | None = Field(default=None, title="受热圆柱长度 / mm")
    radius_mm: Positive | None = Field(default=None, title="圆柱外半径 / mm")
    conductivity: Positive | None = Field(default=None, title="径向热导率 / (W·m⁻¹·K⁻¹)")
    zone_edges_mm: list[Positive] | None = Field(default=None, min_length=1, max_length=256, title="各热源环区外半径 / mm",
        description="从轴心向外依次填写。均匀圆斑只需一个半径；最外环至侧壁之间按无热源计算。")
    relative_density: list[Nonnegative] | None = Field(default=None, min_length=1, max_length=256, title="各环区相对热源密度",
        description="与外半径逐项对应，填沿长度平均的单位体积热源相对值。例如空心环可填 0 和 1。求解器按环面积归一化。")
    side_conductance: Positive | None = Field(default=None, title="侧壁换热系数 / (W·m⁻²·K⁻¹)",
        description="已知时同时给出相对均温热沉的温升；留空仍可求出相对侧壁的温差。")
    basis: str = Field(min_length=1,max_length=1600,title="工况与参数依据",
        description="说明热转化比例、热源剖面、热导率和冷却条件的来源或试算假设。")

    @model_validator(mode="after")
    def physical(self):
        if self.absorbed_w[0] > self.absorbed_w[1] or self.heat_fraction[0] > self.heat_fraction[1]:
            raise ValueError("功率与热转化比例的下限须小于或等于上限。")
        if (self.relative_density is not None and not any(self.relative_density)) or (
                self.zone_edges_mm is not None and self.relative_density is not None and len(self.zone_edges_mm) != len(self.relative_density)):
            raise ValueError("请为每个环区提供相对热源密度，且至少有一个正值。")
        if self.zone_edges_mm and (any(a >= b for a,b in zip(self.zone_edges_mm,self.zone_edges_mm[1:]))
                                   or self.radius_mm is not None and self.zone_edges_mm[-1] > self.radius_mm):
            raise ValueError("环区外半径须严格递增，且不能超过圆柱外半径。")
        if not self.basis.strip():
            raise ValueError("请说明热源与材料参数的依据。")
        return self

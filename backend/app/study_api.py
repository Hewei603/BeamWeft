"""Public v1 interface for installed scientific computations; independent of the web application and optical kernel."""
from collections.abc import Callable
from dataclasses import field
from functools import cached_property
import math
from typing import Annotated, BinaryIO, Literal, get_args, get_origin

from pydantic import field_validator, BaseModel, ConfigDict, Field, PrivateAttr, model_validator
from pydantic.dataclasses import dataclass

from .study_compat import condition_names, port_conditions, study_input_schema
from .version import provider_display_name


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class MissingStudyInput(ValueError):
    """A well-formed draft needs more physical input before the study can run."""


class OriginalFile(StrictInput):
    """Case-local archived bytes. The host opens a fresh verified stream for each input."""
    model_config = ConfigDict(json_schema_extra={"x-beamweft-input": "original-file"})
    attachment_id: int = Field(gt=0, description="Archived attachment in this case.")
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$",
                              description="Optional expected SHA-256. Execution records the verified digest.")
    _stream: BinaryIO | None = PrivateAttr(default=None)
    _relative_path: str = PrivateAttr(default="")

    @classmethod
    def recognizes(cls, prefix: bytes, *, complete: bool) -> bool | None:
        """Optional format hint for discovery. Return None when these bytes are inconclusive.

        Only the beginning of the original is supplied. complete indicates that it
        also contains the end of the file. Actual decoding and physical validation
        belong to compute(), including for a recognized container.
        """
        return None

    @property
    def relative_path(self) -> str:
        """Archived logical path, supplied by the host with the verified stream."""
        return self._relative_path

    @property
    def stream(self) -> BinaryIO:
        """Seekable original bytes, available for the duration of compute()."""
        if self._stream is None or self._stream.closed:
            raise ValueError("此计算需要运行时打开的案例原件。")
        return self._stream


def _original_types(annotation, parents=()):
    if isinstance(annotation, type) and issubclass(annotation, OriginalFile):
        yield annotation
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation not in parents:
            for member in annotation.model_fields.values():
                yield from _original_types(member.annotation, (*parents, annotation))
    elif get_origin(annotation) is Annotated:
        yield from _original_types(get_args(annotation)[0], parents)
    else:
        for member in get_args(annotation):
            yield from _original_types(member, parents)


class RasterView(StrictInput):
    """Paths to grid centers or cell edges and a rectangular scalar field, with optional chart overlays."""
    x: str
    y: str
    z: str
    label: str
    value_label_path: str | None = Field(default=None, exclude_if=lambda value: value is None,
        description="Optional output field with the calibrated color scale label for this calculation.")
    native_density: bool = Field(default=False, exclude_if=lambda value: not value,
        description="This raster shows the native grid's cell-integrated signal divided by physical area, in the same coordinates and units.")
    x_label: str
    y_label: str
    y_down: bool = False
    equal_scale: bool = False
    power_db: bool = Field(default=False, description="The nonnegative raster is power or intensity and supports 10 log10 relative to its displayed peak.")
    pixel_window_input: str | None = Field(default=None, description="Optional input accepting [first column, first row, width, height] in this grid's original zero-based pixel coordinates.")


Shape = Literal["scalar", "series", "state", "interval"]


class Condition(StrictInput):
    """A named condition in canonical units, located in the producer output or consumer inputs."""
    path: str = Field(min_length=1)
    label: str = Field(min_length=1)
    value_type: Literal["number", "text"] = "number"
    reference_only: bool = Field(default=False, exclude_if=lambda value: not value,
        description="For impact comparisons this identifies an archived source, independently of physical conditions. Cross-source matching still checks it.")

    def read(self, context):
        for key in self.path.split("."):
            if not isinstance(context, dict) or key not in context:
                return None
            context = context[key]
        if self.value_type == "text" and type(context) is str and context.strip():
            return context
        if self.value_type == "number" and type(context) in (int, float) and math.isfinite(context):
            return context
        return None


class Port(StrictInput):
    """The same quantity ID denotes the same physical meaning and canonical unit across providers."""
    quantity: str = Field(min_length=1)
    label: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    scale: float = Field(gt=0, description="Multiply a numeric value by this factor to obtain the canonical unit.")
    shape: Shape = "scalar"
    available_on_partial: bool = Field(default=False, exclude_if=lambda value: not value,
                                       description="An output with a valid published value remains usable when other results are incomplete.")
    conditions: dict[str, Condition] = Field(default_factory=dict)
    publish_when: dict[str, tuple[str, ...]] = Field(default_factory=dict, exclude_if=lambda value: not value,
        description="Publish only for these solver-declared regimes, keyed by output field path.")
    comparison_conditions: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value,
        description="These declared conditions must agree when comparing this quantity across candidates.")
    registration_label: str | None = Field(default=None, min_length=1)
    inherit_conditions_from: str | None = Field(default=None, description="On an output, carry documented operating conditions from this bound input.")
    alternate_units: dict[str, Annotated[float, Field(gt=0)]] = Field(default_factory=dict, description="Additional source units with their factor to the same canonical unit.")

    @model_validator(mode="before")
    @classmethod
    def accept_archived_conditions(cls, value):
        return port_conditions(value)

    @model_validator(mode="after")
    def state_scale(self):
        if set(self.comparison_conditions) - self.conditions.keys():
            raise ValueError("Comparison conditions must be declared on the port.")
        if self.shape == "state" and self.scale != 1:
            raise ValueError("Structured states must declare their native coordinate convention.")
        if self.unit in self.alternate_units and self.alternate_units[self.unit] != self.scale:
            raise ValueError("Conflicting scales for the declared unit.")
        return self

    def check_conditions(self, source_values, inputs):
        for key, rule in self.conditions.items():
            actual, expected = source_values.get(key), rule.read(inputs)
            if actual is None or expected is None:
                raise ValueError(f"{self.label}需要核对{rule.label}。来源或本次输入缺少这项条件，请选取记录了该条件的来源或补全输入。")
            matches = (math.isclose(actual, expected, rel_tol=1e-12) if isinstance(actual, (int, float))
                       and isinstance(expected, (int, float)) else actual == expected)
            if not matches:
                raise ValueError(f"{self.label}的{rule.label}不匹配：来源为 {actual}，本次模型为 {expected}。请核对所选来源与工况。")


class PublishedPort(Port):
    """The lossless representations offered by this particular saved value."""
    connects_as: tuple[Shape, ...] = ()
    condition_values: dict[str, float | str] = Field(default_factory=dict)

    @classmethod
    def describe(cls, port, value, context=None):
        def number(v):
            return type(v) in (int, float) and math.isfinite(v)

        if context is not None and any(Condition(path=path, label=path, value_type="text").read(context) not in regimes
                                       for path, regimes in port.publish_when.items()):
            return None
        shapes = [port.shape]
        if port.shape == "scalar":
            if not number(value):
                return None
            shapes.append("interval")
        elif port.shape == "interval":
            if not (isinstance(value, (list, tuple)) and len(value) == 2
                    and all(map(number, value)) and value[0] <= value[1]):
                return None
            if value[0] == value[1]:
                shapes.append("scalar")
        elif port.shape == "series":
            if not (isinstance(value, list) and value and all(map(number, value))):
                return None
        elif not (isinstance(value, dict) and value):
            return None
        conditions = dict(getattr(port, "condition_values", {}))
        if context is not None:
            conditions = {k: found for k, rule in port.conditions.items() if (found := rule.read(context)) is not None}
        return cls(**port.model_dump(exclude={"connects_as", "condition_values"}), connects_as=tuple(shapes),
                   condition_values=conditions)

    def value_for(self, target, value, registration=""):
        # Recheck the immutable value at consumption, including in legacy records.
        actual = self.describe(self, value)
        if actual is None:
            raise ValueError(f"已存的{self.label}与其物理量声明不一致。")
        if self.quantity != target.quantity or (self.unit == target.unit and self.scale != target.scale):
            raise ValueError(f"需要{target.label}（{target.unit}），来源声明为{self.label}（{self.unit}）。请核对物理含义和单位。")
        if target.shape not in actual.connects_as:
            shapes = {"scalar": "单值", "interval": "上下界", "series": "数列", "state": "带坐标约定的状态"}
            raise ValueError(f"{target.label}需要{shapes[target.shape]}，这份{shapes[self.shape]}无法无损转换。")
        if self.shape == "state":
            if self.unit != target.unit or not registration.strip():
                requirement = target.registration_label or "参考面与轴向对应关系"
                raise ValueError(f"{target.label}需要一致的状态约定，并注明{requirement}。")
            return value
        if self.shape == "interval" and target.shape == "scalar":
            value = value[0]
        elif self.shape == "scalar" and target.shape == "interval":
            value = [value, value]
        factor = self.scale / target.scale
        if not 0 < factor < math.inf:
            raise ValueError(f"{target.label}的单位比例超出可表示范围。")
        def scale(v):
            result = v * factor
            if not math.isfinite(result) or (v != 0 and result == 0):
                raise ValueError(f"{target.label}换算到 {target.unit} 后超出可表示范围。")
            return result

        converted = [scale(v) for v in value] if target.shape in {"interval", "series"} else scale(value)
        if target.shape == "interval" and value[0] != value[1] and converted[0] == converted[1]:
            raise ValueError(f"{target.label}换算到 {target.unit} 后无法区分两个界限。")
        return converted


class ResultReference(StrictInput):
    run_id: int = Field(gt=0)
    port: str = Field(min_length=1)
    registration: str = Field(default="", max_length=500, description="For a state, document how the conditions in the receiving port's registration label correspond.")
    registration_from: str | None = Field(default=None, min_length=1, max_length=100,
        description="For a shared registration, use the receiving study's declared text input. Saved links include its resolved text.")


class RowOutputs(StrictInput):
    """Quantities available on each row of a saved result table, addressed within that immutable run."""
    columns: dict[str, Port] = Field(min_length=1)
    coordinates: dict[str, str] = Field(default_factory=dict, description="Row fields and captions that identify the reading to a user.")
    identity_fields: tuple[str, ...] = Field(default=(), description="Stable row coordinates across runs, independent of order and display name. Only unique, fully specified identities can be rebound.")
    available_on_partial: bool = Field(default=False, description="Opt in only if returned rows are independently valid when the rest of the calculation is incomplete.")
    where: dict[str, bool] = Field(default_factory=dict, description="Publish a row only when its named boolean fields have exactly these values.")


class ConditionMatch(StrictInput):
    anchor: str
    other: str
    keys: dict[str, str] = Field(min_length=1, description="Condition identifiers and readable captions. Conflicting known values require a documented transfer on the other input.")


    _condition_names = field_validator("keys", mode="before")(condition_names)


class ElementLaw(StrictInput):
    """Expose a deterministic study as a parameterized device on an optical path."""
    representation: Literal["ray_matrix", "scattering"]
    scalar_transfer: Literal["quadratic_phase"] | None = Field(default=None, exclude_if=lambda value: value is None,
        description="Opt in for a lossless thin scalar phase screen with aligned T/S axes. Requires coincident reference planes and matrices [1,0,C,1].")
    wavelength_input: str = Field(min_length=1, description="Field receiving this path's vacuum wavelength in nm.")
    length_input: str | None = Field(default=None, min_length=1, description="For a ray matrix, input for the physical entrance-to-exit separation in mm. Omit for coincident reference planes.")
    output: str = Field(min_length=1, description="Result key containing the transfer matrices.")
    preset: dict = Field(default_factory=dict, description="Initial device settings in addition to input model defaults.")
    ports: tuple[str, ...] = Field(default=(), max_length=16, description="Scattering channels in matrix row/column order.")

    @model_validator(mode="after")
    def port_order(self):
        if self.scalar_transfer and (self.representation != "ray_matrix" or self.length_input):
            raise ValueError("A scalar quadratic phase screen requires a ray law with coincident reference planes.")
        if bool(self.ports) != (self.representation == "scattering"):
            raise ValueError("Declare ports for a scattering law and use separate T/S axes for a ray matrix.")
        if len(set(self.ports)) != len(self.ports) or any(not p.strip() or len(p)>100 for p in self.ports):
            raise ValueError("Scattering ports require distinct, nonempty names of at most 100 characters.")
        return self


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class Study:
    name: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,63}$")]
    title: Annotated[str, Field(min_length=1)]
    version: Annotated[str, Field(min_length=1)]
    scope: Annotated[str, Field(min_length=1)]
    inputs: type[StrictInput]
    compute: Callable[[StrictInput], dict]
    metrics: dict[str, str] = field(default_factory=dict)
    table: tuple[str, dict[str, str]] | None = None
    chart: tuple[str, str, list[tuple[str, str, str, Literal["lines", "markers", "lines+markers"]]]] | None = None
    input_ports: dict[str, Port] = field(default_factory=dict)
    output_ports: dict[str, Port] = field(default_factory=dict)
    view: str | None = None
    paired_inputs: tuple[tuple[str, ...], ...] = ()
    row_outputs: dict[str, RowOutputs] = field(default_factory=dict)
    condition_matches: tuple[ConditionMatch, ...] = ()
    raster: RasterView | None = None
    element_law: ElementLaw | None = None
    keywords: tuple[str, ...] = ()
    output_requirements: dict[str, tuple[str, ...]] = field(default_factory=dict)
    live_layout: str | None = None
    # The same method name with an explicitly compatible input meaning, unit and reference frame.
    accepts_inputs_from: tuple[tuple[str, str], ...] = ()
    layout_prefix_inputs: dict[Literal["path","plane"], str] = field(default_factory=dict)
    registration_inputs: dict[str, str] = field(default_factory=dict)
    # Derive declared ports from a finished calculation when saving. Live previews skip these extra values.
    saved_outputs: Callable[[StrictInput, dict], dict] | None = None
    # Compute the same visible physics without materializing large continuation states during editing.
    preview_compute: Callable[[StrictInput], dict] | None = None
    # For rows in table, these fields are one-based indices into the corresponding
    # input series. A file-backed series retains the original record numbers after filtering.
    table_input_rows: dict[str, str] = field(default_factory=dict)
    # Reproduce the native grid from frozen inputs. The original computation records native_grid_sha256.
    native_grid: Callable[[StrictInput], object] | None = None
    # A live method may reuse a result when only network-component diagram_xy changes.
    # Existing extensions receive the complete layout, including drawing positions.
    ignores_diagram_positions: bool = False

    @cached_property
    def text_column_inputs(self):
        """A declared sequence of plain strings can take labels directly from an original table."""
        fields = {}
        for key, prop in study_input_schema(self.inputs)["properties"].items():
            if key in self.input_ports:
                continue
            variants = [v for v in prop.get("anyOf", [prop]) if v.get("type") != "null"]
            if len(variants) != 1:
                continue
            series = variants[0]
            item = series.get("items", {})
            if series.get("type") == "array" and item.get("type") == "string" and "format" not in item:
                fields[key] = {"label": prop.get("title") or key, "max_items": series.get("maxItems"),
                               "max_length": item.get("maxLength")}
        return fields

    @cached_property
    def layout_selectors(self):
        return tuple(dict.fromkeys(
            field["x-beamweft-input"] for field in study_input_schema(self.inputs)["properties"].values()
            if "x-beamweft-input" in field
        ))

    @cached_property
    def original_inputs(self):
        """Identify the file types accepted by each named argument, including structured arguments."""
        return {key: types for key, member in self.inputs.model_fields.items()
                if (types := tuple(dict.fromkeys(_original_types(member.annotation))))}

    @cached_property
    def uses_original_files(self):
        schema = study_input_schema(self.inputs)
        return any(item.get("x-beamweft-input") == "original-file"
                   for item in (schema, *schema.get("$defs", {}).values()))

    def __post_init__(self):
        if self.raster and self.raster.native_density and not self.native_grid:
            raise ValueError("原生网格密度图需要可复算的全分辨率网格。")
        if self.table_input_rows:
            if not self.table:
                raise ValueError("原始行引用需要声明结果表。")
            for source, index_field in self.table_input_rows.items():
                if source not in self.column_series or not index_field.strip():
                    raise ValueError("原始行引用须指向表列输入和结果行中的序号字段。")
        if self.registration_inputs:
            properties = study_input_schema(self.inputs)["properties"]
            for target, basis in self.registration_inputs.items():
                if (target not in self.input_ports or self.input_ports[target].shape != "state"
                        or properties.get(basis, {}).get("type") != "string" or basis in self.input_ports):
                    raise ValueError("Shared registration requires a state input and a separate authored text field.")
        if self.live_layout:
            port = self.input_ports.get(self.live_layout)
            if not port or port.quantity != "optics.ordered-layout.v1" or port.shape != "state" or self.uses_original_files:
                raise ValueError("A live optical view requires a declared layout input and self-contained computation.")
        elif self.ignores_diagram_positions:
            raise ValueError("Only a live layout study can opt out of schematic component positions.")
        if self.layout_prefix_inputs:
            fields = study_input_schema(self.inputs)["properties"]
            if (not self.live_layout or set(self.layout_prefix_inputs) != {"path","plane"} or
                    any(fields.get(key,{}).get("x-beamweft-input") != kind+"-uid" for kind,key in self.layout_prefix_inputs.items())):
                raise ValueError("A source-to-station preview needs a live layout and named path/plane selectors.")
        for path, inputs in self.output_requirements.items():
            if path not in self.output_ports or set(inputs) - self.inputs.model_fields.keys():
                raise ValueError("Each independently assessed output must address a declared port and known input fields.")
        law = self.element_law
        if law:
            fields = self.inputs.model_fields
            properties = study_input_schema(self.inputs)["properties"]
            if properties.get(law.wavelength_input, {}).get("type") != "number" or self.uses_original_files:
                raise ValueError("An element law requires a numeric wavelength and self-contained model parameters.")
            if law.length_input is not None and (law.representation != "ray_matrix" or law.length_input == law.wavelength_input
                    or properties.get(law.length_input, {}).get("type") != "number"):
                raise ValueError("A physical length input must be a separate numeric ray-device parameter in mm.")
            if law.wavelength_input in law.preset or law.preset.keys() - fields.keys():
                raise ValueError("Device presets may specify only editable inputs; the optical path provides wavelength.")
            if any(f.is_required() and k != law.wavelength_input and k not in law.preset for k,f in fields.items()):
                raise ValueError("Provide an initial device setting for each required editable input.")
        if self.raster and self.raster.pixel_window_input and self.raster.pixel_window_input not in self.inputs.model_fields:
            raise ValueError("A raster window must address a declared study input.")
        if self.input_ports.keys() - self.inputs.model_fields.keys():
            raise ValueError("Each input port must correspond to a declared input field.")
        for match in self.condition_matches:
            if match.anchor == match.other or {match.anchor, match.other} - self.input_ports.keys():
                raise ValueError("A condition comparison requires two distinct declared input ports.")
        for port in [*self.output_ports.values(), *(p for table in self.row_outputs.values() for p in table.columns.values())]:
            if port.inherit_conditions_from is not None and port.inherit_conditions_from not in self.input_ports:
                raise ValueError("Inherited conditions require a declared source input.")
        if any(len(group) < 2 or any(k not in self.column_series for k in group)
               for group in self.paired_inputs):
            raise ValueError("Paired measurements require at least two declared series inputs.")
        for table, rows in self.row_outputs.items():
            paths = (table, *rows.columns, *rows.coordinates, *rows.where, *rows.identity_fields)
            if any(not all(path.split(".")) for path in paths):
                raise ValueError("Result paths need a field name at each level.")
            for path in self.output_ports:
                relative = path.removeprefix(table + ".")
                index, _, column = relative.partition(".")
                if relative != path and index.isdecimal() and column in rows.columns:
                    raise ValueError(f"Output {path} has both a fixed and a row declaration.")

    @cached_property
    def column_series(self):
        return set(self.text_column_inputs) | {key for key, port in self.input_ports.items() if port.shape == "series"}

    def accepts_saved_inputs(self, provider, owner, version):
        archived = (provider_display_name(owner), version)
        return archived == (provider, self.version) or any(
            archived == (provider_display_name(accepted), v) for accepted, v in self.accepts_inputs_from
        )

    def presentation(self, provider):
        return {"name": self.name, "title": self.title, "version": self.version, "provider": provider_display_name(provider),
                "provider_display_name": provider_display_name(provider),
                **({"keywords": self.keywords} if self.keywords else {}),
                "scope": self.scope, "metrics": self.metrics, "table": self.table, "chart": self.chart, "view": self.view,
                **({"table_input_rows": self.table_input_rows} if self.table_input_rows else {}),
                **({"native_grid": True} if self.native_grid else {}),
                "paired_inputs": self.paired_inputs,
                **({"registration_inputs": self.registration_inputs} if self.registration_inputs else {}),
                **({"output_requirements": self.output_requirements} if self.output_requirements else {}),
                **({"live_layout": self.live_layout} if self.live_layout else {}),
                **({"ignores_diagram_positions": True} if self.ignores_diagram_positions else {}),
                **({"layout_prefix_inputs": self.layout_prefix_inputs} if self.layout_prefix_inputs else {}),
                **({"accepts_inputs_from": [(provider_display_name(owner), version) for owner, version in self.accepts_inputs_from]}
                   if self.accepts_inputs_from else {}),
                **({"raster": self.raster.model_dump()} if self.raster else {}),
                **({"element_law": self.element_law.model_dump()} if self.element_law else {}),
                **({"condition_matches": [m.model_dump() for m in self.condition_matches]} if self.condition_matches else {}),
                "input_ports": {k: v.model_dump() for k, v in self.input_ports.items()},
                **({"text_column_inputs": self.text_column_inputs} if self.text_column_inputs else {}),
                "output_ports": {k: v.model_dump() for k, v in self.output_ports.items()},
                **({"row_outputs": {k: v.model_dump() for k, v in self.row_outputs.items()}} if self.row_outputs else {})}

    def describe(self, provider):
        return {**self.presentation(provider), "inputs": study_input_schema(self.inputs)}

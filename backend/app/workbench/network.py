"""Passive single-mode optical ports, connections and excitation at one vacuum wavelength."""
import math
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..study_api import StrictInput
from .device_laws import Prescription
from .component_origin import ComponentOrigin, model_wavelength_notes
from .beam_source import BeamSource
from ..physics.port_network import scattering_matrix, validate_passivity, solve_network

Identifier = Annotated[str, Field(min_length=1, max_length=100, pattern=r"\S")]


def declared_ports(kind, ports):
    """Port identities can be read from an archived prescription without evaluating it."""
    if kind == "coupler":
        return ["1", "2", "3", "4"]
    if kind == "reflector":
        return ["1"]
    return ports or []


class Coupler(StrictInput):
    kind: Literal["coupler"]
    through_fraction: float = Field(ge=0, le=1, description="Power from 1 to 3 or 2 to 4. Cross amplitude has +i phase.")


class Reflector(StrictInput):
    kind: Literal["reflector"]
    reflectance: float = Field(ge=0, le=1)
    phase_deg: float = Field(description="Phase of the reflected field at this reference plane.")


class Scattering(StrictInput):
    kind: Literal["matrix"]
    reference_wavelength_nm: float | None = Field(default=None, gt=0, le=1e6, description="Calibration wavelength; omit only when treating the supplied matrix as constant.")
    ports: list[Identifier] = Field(min_length=1, max_length=16)
    matrix: list[list[tuple[float, float]]] = Field(min_length=1, max_length=16,
        description="Power-normalized complex S[row outgoing][column incoming], as [real,imaginary], at the model wavelength.")

    @model_validator(mode="after")
    def valid_matrix(self):
        n = len(self.ports)
        if len(set(self.ports)) != n or len(self.matrix) != n or any(len(row) != n for row in self.matrix):
            raise ValueError("每个端口需要唯一名称，散射矩阵的行数和列数须等于端口数。")
        validate_passivity(scattering_matrix(self))
        return self


class RegisteredScattering(StrictInput):
    kind: Literal["registered"]
    prescription: Prescription
    ports: list[Identifier] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_ports(self):
        if len(self.ports) != len(set(self.ports)):
            raise ValueError("器件端口名称必须唯一。")
        return self

    def transfer(self, wavelength_nm):
        data = self.prescription.evaluate("scattering", wavelength_nm)
        if data["ports"] != self.ports:
            raise ValueError("工程中保存的通道顺序与器件模型不一致，请核对端口连接。")
        return data


class NetworkComponent(StrictInput):
    uid: Identifier
    name: Identifier
    component_origin: ComponentOrigin | None = Field(default=None, exclude_if=lambda value: value is None)
    diagram_xy: tuple[Annotated[float, Field(ge=0, le=10000)], Annotated[float, Field(ge=0, le=10000)]] | None = Field(default=None, description="Optional schematic position, without optical length or phase.")
    model: Coupler | Reflector | Scattering | RegisteredScattering = Field(discriminator="kind")
    transverse_action: Literal["uniform", "matched_mode"] | None = Field(default=None, exclude_if=lambda v:v is None,
        description="A pointwise uniform device applies the same S at every transverse position. Ideal built-in surfaces assume uniform action. Custom or installed S matrices describe matched modes unless the user declares a uniform surface.")

    def acts_pointwise(self):
        if self.transverse_action is not None:
            return self.transverse_action == "uniform"
        return isinstance(self.model, (Coupler, Reflector))

    def ports(self):
        return declared_ports(self.model.kind, getattr(self.model, "ports", None))


class Terminal(StrictInput):
    component: Identifier
    port: Identifier

    def key(self):
        return (self.component, self.port)


class OpticalLink(StrictInput):
    a: Terminal
    b: Terminal
    length_mm: float = Field(ge=0, le=1e9, description="Physical length, not diagram coordinates.")
    n: float = Field(default=1, gt=0, le=100)
    power_transmission: float = Field(default=1, ge=0, le=1)
    phase_deg: float = Field(default=0, description="Additional reciprocal phase shift, exp(+i phi).")
    shift_t_mm: float = Field(default=0, exclude_if=lambda x:x==0, description="Lateral translation from a to b in their registered unfolded T axes. The reverse translation has opposite sign.")
    shift_s_mm: float = Field(default=0, exclude_if=lambda x:x==0, description="Lateral translation from a to b along S, in mm.")


class ElementReference(StrictInput):
    scope: Literal["host", "arm"]
    element_uid: Identifier


class PathFeed(ElementReference):
    path_uid: Identifier


class GaussianProfile(BeamSource):
    kind: Literal["gaussian"]


class LayoutProfile(StrictInput):
    kind: Literal["layout"]
    beam_uid: Identifier
    plane_uid: Identifier | None = None
    element_exit: ElementReference | None = Field(default=None, exclude_if=lambda value: value is None,
        description="Outgoing face of a specific element, even after other optics are added downstream.")

    @model_validator(mode="after")
    def one_reference(self):
        if self.plane_uid and self.element_exit:
            raise ValueError("光场只能引用一个观察面或一个元件出射面。")
        return self


class NetworkDrive(StrictInput):
    at: Terminal
    power_w: float | None = Field(default=None, ge=0)
    path_output: PathFeed | None = Field(default=None, description="Use the exact forward power at this engineering reference plane.")
    coherence_group: Identifier | None = Field(default=None, description="Unspecified sources are mutually incoherent. Equal group names mean phase locked.")
    phase_deg: float | None = Field(default=None, description="Field phase against a common group reference. Required for phase-locked drives.")
    profile: GaussianProfile | LayoutProfile | None = Field(default=None, discriminator="kind",exclude_if=lambda v:v is None,
        description="Transverse field at the input port. A layout reference transports the actual complex envelope to the named plane, or to the path exit. Port watts set the delivered power.")

    @model_validator(mode="after")
    def source(self):
        if self.path_output and self.power_w is not None:
            raise ValueError("一个入射端口只能选取一个功率来源。")
        return self


class PortNetwork(StrictInput):
    wavelength_nm: float | None = Field(default=None, gt=0, le=1e6, description="Vacuum wavelength; in an engineering project omission inherits the main path wavelength.")
    components: list[NetworkComponent] = Field(default_factory=list, max_length=128)
    links: list[OpticalLink] = Field(default_factory=list, max_length=128)
    drives: list[NetworkDrive] = Field(default_factory=list, max_length=128)
    basis: str = Field(default="", max_length=2000, description="Basis for scattering, reference planes and spatial/polarization mode matching.")

    @model_validator(mode="after")
    def connections(self):
        names = [c.uid for c in self.components]
        ports = {(c.uid, p) for c in self.components for p in c.ports()}
        if len(names) != len(set(names)) or len(ports) > 256:
            raise ValueError("元件标识须唯一，每个网络最多支持 256 个端口。")
        feeds = [d.path_output.path_uid for d in self.drives if d.path_output]
        if len(set(feeds)) != len(feeds):
            raise ValueError("同一条顺序路径的功率只能注入网络一次。如需分路，请连接分束元件。")
        occupied = set()
        for terminal in [t for link in self.links for t in (link.a, link.b)] + [d.at for d in self.drives]:
            key = terminal.key()
            if key not in ports:
                raise ValueError(f"找不到端口 {key[0]} / {key[1]}，请核对元件及其端口。")
            if key in occupied:
                raise ValueError(f"端口 {key[0]} / {key[1]} 已连接。请解除原连接后再使用。")
            occupied.add(key)
        return self


class NetworkStudy(PortNetwork):
    wavelength_nm: float = Field(gt=0, le=1e6, title="真空波长 / nm")


def network_sources(network, wavelength_nm, budgets=()):
    feeds = {(b["path_uid"], row["scope"], row["uid"]): (b, row)
             for b in budgets for row in b.get("rows", [])}
    sources, origins = [], []
    for drive in network.drives:
        power, issue = drive.power_w, ""
        ref = drive.path_output
        if ref:
            entry = feeds.get((ref.path_uid, ref.scope, ref.element_uid))
            if not entry or not math.isclose(entry[0]["wavelength_nm"], wavelength_nm, rel_tol=1e-12):
                issue = "需在同一波长核对上游路径、元件及其功率预算。"
            else:
                budget, row = entry
                watts = row["outgoing"].get("watts")
                origins.append({"terminal":drive.at.model_dump(), "path_output":ref.model_dump(),
                                "path_name":budget["name"], "element_name":row["name"], "watts":watts,
                                "source_power":budget.get("input_spec"), "missing":budget.get("missing", [])})
                if watts is not None and watts[0] == watts[1]:
                    power = watts[0]
                else:
                    issue = "上游出射功率未确定。请补充绝对功率，或另存有依据的单值试算。"
        if power is None and not issue:
            issue = "需填写入射功率或选择工程来源。"
        if drive.coherence_group and drive.phase_deg is None:
            issue = "同一相干组需要明确每个入射端口的参考相位。"
        sources.append((drive, power, issue))
    return sources, origins


def evaluate_network(network, wavelength_nm, budgets=()):
    wavelength_nm = network.wavelength_nm or wavelength_nm
    if wavelength_nm is None:
        raise ValueError("端口网络需要网络波长或主光路波长。")
    sources, origins = network_sources(network, wavelength_nm, budgets)
    result = solve_network(network, wavelength_nm, sources)
    result["warnings"].extend(model_wavelength_notes(network.components, wavelength_nm))
    result["origins"] = origins
    return result

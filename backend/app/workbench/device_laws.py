"""Resolve optical element prescriptions against the same installed studies used by the workbench."""
import json
import logging
import math
import re
from functools import lru_cache

from pydantic import Field, JsonValue

from ..solver_receipt import device_runtime_key
from ..study_api import StrictInput
from ..version import provider_display_name

log = logging.getLogger(__name__)


class Prescription(StrictInput):
    study: str = Field(min_length=1)
    version: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    parameters: dict[str, JsonValue] = Field(default_factory=dict)

    def definition(self, representation):
        from .tools import STUDIES
        entry = STUDIES.get(self.study)
        if entry:
            study, provider = entry
            law = study.element_law
            if provider == self.provider and study.version == self.version and law and law.representation == representation:
                return study
        raise ValueError(f"重算此元件需要 {provider_display_name(self.provider)} 中的 {self.study} v{self.version}（{representation}）。可查看保存时的计算结果。")

    def evaluate(self, representation, wavelength_nm, length_mm=0):
        if wavelength_nm is None:
            raise ValueError("参数化元件需要所在光路的真空波长。")
        installed = self.definition(representation)
        state = device_runtime_key(installed.compute)
        transfer = _transfer if state is not None else _transfer.__wrapped__
        return json.loads(transfer(_InstalledLaw(installed), state, self.model_dump_json(), representation, wavelength_nm, length_mm))


class _InstalledLaw:
    """A cache key retaining the actual registered Study, whose schema dictionaries are unhashable."""
    __slots__ = ("study",)

    def __init__(self, study):
        self.study = study

    def __hash__(self):
        return id(self.study)

    def __eq__(self, other):
        return isinstance(other, _InstalledLaw) and self.study is other.study


@lru_cache(maxsize=256)
def _transfer(installed, state, recipe_json, representation, wavelength_nm, length_mm):
    recipe = Prescription.model_validate_json(recipe_json)
    study = installed.study
    law = study.element_law
    bound = {law.wavelength_input:wavelength_nm}
    if law.length_input:
        bound[law.length_input] = length_mm
    elif length_mm:
        raise ValueError("此器件模型的两个参考面重合，请设置零长度或选择声明了物理长度的模型。")
    if bound.keys() & recipe.parameters.keys():
        raise ValueError("元件波长和声明的参考面距离由光路提供，请移除重复的元件参数。")
    try:
        inputs = study.inputs.model_validate({**recipe.parameters, **bound})
        result = study.compute(inputs)
        if result.get("status", "completed") != "completed":
            raise ValueError(result.get("message") or result.get("summary") or "模型缺少有效的传递结果。")
        value = result.get(law.output)
        if not isinstance(value, dict):
            raise ValueError(f"缺少声明的传递结果 {law.output}。")
        if representation == "scattering":
            from .network import Scattering
            data = Scattering.model_validate({"kind":"matrix", **value})
            if tuple(data.ports) != law.ports:
                raise ValueError("求解器返回的端口顺序与器件声明不同。")
            if data.reference_wavelength_nm is not None and not math.isclose(data.reference_wavelength_nm, wavelength_nm, rel_tol=1e-12):
                raise ValueError("返回矩阵的参考波长与当前光路不同。")
            value = {"ports":data.ports, "matrix":data.matrix}
        else:
            from .optics import OpticalElement
            if value.keys() - {"matrix_t", "matrix_s"}:
                raise ValueError("近轴传递结果应包含 matrix_t 及可选的 matrix_s。")
            data = OpticalElement(name=study.title, kind="matrix", **value)
            value = {"matrix_t":data.matrix_t, "matrix_s":data.matrix_s if data.matrix_s is not None else data.matrix_t}
            if law.scalar_transfer == "quadratic_phase" and any(
                (a, b, d) != (1., 0., 1.) for a, b, _, d in value.values()
            ):
                raise ValueError("标量二次相位模型须返回形如 [1, 0, C, 1] 的薄相位面矩阵。")
        value["scope"] = study.scope
        value["inputs"] = inputs.model_dump(mode="json")
        warnings = result.get("warnings") or []
        if not isinstance(warnings, list) or not all(isinstance(note,str) for note in warnings):
            raise ValueError("器件模型的 warnings 应为说明文字列表。")
        if warnings:
            value["warnings"] = warnings
        return json.dumps(value, allow_nan=False)
    except ValueError as exc:
        raise ValueError(f"元件「{study.title}」在 {wavelength_nm:g} nm 的计算未完成。{exc}") from exc
    except Exception as exc:
        log.exception("Device law %s failed", study.name)
        raise ValueError(f"元件「{study.title}」的求解器执行失败（{type(exc).__name__}），请核对参数或扩展版本。") from exc


@lru_cache(maxsize=128)
def device_input_schema(model):
    return model.model_json_schema()


def editable_setting(schema, value, path, root, seen=()):
    """Read model editability annotations. Pydantic validates parameter values and candidate combinations."""
    if isinstance(schema, bool):
        return schema
    if schema.get("type") == "null" and value is not None:
        return True  # Nullable Pydantic fields carry a second, inactive branch.
    if schema.get("readOnly") or "const" in schema or schema.get("x-laserclaw-input") == "original-file":
        return False
    if any(key in schema for key in ("if", "dependentSchemas", "not")):
        return False  # An opaque conditional has no unconditional edit permission.
    if ref := schema.get("$ref"):
        if not ref.startswith("#/") or ref in seen:
            return False
        target = root
        for key in ref[2:].split("/"):
            target = target.get(key.replace("~1", "/").replace("~0", "~"))
            if target is None:
                return False
        if not editable_setting(target, value, path, root, (*seen, ref)):
            return False
    choices = [*schema.get("anyOf", ()), *schema.get("oneOf", ())]
    tagged = schema.get("discriminator", {})
    if tagged.get("mapping") and isinstance(value, dict):
        variant = tagged["mapping"].get(str(value.get(tagged["propertyName"])))
        if variant is None:
            return False
        choices = [{"$ref":variant}]
    # For an untagged ambiguous union the path must be writable in each possible model.
    if any(not editable_setting(variant, value, path, root, seen) for variant in [*choices, *schema.get("allOf", ())]):
        return False
    if not path:
        return True
    key, *rest = path
    if isinstance(value, dict):
        props = schema.get("properties", {})
        controls = [s for pattern,s in schema.get("patternProperties", {}).items() if re.search(pattern,key)]
        if key in props:
            controls.append(props[key])
        if not controls:
            controls.append(schema.get("additionalProperties", "properties" not in schema))
    else:
        prefix = schema.get("prefixItems", ())
        controls = [prefix[key] if key < len(prefix) else schema.get("items", True)]
    return all(editable_setting(control, value[key], rest, root) for control in controls)


def scalar_scan_fields(study):
    """The classic ABCD scan accepts one flat real setting per axis."""
    schema = device_input_schema(study.inputs)
    law = study.element_law
    return [key for key, prop in schema["properties"].items()
            if prop.get("type") == "number" and key != law.wavelength_input
            and editable_setting(schema, {key: prop.get("default")}, [key], schema)]


def device_definition(study, provider):
    law = study.element_law
    schema = study.inputs.model_json_schema()
    params = {k:p["default"] for k,p in schema["properties"].items() if "default" in p and k != law.wavelength_input}
    params.update(law.preset)
    length = params.pop(law.length_input, 0)
    recipe = Prescription(study=study.name, version=study.version, provider=provider, parameters=params)
    return {"title":study.title, "scope":study.scope, "inputs":schema, **law.model_dump(mode="json"),
            "scalar_scan_fields":scalar_scan_fields(study),
            "length_mm":length, "prescription":recipe.model_dump()}


def device_catalog():
    from .tools import STUDIES, STUDY_ISSUES
    devices = {}
    for study, provider in STUDIES.values():
        if study.element_law:
            devices[study.name] = device_definition(study, provider)
    return devices, STUDY_ISSUES

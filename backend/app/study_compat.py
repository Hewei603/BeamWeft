"""Read earlier scientific extension contracts without changing stored evidence or third-party names."""
from uuid import NAMESPACE_URL, uuid5

MODEL_REVISION = "beamweft.project-revision"
_EARLIER_REVISION = "laserclaw.project-revision"
INPUT_MARKER = "x-beamweft-input"
EARLIER_STUDY_GROUP = "laserclaw.studies.v1"


def persistent_optic_uid(kind, source):
    # These UUID seeds were used for saved assembly membership, launch paths and retry keys.
    return str(uuid5(NAMESPACE_URL, f"laserclaw:{kind}:{source}"))


def study_input_schema(model):
    """Publish one spelling of our schema annotation, including for installed earlier plugins."""
    schema = model.model_json_schema()

    def visit(node):
        if not isinstance(node, dict):
            return
        if "x-laserclaw-input" in node:
            node.setdefault(INPUT_MARKER, node.pop("x-laserclaw-input"))
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
            for sub in node.get(key, {}).values():
                visit(sub)
        for key in ("items", "additionalProperties", "contains", "if", "then", "else", "not"):
            visit(node.get(key))
        for key in ("prefixItems", "oneOf", "anyOf", "allOf"):
            for sub in node.get(key, ()):
                visit(sub)

    visit(schema)
    return schema


def condition_names(values):
    """Translate only the host's renamed condition. Distinct evidence must never collapse silently."""
    if not isinstance(values, dict) or _EARLIER_REVISION not in values:
        return values
    result = values.copy()
    old = result.pop(_EARLIER_REVISION)
    if MODEL_REVISION in result and result[MODEL_REVISION] != old:
        raise ValueError("同一输出记录了两个不一致的工程版本，请核对原始计算。")
    result[MODEL_REVISION] = old
    return result


def port_conditions(port):
    if not isinstance(port, dict):
        return port
    result = dict(port)
    for field in ("conditions", "condition_values"):
        if field in result:
            result[field] = condition_names(result[field])
    if "comparison_conditions" in result and isinstance(result["comparison_conditions"], (tuple, list)):
        result["comparison_conditions"] = [
            MODEL_REVISION if name == _EARLIER_REVISION else name for name in result["comparison_conditions"]
        ]
    return result

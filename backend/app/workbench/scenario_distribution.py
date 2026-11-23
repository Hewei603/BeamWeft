"""Mass of joint laboratory conditions on a finite, already evaluated search grid."""
from fractions import Fraction
import sys
from typing import Annotated, Literal

from pydantic import Field, FiniteFloat, StrictFloat, StrictInt, StringConstraints, model_validator

from ..study_api import OriginalFile, StrictInput
from .study_search import MAX_POINTS


MassWeight = (Annotated[StrictInt, Field(ge=0, le=int(sys.float_info.max))] |
              Annotated[StrictFloat, Field(ge=0, le=sys.float_info.max)])


class ScenarioWeight(StrictInput):
    coordinates: list[Annotated[FiniteFloat, Field(strict=True)]] = Field(max_length=128, description=(
        "One complete joint setting of the observed and hidden axes, in original axis/column order."))
    weight: MassWeight = Field(description=(
        "Relative mass for this condition. Zero explicitly excludes it from this distribution."))


class ScenarioDistribution(StrictInput):
    basis: Literal["observed", "assumed"]
    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1500)]
    originals: list[OriginalFile] = Field(default_factory=list, max_length=32, description=(
        "Optional archived files supporting the stated weights. The host checks identity and pins the bytes. "
        "The source description explains how those files support the values."))
    scenarios: list[ScenarioWeight] = Field(min_length=1, max_length=MAX_POINTS, description=(
        "Every distinct observed/hidden condition of the recorded grid, exactly once. "
        "Weights describe a joint distribution shared by all designs and controls."))

    @model_validator(mode="after")
    def validate_masses(self):
        if len({item.attachment_id for item in self.originals}) != len(self.originals):
            raise ValueError("同一依据原件只需引用一次。")
        if len({tuple(s.coordinates) for s in self.scenarios}) != len(self.scenarios):
            raise ValueError("同一联合工况只能填写一次权重。")
        if not any(s.weight > 0 for s in self.scenarios):
            raise ValueError("联合工况中至少需要一个正权重。")
        return self


def _readings(**fractions):
    """Keep exact endpoint evidence when an interior probability rounds to 0 or 1 in JSON."""
    values = {key: float(value) for key, value in fractions.items()}
    exact = {key: str(value) for key, value in fractions.items() if 0 < value < 1 and values[key] in (0, 1)}
    if exact:
        values["exact"] = exact
    return values


def _add_option_masses(selection, cells, fractions):
    """Choose one control common to every condition represented in this selection."""
    lower, upper = [], []
    for option in selection["options"]:
        counts = dict.fromkeys(("pass", "outside", "unknown"), Fraction(0))
        for index in option["cell_indices"]:
            cell = cells[index]
            counts[cell["state"]] += fractions[cell["scenario_index"]]
        low = counts["pass"]
        high = low + counts["unknown"]
        option["mass"] = _readings(**counts, lower=low, upper=high)
        lower.append(low)
        upper.append(high)
    best_lower, best_upper = max(lower), max(upper)
    selection["mass"] = _readings(lower=best_lower, upper=best_upper)
    selection["lower_option_indices"] = [i for i, value in enumerate(lower) if value == best_lower]
    selection["upper_option_indices"] = [i for i, value in enumerate(upper) if value == best_upper]
    return best_lower, best_upper


def apply_distribution(report, distribution):
    fields = [(path, axis["role"]) for axis in report["axes"] for path in axis["paths"]]
    columns = [i for i, (_, role) in enumerate(fields) if role in ("observed", "hidden")]
    conditions = [tuple(cell["coordinates"][i] for i in columns) for cell in report["cells"]]
    indices = {tuple(s.coordinates): i for i, s in enumerate(distribution.scenarios)}
    wanted = set(conditions)
    extra = next((point for point in indices if point not in wanted), None)
    if extra is not None:
        raise ValueError(f"权重表中的联合工况 {list(extra)} 不属于原网格。请按工况列的原顺序填写。")
    missing = next((point for point in conditions if point not in indices), None)
    if missing is not None:
        raise ValueError(f"缺少联合工况 {list(missing)} 的权重。若该工况不可能出现，请显式填写 0。")
    # Exact relative masses preserve ties, extreme ratios, and control selection even when
    # ordinary binary64 addition overflows or rounds a strictly positive tail away.
    weights = [Fraction(str(s.weight)) for s in distribution.scenarios]
    total = sum(weights)
    fractions = [w / total for w in weights]
    report["distribution"] = {
        "basis": distribution.basis, "source": distribution.source, "paths": [fields[i][0] for i in columns],
        "scenarios": [{**s.model_dump(), **_readings(fraction=f)} for s, f in zip(distribution.scenarios, fractions)],
    }
    for cell, condition in zip(report["cells"], conditions):
        cell["scenario_index"] = indices[condition]
    for design in report["designs"]:
        _add_option_masses(design["fixed"], report["cells"], fractions)
        adaptive_lower = adaptive_upper = Fraction(0)
        for group in design["adaptive"]["groups"]:
            low, high = _add_option_masses(group, report["cells"], fractions)
            adaptive_lower += low
            adaptive_upper += high
            # Every possible control covers exactly the same observed/hidden conditions.
            members = group["options"][0]["cell_indices"]
            group.update(_readings(fraction=sum(fractions[report["cells"][i]["scenario_index"]] for i in members)))
        design["adaptive"]["mass"] = _readings(lower=adaptive_lower, upper=adaptive_upper)

"""Evaluate which adjustments are supported by a saved, discrete parameter grid."""
from collections import Counter, defaultdict
from typing import Annotated, Literal

from pydantic import Field, FiniteFloat
from sqlalchemy.orm import Session

from ..study_api import StrictInput
from .scenario_distribution import ScenarioDistribution, apply_distribution
from .study_search import StudySearch
from .measurement_sources import case_original_snapshot
from .study_tolerance import hard_limits, read_tolerance_source, require_all_samples, sampled_cells


class AdjustmentPolicy(StrictInput):
    search_id: int = Field(gt=0, description="Archived parameter comparison in this project.")
    roles: list[Literal["design", "observed", "hidden", "control"]] = Field(
        min_length=1, max_length=8,
        description="One role per recipe axis, in order. Design is selected before assembly. "
        "Observed conditions can be distinguished experimentally before setting controls. Hidden "
        "conditions must share the same control setting. A coupled axis keeps one role for all its paths.")
    distribution: ScenarioDistribution | None = Field(default=None, description=(
        "Optional sourced mass for every joint observed/hidden condition. Omitting it keeps the "
        "all-conditions assessment; grid point counts do not imply probabilities."))
    focus_design: list[Annotated[FiniteFloat, Field(strict=True)]] | None = Field(default=None, max_length=128,
        description="Optional physical coordinates of the design under discussion, in the original order of all "
                    "columns assigned role design. Use its actual sampled values, including every coupled column. "
                    "Opens that design while retaining the complete comparison.")


def focused_design_index(designs, coordinates):
    for index, design in enumerate(designs):
        if design["design"] == coordinates:
            return index
    raise ValueError("未找到所选设计坐标。请按原比较中各设计轴及其联动列的顺序，填写同一方案的完整采样值。")


def _choose_one(states):
    if "pass" in states:
        return "pass"
    return "unknown" if "unknown" in states else "outside"


def _options(indices, controls, cells):
    by_control = defaultdict(list)
    for index in indices:
        by_control[controls[index]].append(index)
    options = []
    for control, members in by_control.items():
        counts = Counter(cells[i]["state"] for i in members)
        options.append({"control": list(control), "state": require_all_samples(counts),
                        "counts": {state: counts[state] for state in ("pass", "outside", "unknown")},
                        "cell_indices": members})
    return {"state": _choose_one({option["state"] for option in options}), "options": options}


def inspect_adjustment_policy(bind, case_id, args):
    report, digests = read_tolerance_source(bind, case_id, args.search_id)
    recipe = StudySearch.model_validate(report["recipe"])
    if len(args.roles) != len(recipe.axes):
        raise ValueError("请为原网格中的每个参数组指定一个装调角色，联动参数须一起选择。")
    limits = hard_limits(recipe, report)
    cells = sampled_cells(recipe, report["rows"], limits, digests)
    columns = [role for role, axis in zip(args.roles, recipe.axes) for _ in axis.paths]
    by_design = defaultdict(list)
    observable_conditions, controls = [], []
    for index, cell in enumerate(cells):
        coordinates = defaultdict(list)
        for role, value in zip(columns, cell["coordinates"]):
            coordinates[role].append(value)
        by_design[tuple(coordinates["design"])].append(index)
        observable_conditions.append(tuple(coordinates["observed"]))
        controls.append(tuple(coordinates["control"]))

    designs = []
    for design, members in by_design.items():
        by_observation = defaultdict(list)
        for index in members:
            by_observation[observable_conditions[index]].append(index)
        groups = [{"observed": list(observed), **_options(indices, controls, cells)}
                  for observed, indices in by_observation.items()]
        designs.append({"design": list(design), "fixed": _options(members, controls, cells),
                        "adaptive": {"state": require_all_samples({group["state"] for group in groups}),
                                     "groups": groups}})

    result = {**args.model_dump(exclude={"distribution"}, exclude_none=True), "baseline_id": report["baseline_id"],
            "axes": [{"paths": axis.paths, "role": role} for role, axis in zip(args.roles, recipe.axes)],
            "limits": limits, "designs": designs, "cells": cells,
            "scope": "仅覆盖原比较的离散采样点。测后调节以实验时能分辨所选工况、且对应的联动设定实际可达为前提。"}
    if args.focus_design is not None:
        result["designIndex"] = focused_design_index(designs, args.focus_design)
    if args.distribution is not None:
        apply_distribution(result, args.distribution)
        if args.distribution.originals:
            originals = []
            with Session(bind) as db:
                for ref in args.distribution.originals:
                    with case_original_snapshot(db, case_id, ref.attachment_id) as (_, evidence):
                        if ref.sha256 and ref.sha256 != evidence["sha256"]:
                            raise ValueError(f"依据原件 #{ref.attachment_id} 与所选指纹不符，请核对资料版本。")
                        originals.append({"attachment_id":ref.attachment_id, "sha256":evidence["sha256"],
                                          "relative_path":evidence["path"]})
            result["distribution"]["originals"] = originals
    return result

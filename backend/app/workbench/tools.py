"""Executable capability registry, shared by discovery, validation and dispatch."""
from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path

import numpy as np

from ..agent.tools import search_payload
from ..auth.security import Principal
from ..config import get_settings
from ..version import provider_display_name
from ..design.brief import merge_facts
from ..design.analysis import analyze_design
from ..design.projection import accepted_updates, record_projection
from ..design.service import inventory_snapshot, read_design_files, specialized_projection
from ..design.solver import solve_design
from .component_selection import replacement_optical_results, stock_candidates
from .projects import PreviewReplacement, ReplacementCandidates
from ..inventory.evaluator import evaluate_candidates
from ..inventory.crystals import ManualItemCreate, ManualItemPatch
from ..inventory.service import create_item, update_item
from ..attachment_files import read_file
from ..literature.service import run_literature_search
from ..models import AgentChatMessage, AgentTask, AgentToolCall, Attachment, GeneratedContent, InventoryItem, User
from ..observability.usage import apply_usage_to_generated
from .observation import OpticalLayout, PowerAnalysis, host_positions, locate_planes, power_for_layout
from .mode_overlap import InspectModeOverlap, SECTION_MODE_STUDY, evaluate_overlap, overlap_input
from .prediction_comparisons import PredictionComparison, agent_compare_predictions
from .prediction_checks import PredictionCheck, agent_check_prediction
from .beam_readings import BeamReading, register_reading, readings_for_revision
from .beam_comparisons import CompareBeam, ReadingQuery, agent_compare_reading, observation_index
from ..auth.acl import assert_case_edit, assert_case_view
from .component_models import CaptureModel, LibraryQuery, PlaceModel, capture_model, library, place_model
from .measurement_view import InspectMeasurement, agent_inspect_measurement
from .study_inputs import AdoptSnapshot, BeginStudy, adopt_snapshot, begin_study, case_for_planning, save_working_project
from .project_outputs import ProjectOutputQuery, discover_project_outputs
from .model_comparison import CompareProjectInputs, RecheckProject, compare_project_inputs, read_recheck_source, recheck_project
from .power_measurements import MeasurePower, MeterHistory, agent_measure, meter_history
from .beam_studies import BeamStudy, StudyHistory, beam_study_history, run_agent_beam_study
from .measured_launch import MeasuredLaunch, bind_measured_launch
from .analyses import ProjectScan, ProjectScan2D, run_scan
from .projects import (SaveProject, ProjectLookup, ReadInventoryItem, ReplaceComponent, ReplaceComponentTool,
                       ReplaceCrystalTool, read_project, model_version_index, replace_component)
from .design_import import independent_design, saved_geometry
from .contracts import ImportDesignProject
from .studies import ConnectedStudy, discover_studies, inspect_native_grid, installed_study_receipt, run_study
from .grid_export import InspectNativeGrid, native_grid_access
from .study_replay import StudyReplay, replay_study, study_project_basis
from .study_layouts import begin_study_input, study_layout_inputs
from .study_usage import StudyUsage, study_usage
from .study_tolerance import StudyTolerance, inspect_tolerance
from .adjustment_policy import AdjustmentPolicy, inspect_adjustment_policy
from .policy_records import save_policy_comparison
from .tolerance_refinement import RefineStudyTolerance, refine_tolerance
from .study_search import StudyReassessment, StudySearch, layout_search_parameters, reassess_search, run_search, search_measures
from ..project_details import ProjectDetails, project_details
from .discovery import CapabilityDirectory, CapabilitySearch
from .context import attachment_page
from .device_laws import device_definition
from .contracts import (ArtifactQuery, AttachmentQuery, ComponentMatch, Finish, HistoryQuery,
                        InventoryQuery, KnowledgeQuery, LinearDesign, LiteratureQuery,
                        SavedAnalysis, ToolHelp, ToolResultQuery, CreateInventoryItem, UpdateInventoryItem)

REGISTRY = {
    "check_prediction_reading": (PredictionCheck, "Test the two immutable forecasts from an existing beam_prediction_comparison against a registered beam_observation, including observations registered on a third project version. 核对补测结果、检验事先预测、各模型的数据用途。 Read original provenance, per-model residuals and compatibility with the saved reading intervals. Evaluate data reuse for each model separately. Use read_artifact by content_type to locate forecasts and read_beam_readings(all_revisions=true) to find observations. A saved match to one sampled point does not establish a unique cause."),
    "inspect_adjustment_policy": (AdjustmentPolicy, "Compare fixed and measurement-dependent controls across saved candidates. 方案稳健性、装调、补偿、不可观测误差、联合工况频次、加权达标比例。 Assign roles in recipe axis order. Observed means distinguishable before adjustment. Optional joint weights require a stated basis and cover the sampled conditions. Missing results give a confirmed lower bound and a possible upper bound. Without weights only qualify sampled settings against hard limits. When discussing a particular design, supply its physical coordinates in focus_design. Read-only."),
    "save_adjustment_evaluation": (AdjustmentPolicy, "Save a reproducible qualification in the project's comparison history. 归档工况依据、调节策略及达标结果。 Use the same roles and optional joint distribution as inspect_adjustment_policy. If the conclusion addresses one specific design, include its sampled physical coordinates in focus_design so reopening selects it in the full table. Preserves the input table, evidence and results for manual reopening and project-file exchange."),
    "inspect_study_tolerance": (StudyTolerance, "Inspect sampled adjustment margins around a particular row of a saved parameter comparison. 装调余量、公差、允许偏差、容差范围、限制工况。 Use recorded absolute limits. Choose whether to fix other groups or require all sampled conditions to pass. Stops at the first failed or unknown sample, and identifies the archived witnesses. A sampled edge does not guarantee continuous acceptance or yield. Read-only; works without the solver."),
    "refine_study_tolerance": (RefineStudyTolerance, "Narrow the bracket between adjacent passing and failing simulated settings. 补算、加密、缩小装调边界区间。 Inspect the tolerance first. Choose a side and a resolution in the input coordinate's units. With all other settings fixed, check up to 16 midpoints of one continuous input using the original solver or its matching saved results. The linked comparison preserves endpoints and tested midpoints. refinement_execution counts newly computed settings, reused results and settings without a run for this call."),
    "reassess_study_search": (StudyReassessment, "Evaluate the same saved candidates under revised objectives or acceptance limits. 重评已存方案、改变偏好、只筛选达标项。 Use the id of a study_search record and measures in the baseline units. Read its assessment_options for output keys and units. Use min, max or target (closest to a specified scalar), with optional hard bounds on raw values. Preserves candidate identities, partial evidence and the original comparison. Reads frozen evidence without executing any solver. For different physical inputs or a new grid use search_study_parameters."),
    "inspect_native_grid": (InspectNativeGrid, "Verify a saved native-resolution numerical grid from its frozen inputs and original files. Returns a checked digest, grid dimensions, signal units, coordinate bounds, integral and the project download path. Request an optional window to inspect individual cells or sum a region."),
    "compare_project_inputs": (CompareProjectInputs, "Compare recorded inputs and archived solver identities of two optical projects or variants. 工程差异、方案对照、建模假设、光路顺序、器件来源。 Includes additions, removals, order, source conditions, unknown values and extension parameters. Supply reference_revision_id and an optional target revision_id; defaults to the question's model. Read-only. Use offset for further changes."),
    "recheck_optical_project": (RecheckProject, "Recompute the same frozen optical project inputs with the installed solver, compare recorded output fields and numerical values, and return the two solver receipts. 已存光路复核、复算原输入、求解器版本、复现数值。 Read-only. An absent historical solver receipt leaves the original implementation unknown."),
    "edit_project_details": (ProjectDetails, "Read or update the current project's name (title), experimental goal (goal) and notes (description). 工程名称、实验目标、工程备注。 Call with no arguments to read. For edits supply changes and expected with the same field names and their exact previous values. An empty goal or description is valid. A temporary task goal need not replace the project's goal."),
    "search_capabilities": (CapabilitySearch, "Find installed scientific models and workbench operations by purpose or quantity. For an attachment, original_inputs are whole-file roles. table_column_inputs are fields you can map after inspecting the table's actual columns, units and row range. For a saved quantity, pass its source reference. Use concise terms or omit the query to browse. Inspect the method for its parameters and scope. Returned tools are callable next turn. Use next_offset to continue."),
    "read_project_outputs": (ProjectOutputQuery, "Discover connectable physical quantities of an immutable optical model. Defaults to the bound model. Filter by quantity or location. Each port declares its physical meaning, unit and available representations in connects_as. Use revision_id and the returned port key unchanged in run_connected_study connections."),
    "find_study_usage": (StudyUsage, "Find downstream saved calculations from an original attachment, optical project or intermediate study, including indirect conclusions. 哪些计算使用了这份资料、工程或中间结果；影响分析、下游依赖、工况权重依据。 For a saved assessment open workbench_view.run_id (baseline) and comparison_id. For a model add compare_revision_id to compare consumed values and conditions before proposing replay_study. A link alone does not prove the conclusion changes."),
    "search_study_parameters": (StudySearch, "Compare and rank parameter combinations from one saved scientific run. 参数扫描、方案比较、多目标优化、联动位置、装调候选。 For a layout input read_artifact path=['search_parameters', input_name] lists adjustable relative paths; prepend input_name. Each axis has paths addressed by arrays of field names and zero-based indexes, samples as rows of simultaneous values or {start,stop,count} vectors. Cross independent axes. Measures use a published scalar key. Choose min, max or target (absolute linear distance to target in its baseline unit). Inclusive bounds apply to the measured value. Returns all feasible nondominated alternatives. For connected layouts explain registration; saves isolated candidate models and keeps the original."),
    "replay_study": (StudyReplay, "Inspect the provenance of a saved calculation and repeat its analysis chain with a different saved optical project revision or upstream study. Recompute dependent steps, reuse independent branches, compare outputs. 提取计算链、换工程版本、替换上游结果、重算比较。 Use run_id alone to see sources, or source_id and replacement_id to preview or execute. Supply renewed registration for changed experimental relationships."),
    "run_connected_study": (ConnectedStudy, "Link inputs of a registered calculation to original files, frozen models or prior results. Inspect its provider, version, ports and text_column_inputs with inspect_tool. For a model use read_project_outputs and pass {revision_id,port}. For a prior result use {run_id,port} from read_artifact. For a file use inspect_measurement_table, then bind the selected table, source SHA-256 and 1-based column. Supply the original unit for numeric ports. Omit units and calibration for text columns. Paired fields in one selection keep original rows together. For conditioned file inputs supply condition_values and condition_note from the source. For states follow the receiving port's registration_label. Document justified transfers across conditions. Supply each bound input only in connections."),
    "analyze_mode_overlap": (InspectModeOverlap, "Compare two Gaussian beams at one registered plane of the bound project. Evaluate transverse intensity also across wavelengths, and scalar coherent projection for the same wavelength and medium. Set save_result=true to retain the model and comparison for parameter searches or replay. For absorption-weighted results across a medium, discover the applicable volume study."),
    "begin_optical_study": (BeginStudy, "Start an independent design alternative. With no arguments use the question's model. revision_id selects an earlier model, case_head the current official model. For a saved scientific calculation or search candidate, read its layout_inputs with read_artifact and select study_input by run_id and input_key. A direct project binding keeps the complete source project, including when the solver consumed one arm or prefix. Other layout inputs copy the frozen optical model. An optional variant_name names a fork. Edits stay in the study until adopted. An empty case can also be studied."),
    "adopt_optical_snapshot": (AdoptSnapshot, "Adopt an exact study snapshot as a new official engineering revision. Use only when the user asks to apply that proposal to the case; requires its original official base and normal hardware locks."),
    "read_component_library": (LibraryQuery, "Browse reusable optical elements and assemblies, or read an exact revision. With include_builtin=true the catalog includes material_models (dispersion keys, supported wavelengths, polarization basis, source) and device prescriptions. Use a supported material key in an element's index_reference when evidence identifies that model."),
    "capture_component_model": (CaptureModel, "Save any combination of an ordered interval, its registered observation planes, and selected network components as one library model. Internal optical links, including power and field references between the selections, travel. External illumination stays in the source case. An optional previous_model_revision_id appends a version."),
    "place_component_model": (PlaceModel, "Insert an independent copy of an exact library revision. Ordered elements use the chosen path and insertion boundary. Registered planes, network components and all internal optical links are remapped to the copy. The receiving project supplies its illumination. Read the resulting project for instance identities."),
    "compare_measured_power": (MeasurePower, "Compare an archived power-meter table or an explicitly transcribed reading with one saved project's before-element, after-element, or internal observation plane. Use read_power_measurements for target identities. Read the original first. A simultaneous source-plane column can compare transmission even without source watts. Keep units, acquisition conditions, bounds and whether inputs used these data explicit. Never infer calibration, collection, independence or missing readings."),
    "read_power_measurements": (MeterHistory, "List addressable single-pass power measurement locations and stored comparisons for a saved case revision, or retrieve a measurement_id including its source rows and model."),
    "analyze_path_power": (PowerAnalysis, "Recalculate passive single-pass optical power for a case. Supply revision_id for a saved revision, {} for the latest, or layout for a proposed design. Report delivered, modeled bulk-absorbed, and unassigned power plus missing inputs at each budget wavelength. Never infer absorption, heating or laser output from unexplained path loss."),
    "study_measured_beam": (BeamStudy, "Fit measured widths from case tables or registered readings. To test spatial prediction on an existing series, reserve whole measured stations with holdout.positions_mm and map source.acquisition_column if provided. The fit uses training stations only. Held-out rows retain original units, errors and any extrapolation. To freeze predictions before new measurements, use source.kind=prediction; compare later measured rows with reference_fit and a coordinate registration. Known reused observations are excluded."),
    "read_beam_studies": (StudyHistory, "List case fits and comparisons, retrieve an exact stored result by study_id, or predict radii and diameters at positions_mm in a saved fit's original coordinate frame without using new observations."),
    "bind_measured_launch": (MeasuredLaunch, "Register one axis of a case beam caustic fit at an air source plane in a traceable independent design revision. Select the single-pass primary source, an existing incident path, or start a probe with entry_uid/exit_uid. Other axes keep their evidence. 实测焦散用作光源，拟合接入光路。"),
    "record_beam_reading": (BeamReading, "Record an actual transcribed measurement at a saved beam and section, with case-original attachment, row/region and experimental conditions. Read the source or use explicit user-supplied readings; never treat simulated values as observed."),
    "read_beam_readings": (ReadingQuery, "Read original units, source references and residuals for the selected optical version. Use all_revisions=true to locate existing case measurements from earlier models; paginate with before_id."),
    "compare_beam_predictions": (PredictionComparison, "Compare two saved models' detector readings at accessible stations. Apply documented width and axial position bounds when provided. With both bounds, separated_at_mm ranks stations by clearance between possible readings. largest_at_mm uses nominal positions only. Discover models with read_optical_project(list_versions=true)."),
    "compare_beam_reading": (CompareBeam, "Test saved models against an archived width in its instrument convention and registered frame. Include documented axial position_bound and width_bounds to test compatibility across the allowed detector locations. Establish beam, datum, axes and experimental conditions from evidence; only independent data can validate a model."),
    "create_inventory_item": (CreateInventoryItem, "Record any actual component (mirror, lens, crystal, window or other) from current user evidence and optional case attachments. Unknown/assumed fields remain unverified; unconfirmed stock has quantity 0. Preserves sources and audit. Creating inventory does not install or replace hardware."),
    "update_inventory_item": (UpdateInventoryItem, "Correct an existing inventory component from current user evidence. Only patch supplied fields; retain uncertainty and sources. Does not change installed hardware, case facts, or historical optical projects; explicit selection/replacement is a separate operation."),
    "read_inventory_item": (ReadInventoryItem, "Read a specific actual inventory item, its reviewed crystal sections, uncertainty and coating data. It does not select or replace hardware."),
    "find_replacement_candidates": (ReplacementCandidates, "Find stock for a specific mounted component identified by revision, target_path_uid and target_element_uid. Check recorded optical specification, loans, already occupied copies and wavelength evidence. Search and paginate by name or label."),
    "preview_component_replacement": (PreviewReplacement, "Recompute a stock installation on the selected main or incident path without saving. Choose an axial mounting datum when thickness changes. Returns face positions, gap adjustment, affected observation planes and a preview token."),
    "replace_component": (ReplaceComponentTool, "Install a stock part at a saved main or incident path element. Query compatible stock and preview its optical response and mounting datum. A preview_token pins the evaluated stock, installation and revision. A study snapshot remains independent. Requires the current user's literal instruction."),
    "replace_crystal": (ReplaceCrystalTool, "Install a user-selected structured crystal using the requested axial mounting datum and append the recalculated geometry. Requires source_quote from the current human request."),
    "read_optical_project": (ProjectLookup, "Read the model attached to this question by default, including its full deterministic analysis. Request a revision_id for a particular saved project or study snapshot, or case_head=true for the latest official case model."),
    "save_optical_project": (SaveProject, "Build or update the editable canvas model and recalculate it using its exact base revision. On an empty project a direct save creates its first main layout. A draft creates a study snapshot. The project can have an ordered beam train, a passive optical port network, or both. Preserve identities, wiring and untargeted settings across edits. A network-only model uses a single-pass system with the wavelength and an empty element list. Assemblies group ordered primitives of one physical piece; distinct installations may share a stock ID."),
    "import_design_project": (ImportDesignProject, "Open the computed geometry in a saved design as editable optics. Choose an assessed pump path, a cold-cavity row, or their shared-crystal layout. Returns an editable version with source references. An omitted baseline uses the current working model."),
    "scan_optical_system_2d": (ProjectScan2D, "Compute and save a two-parameter T/S stability map, linked to its matching engineering revision and visible in the workspace. Supports physical gaps, radii, focal power and indices. Finite grid, no output-power optimization."),
    "analyze_saved_design": (SavedAnalysis, "Precisely re-evaluate a saved specialized laser design: cavity/pump q traces, analytic waists and physical boundaries. Use this to investigate narrow pump waists instead of inferring a minimum from sampled artifact rows. Thermal focal length here is INSIDE the crystal; convert f_air=f_inside/n for the general system tool."),
    "read_tool_result": (ToolResultQuery, "Read a tool_call_id from the Agent execution trace (工具调用). For a saved calculation record (计算记录), use read_artifact with its content_id. For sampled tool output, supply the list path, zero-based offset and limit up to 24."),
    "list_inventory": (InventoryQuery, "Search actual owned inventory, coating records and available quantities; paginate using offset. Empty inventory does not prevent proposing purchase specifications."),
    "inspect_measurement_table": (InspectMeasurement, "Plot and inspect a case instrument table directly, including delimited vendor text and workbooks. Start with attachment_id, or provide explicit sheet, delimiter, decimal mark, 1-based rows and x/y columns (null x uses record number). x_lower and x_upper bound the inclusive region in actual x values, including repeated or descending sweeps. Returns the exact selected range's finite count, skipped records and first global extrema as [source record, x, y]. Plotted points may be reduced. Infer physical units and significance only from documented acquisition conditions."),
    "read_attachment": (AttachmentQuery, "Browse case originals by path or filename with query and next_before_id as before_id; {} lists recent originals. Read a specific attachment_id for paged text, PDFs, workbook cell values or an image. Distinguish same-named files by path and ID. Unknown formats remain archived."),
    "read_artifact": (ArtifactQuery, "Read a case artifact by content_id or list this case's records by content_type and before_id. In a scientific_study, read available_outputs for the ports actually published by this run, then use its id and exact port key for source-aware capability search. Read output, inputs or connections for their values and provenance. solver_receipt was captured during the original calculation. installed_solver_receipt is a live lookup in the installation serving this read. Read project_basis for all frozen project sources including those used by upstream results. Use offset and limit to page large objects or lists. Generated designs are proposals."),
    "read_history": (HistoryQuery, "Read earlier task conversation including replies to questions; optional before_id for pagination."),
    "search_knowledge": (KnowledgeQuery, "Search accessible indexed documents and similar cases, returning evidence and citation identifiers."),
    "search_literature": (LiteratureQuery, "Search OpenAlex/arXiv metadata and abstracts, not full texts. Return actual records; do not invent literature when a source fails."),
    "scan_optical_system": (ProjectScan, "Scan and save one element parameter; link to the matching engineering revision. Inspect stability and input/output beam size in both planes. Exploratory systems remain proposals; finite grid, no hidden optimization objective."),
    "match_components": (ComponentMatch, "Evaluate actual inventory against coating/ROC/aperture/quantity requirements; returns matched, uncertain and rejected candidates with reasons. Inspect_tool gives exact configuration."),
    "design_linear_laser": (LinearDesign, "Two-mirror end-pumped linear design. M1=input, M2=output; either mirror can be flat or concave. Computes supported quantities from documented inputs by default. Select exploratory for a conditional design search. Reads selected labels and bounded text excerpts and records their sources. Saves a detailed design; import_design_project creates an editable engineering model."),
    "inspect_tool": (ToolHelp, "Read supported configuration and model boundaries of a named tool before using unfamiliar fields."),
    "finish": (Finish, "Deliver results, evidence, assumptions, actionable questions and next steps. This ends the run, preserving all actual tool outputs."),
}


STUDIES, STUDY_ISSUES = discover_studies(reserved=REGISTRY)
REGISTRY.update({name: (study.inputs, study.scope) for name, (study, _) in STUDIES.items()})
_LABELS = json.loads(Path(__file__).with_name("action_labels.json").read_text(encoding="utf8"))
DIRECTORY = CapabilityDirectory(REGISTRY, STUDIES, _LABELS)
STARTING_TOOLS = ("search_capabilities", "inspect_tool", "read_optical_project", "read_artifact",
                  "read_attachment", "read_tool_result", "finish")


def capability_title(name):
    return DIRECTORY.entries.get(name, {}).get("title", _LABELS.get(name, name))


@lru_cache(maxsize=128)
def _schema(name):
    model, description = REGISTRY[name]
    schema = model.model_json_schema()
    if name in STUDIES and STUDIES[name][0].column_series:
        description += " For archived table columns use run_connected_study to preserve units, row identity and provenance."
    if name in STUDIES and any(port.shape == "state" for port in STUDIES[name][0].input_ports.values()):
        description += " To cite a saved model or upstream field, bind its state port with run_connected_study; submitting inline values records those values alone."
    if name in STUDIES and STUDIES[name][0].uses_original_files:
        description += " Pass the case attachment_id and optional expected sha256 for verified original files."
    schema["properties"]["purpose"] = {"type": "string", "description": "Short user-visible reason for this operation"}
    return {"name": name, "description": description, "parameters": schema}


def schemas(names=None):
    selected = REGISTRY if names is None else names
    return [deepcopy(_schema(name)) for name in dict.fromkeys(selected) if name != "replace_crystal"]


def _matrix_receipt(rows, tool_call_id, path):
    """Describe a dense numerical result while keeping direct access to each original row."""
    if tool_call_id is None or len(rows) < 8 or not isinstance(rows[0], list):
        return None
    width = len(rows[0])
    if width < 8 or len(rows) * width <= 576 or not all(isinstance(row, list) and len(row) == width for row in rows):
        return None
    low = high = None
    missing = 0
    for y, row in enumerate(rows):
        for x, value in enumerate(row):
            if value is None:
                missing += 1
                continue
            if type(value) not in (float, int):
                return None
            if low is None or value < low["value"]:
                low = {"value": value, "index": [y, x]}
            if high is None or value > high["value"]:
                high = {"value": value, "index": [y, x]}
    return {
        "shape": [len(rows), width], "minimum": low, "maximum": high, "null_cells": missing,
        "read_row": {"tool_call_id": tool_call_id, "path": [*path, 0], "offset": 0},
        "note": "Extrema cover every numerical cell. Indices are zero-based [row, column]. "
                "Use read_tool_result with the desired row in path and column offset for the exact values.",
    }


def compact(value, max_items=24, depth=0, *, tool_call_id=None, path=()):
    """Explicit sampling for model context only; persisted tool output stays intact."""
    if isinstance(value, dict):
        # Instruments may label columns by one-based integers in memory. JSON
        # stringifies these keys on disk; the next model turn needs the same view.
        return {str(key): compact(item, max_items, depth + 1, tool_call_id=tool_call_id, path=(*path,str(key))) for key, item in value.items()
                if not str(key).startswith("_")}
    if isinstance(value, list):
        matrix = _matrix_receipt(value, tool_call_id, path)
        if matrix is not None:
            return matrix
        if len(value) > max_items:
            selected = set(int(x) for x in np.linspace(0, len(value) - 1, max_items))
            # Preserve narrow waists/extrema that even sampling can entirely miss.
            critical, extremes = set(), set()
            for key in ("w_mm", "radius_mm", "m", "w_um", "watts", "fraction"):
                if not all(isinstance(row, dict) and isinstance(row.get(key), (int, float)) for row in value):
                    continue
                ys = [row[key] for row in value]
                extremes.update((min(range(len(ys)), key=ys.__getitem__), max(range(len(ys)), key=ys.__getitem__)))
                critical.update(i for i in range(1, len(ys) - 1) if (ys[i] < ys[i - 1] and ys[i] <= ys[i + 1]) or (ys[i] > ys[i - 1] and ys[i] >= ys[i + 1]))
            critical.update(extremes)
            selected.update(extremes)
            selected.update(sorted(critical-extremes)[:96-len(extremes)])
            indexes = sorted(selected)
            return {"sampled_items": [compact(value[i], max_items, depth + 1, tool_call_id=tool_call_id, path=(*path,i)) for i in indexes],
                    **({"read_original":{"tool_call_id":tool_call_id,"path":list(path),"offset":0}} if tool_call_id is not None else {}),
                    "total_items": len(value), "sampled_indices": indexes,
                    "all_detected_extrema_retained": len(critical) <= 96,
                    "note": "This is a sample, with original list indices. For contiguous original entries, call read_tool_result with the saved call ID, list path and offset. Use computed summaries or analytic results for numerical conclusions."}
        return [compact(item, max_items, depth + 1, tool_call_id=tool_call_id, path=(*path,i)) for i,item in enumerate(value)]
    if isinstance(value, str) and len(value) > 18000:
        return value[:18000] + "\n[Context text truncated; read the source in pages.]"
    return value


def _result_at_path(output, path, *, tool_envelope=True):
    keys = path if isinstance(path,list) else path.split(".") if path else []
    # Dotted paths copied from the message may begin at its outer envelope.
    # A real key named "result" takes precedence. Array paths are always exact.
    if tool_envelope and isinstance(path,str) and keys[:1]==["result"] and not (isinstance(output,dict) and "result" in output):
        keys = keys[1:]
    for index,key in enumerate(keys):
        try:
            if isinstance(output,list):
                output = output[int(key)]
            elif isinstance(output,dict):
                output = output[str(key)]
            else:
                raise TypeError()
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            node = json.dumps(keys[:index], ensure_ascii=False)
            if isinstance(output,dict):
                hint = "可用字段为 " + "、".join(str(k) for k in list(output)[:16])
            elif isinstance(output,list):
                hint = f"列表长度为 {len(output)}，首项下标为 0"
            else:
                hint = "此处已是一个值"
            raise ValueError(f"记录在 {node} 找不到 {key!r}。{hint}。") from exc
    return output


def _record_page(value, offset, limit):
    if offset is None:
        return value
    end = offset + limit
    if isinstance(value, dict):
        items = {key:value[key] for key in sorted(value)[offset:end]}
    elif isinstance(value, list):
        items = value[offset:end]
    else:
        raise ValueError("分页需要选择列表或含有多个字段的对象。")
    return {"items":items, "offset":offset, "total":len(value), "next_offset":end if end < len(value) else None}


def _workbench_principal(db, task):
    """Use server-stored identity, never a model-supplied role."""
    if task.user_id is not None:
        user = db.get(User, task.user_id)
        if user is not None:
            if not user.is_active:
                raise ValueError("任务用户已停用。")
            return Principal(actor=user.email, role=user.role or "user", user_id=user.id)
        if get_settings().require_auth:
            raise ValueError("任务用户不存在。")
    request = db.query(AgentToolCall).filter_by(task_id=task.id, tool_name="workbench_request").first()
    stored = (request.input_json or {}).get("principal") if request else None
    if stored:
        if stored.get("user_id") != task.user_id:
            raise ValueError("任务身份与请求身份不一致，未执行操作。")
        return Principal(actor=stored.get("actor") or "anonymous", role=stored.get("role") or "user", user_id=task.user_id)
    if task.user_id is not None:
        raise ValueError("此任务缺少有效用户身份，请重新提交需求。")
    if get_settings().require_auth:
        raise ValueError("此历史任务缺少用户身份，请重新提交需求。")
    return Principal(actor="anonymous", role="user")


def _inventory_write_input(args, *, case, task, db, creating):
    if not args.source_quote.strip() or args.source_quote.strip() not in task.goal:
        raise ValueError("库存写入依据必须来自本轮用户原话，不能引用旧消息或模型建议。")
    ids = list(dict.fromkeys(args.source_attachment_ids))
    attachments = db.query(Attachment).filter(Attachment.id.in_(ids)).all() if ids else []
    if len(attachments) != len(ids) or any(a.case_id != case.id for a in attachments):
        raise ValueError("来源附件不存在或不属于当前案例，未修改库存。")
    values = args.item.model_dump() if creating else args.patch.supplied_changes()
    if not creating and "crystal_spec" not in values and set(args.unverified_fields) & {"material", "thickness_mm", "dimensions", "doping_pct"}:
        existing = db.get(InventoryItem, args.inventory_id)
        if existing and existing.crystal_spec:
            values["crystal_spec"] = deepcopy(existing.crystal_spec)
    unverified_values = {field:values.get(field) for field in args.unverified_fields}
    optional = set(ManualItemCreate.model_fields)-{"name", "category"}
    for field in args.unverified_fields:
        if field not in optional:
            raise ValueError(f"待核验字段 {field} 无效；仅支持元件可选字段。材料结构或镀膜不确定时标记 crystal_spec 或 coatings。")
        values[field] = [] if field == "coatings" else False if field == "roc_is_flat" else 0 if field == "quantity" else None
        if field == "roc_mm":
            values["roc_is_flat"] = False
        spec = values.get("crystal_spec")
        if spec and field == "material":
            independent_label = bool(spec.get("material_label"))
            spec["material_label"] = None
            if not independent_label:
                # Old inventory flattened the material field from the section
                # identities. A withdrawal there applies to that derived reading.
                for segment in spec["segments"]:
                    segment["material"] = None
        if spec and field in {"thickness_mm", "dimensions"}:
            spec["overall_length_mm"] = None
            values["thickness_mm"] = None
            if field == "dimensions":
                spec.update(width_mm=None, height_mm=None)
        if field == "doping_pct" and values.get("crystal_spec"):
            for segment in values["crystal_spec"]["segments"]:
                if segment["role"] == "gain":
                    segment.update(doping_pct=None, uncertain=True)
        if field == "crystal_spec":
            for derived in ("material", "doping_pct", "thickness_mm", "dimensions"):
                values[derived] = None
    changes_quantity = creating or "quantity" in values
    if changes_quantity and not args.stock_confirmed:
        values["quantity"] = 0
    # Unverified raw readings survive in notes/audit, not structured physical data.
    provenance = {"case_id":case.id, "task_id":task.id, "source_quote":args.source_quote,
                  "source_attachment_ids":ids, "unverified_fields":list(args.unverified_fields),
                  "unverified_values":unverified_values,
                  "uncertainty_notes":list(args.uncertainty_notes),
                  "stock_confirmed":args.stock_confirmed if changes_quantity else True}
    return (ManualItemCreate.model_validate(values) if creating else ManualItemPatch.model_validate(values)), provenance


async def execute(name, args, *, db, case, task, provider, session_id, attachment_ids, working=None):
    if name == "edit_project_details":
        principal = _workbench_principal(db, task)
        if args.changes:
            db.refresh(task)
            if task.status == "cancelled":
                raise ValueError("任务已停止，请重新提交修改。")
            assert_case_edit(db, case, principal)
        else:
            assert_case_view(db, case, principal)
        return {"status": "completed", "details": project_details(db, case, args)}
    if name == "search_study_parameters":
        return await asyncio.to_thread(run_search, db.get_bind(), case.id, args, STUDIES, task.id)
    if name == "reassess_study_search":
        return await asyncio.to_thread(reassess_search, db.get_bind(), case.id, args, task_id=task.id)
    if name == "inspect_study_tolerance":
        return await asyncio.to_thread(inspect_tolerance, db.get_bind(), case.id, args)
    if name == "inspect_adjustment_policy":
        return await asyncio.to_thread(inspect_adjustment_policy, db.get_bind(), case.id, args)
    if name == "save_adjustment_evaluation":
        assert_case_edit(db, case, _workbench_principal(db, task))
        return await asyncio.to_thread(save_policy_comparison, db.get_bind(), case.id, args, task.id)
    if name == "refine_study_tolerance":
        return await asyncio.to_thread(refine_tolerance, db.get_bind(), case.id, args, STUDIES, task.id)
    if name == "find_study_usage":
        return await asyncio.to_thread(study_usage, db.get_bind(), case.id, args)
    if name == "inspect_native_grid":
        return await asyncio.to_thread(inspect_native_grid, db.get_bind(), case.id, args, STUDIES)
    if name == "replay_study":
        return await asyncio.to_thread(replay_study, db.get_bind(), case.id, args, STUDIES, task.id)
    if name == "search_capabilities":
        return DIRECTORY.search_case(db, case.id, args)
    if name == "read_project_outputs":
        if args.revision_id is None and working and not working.get("revision_id"):
            return {"revision_id":None,"outputs":{}}
        return discover_project_outputs(db,case.id,args.revision_id or (working or {}).get("revision_id"),args)
    if name == "compare_project_inputs":
        return {"status":"completed", **compare_project_inputs(db, case.id, args, (working or {}).get("revision_id"))}
    if name == "recheck_optical_project":
        source = read_recheck_source(db, case.id, args.revision_id or (working or {}).get("revision_id"))
        return await asyncio.to_thread(recheck_project, source)
    if name == "run_connected_study":
        entry = STUDIES.get(args.study)
        if entry is None or (entry[0].version, entry[1]) != (args.version, args.provider):
            raise ValueError("请用 inspect_tool 核对已安装的求解器名称、版本和来源。")
        return await asyncio.to_thread(run_study, db.get_bind(), case.id, *entry, args.inputs, task.id,
                                      connections=args.connections, reuse_inputs_from=args.reuse_inputs_from)
    if name in STUDIES:
        study, provider_name = STUDIES[name]
        return await asyncio.to_thread(run_study, db.get_bind(), case.id, study, provider_name, args, task.id)
    if name == "begin_optical_study":
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("任务已停止。")
        if args.study_input:
            assert_case_edit(db, case, _workbench_principal(db, task))
            return begin_study_input(db, case.id, args.study_input.run_id, args.study_input.input_key, args.variant_name)
        return begin_study(db, case.id, None if args.case_head else working, args.revision_id, args.variant_name)
    if name == "adopt_optical_snapshot":
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("任务已停止。")
        if not args.source_quote.strip() or args.source_quote.strip() not in task.goal:
            raise ValueError("采用试算方案的依据必须来自本轮用户的原话。")
        assert_case_edit(db, case, _workbench_principal(db, task))
        return adopt_snapshot(db, case.id, args.snapshot_id, args.base_revision_id)
    if name in {"read_component_library", "capture_component_model", "place_component_model"}:
        principal = _workbench_principal(db, task)
        if name == "read_component_library":
            assert_case_view(db, case, principal)
            return {"status": "completed", **library(db, principal, args)}
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("任务已停止，请重新提交模型操作。")
        assert_case_edit(db, case, principal)
        if name == "capture_component_model":
            return {"status": "completed", **capture_model(db, principal, case.id, args)}
        if working and args.project_revision_id != working["revision_id"]:
            raise ValueError("请选择本轮使用的光路作为元件插入位置。")
        return place_model(db, principal, case.id, args, working=working)
    if name in {"create_inventory_item", "update_inventory_item"}:
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("任务已停止，没有修改库存。")
        creating = name == "create_inventory_item"
        principal = _workbench_principal(db, task)
        payload, provenance = _inventory_write_input(args, case=case, task=task, db=db, creating=creating)
        item = create_item(db, payload, principal, provenance=provenance) if creating else update_item(db, args.inventory_id, payload, principal, provenance=provenance)
        return {"status":"completed", "inventory_id":item.id,
                "item":next(row for row in inventory_snapshot(db) if row["id"] == item.id),
                "provenance":provenance, "installed_hardware_changed":False,
                "message":"已保存元件记录与来源。当前工程和历史版本未改变；需要采用此元件时再明确选择或替换。"}
    if name == "read_inventory_item":
        item = next((row for row in inventory_snapshot(db) if row["id"] == args.inventory_id), None)
        if item is None:
            raise ValueError("库存元件不存在，请先调用list_inventory查看实际元件。")
        return {"status":"completed", "item":item}
    if name == "find_replacement_candidates":
        project = read_project(db, case.id, args.revision_id)["project"]
        return {"revision_id": args.revision_id, **stock_candidates(db, project, args.target_path_uid, args.target_element_uid,
                args.target_role, args.search, args.skip, args.limit)}
    if name == "preview_component_replacement":
        result = replace_component(db, case.id, args, active_task_id=task.id, working=working, preview=True)
        return {**{key: result[key] for key in ("replacement", "scope_warnings", "preview_token", "summary")},
                "optical_results": replacement_optical_results(result["analysis"])}
    if name in {"replace_component", "replace_crystal"}:
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("任务已停止，没有替换元件。")
        if not args.source_quote.strip() or args.source_quote.strip() not in task.goal:
            raise ValueError("替换依据必须来自本轮用户原话，不能引用旧消息或模型自己的建议。")
        if working and args.base_revision_id != working["revision_id"]:
            raise ValueError("请选择本轮使用的光路作为替换起点。")
        values = args.model_dump(exclude={"source_quote"})
        if name == "replace_crystal":
            values["target_role"] = "crystal"
        return replace_component(db, case.id, ReplaceComponent(**values), active_task_id=task.id, working=working)
    if name == "read_optical_project":
        if args.list_versions:
            return {"status": "completed", **model_version_index(db, case.id, args.before_id, args.variant_name)}
        selected_id = args.revision_id
        if selected_id is None and not args.case_head and working is not None:
            selected_id = working["revision_id"]
            if selected_id is None:
                return {"status":"completed", "project_revision":None, "message":"提问时尚未建立光路。"}
        return {"status": "completed", "project_revision": read_project(db, case.id, selected_id)}
    if name == "save_optical_project":
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("Task stopped before project save")
        allow = bool(args.unlock_quote.strip()) and args.unlock_quote.strip() in task.goal
        if args.unlock_quote and not allow:
            raise ValueError("Unlock quote must be present in the current user request")
        return save_working_project(db, case.id, args, working, allow_unlock=allow)
    if name == "import_design_project":
        artifact = db.get(GeneratedContent, args.content_id)
        if not artifact or artifact.case_id != case.id:
            raise ValueError("Design is outside this case")
        design = artifact.content.get("engineering_design", {})
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("Task stopped before project import")
        source_id = args.base_revision_id
        if "base_revision_id" not in args.model_fields_set and working:
            source_id = working["revision_id"]
        if working and working["revision_id"] != source_id:
            raise ValueError("请使用本轮正在分析的光路版本作为导入基线。")
        previous = read_project(db, case.id, source_id) if source_id else None
        if not working and "base_revision_id" not in args.model_fields_set:
            previous = read_project(db, case.id)
            source_id = previous["revision_id"] if previous else None
        if args.target != "cold_cavity" or design.get("assumption_policy") == "evidence_only" or previous or (working or {}).get("study_only"):
            base = previous.get("base_revision_id") if previous and previous.get("model_kind") == "draft" else source_id
            return independent_design(db, case.id, design, args.content_id, args.candidate, source_id, base, target=args.target)
        project = saved_geometry(design, args.content_id, args.candidate, target=args.target)
        return save_working_project(db, case.id, SaveProject(base_revision_id=source_id, project=project,
                                    change_note=f"从设计 #{args.content_id} 候选 {args.candidate} 导入"), working)
    if name == "scan_optical_system_2d":
        return await run_scan(db, case.id, args, task_id=task.id, working=working)
    if name == "inspect_measurement_table":
        return await asyncio.to_thread(agent_inspect_measurement, db.get_bind(), case.id, args)
    if name == "study_measured_beam":
        return await asyncio.to_thread(run_agent_beam_study, db.get_bind(), case.id, task.id, args)
    if name == "bind_measured_launch":
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("任务已停止。")
        assert_case_edit(db, case, _workbench_principal(db, task))
        if working and args.revision_id != working["revision_id"]:
            raise ValueError("请先选中目标光路版本，再接入实测光束。")
        return bind_measured_launch(db, case.id, args)
    if name == "compare_measured_power":
        return await asyncio.to_thread(agent_measure, db.get_bind(), case.id, args, task.id)
    if name == "read_power_measurements":
        result = meter_history(db, case.id, args.revision_id, args.measurement_id)
        return {"status": result.get("status", "completed"), "result": result}
    if name == "read_beam_studies":
        found = beam_study_history(db, case.id, args.study_id, args.positions_mm)
        return {"status": found["status"] if args.study_id else "completed", "result": found}
    if name == "analyze_saved_design":
        artifact = db.get(GeneratedContent, args.content_id)
        if not artifact or artifact.case_id != case.id:
            raise ValueError("Design artifact does not belong to this case")
        design = artifact.content.get("engineering_design", {})
        candidates = design.get("candidates", [])
        if not 1 <= args.candidate <= len(candidates):
            raise ValueError("Saved design has no such candidate")
        result = await asyncio.to_thread(analyze_design, design, candidates[args.candidate - 1], scan_samples=150,
                                         **args.model_dump(exclude={"content_id", "candidate"}))
        return {"status": "completed", "content_id": artifact.id, "candidate": args.candidate, "analysis": result}
    if name == "read_tool_result":
        call = db.query(AgentToolCall).join(AgentTask).filter(AgentToolCall.id == args.tool_call_id, AgentTask.case_id == case.id).first()
        if not call or call.tool_name == "workbench_request":
            raise ValueError("当前案例找不到这个工具调用编号。执行记录中的工具调用可用 tool_call_id 读取；分析记录中的已存计算请用 read_artifact 和 content_id 读取。")
        output = _result_at_path(call.output_json, args.path)
        output = _record_page(output, args.offset, args.limit)
        original_status = call.output_json.get("status") if isinstance(call.output_json,dict) else None
        states = {call.status,original_status}
        status = "needs_input" if states & {"failed","needs_input","unsupported"} else "partial" if "partial" in states else "completed"
        return {"status": status, "source_tool_call_id": call.id, "source_status": call.status, "name": call.tool_name,
                "original_input": call.input_json, "output": output}
    if name == "analyze_mode_overlap":
        layout, record = overlap_input(db, case.id, args, working)
        if getattr(args, "save_result", False):
            inputs = args.model_dump(include={"plane_uid", "first_beam_uid", "second_beam_uid"}, mode="json")
            links = {}
            if record is None:
                inputs["layout"] = layout.model_dump(mode="json")
            else:
                links["layout"] = {"revision_id": record["revision_id"], "port": "v1/layout",
                                   "registration": "采用工程中登记的同一观察面和两束光的共向 T/S 坐标。"}
            return await asyncio.to_thread(run_study, db.get_bind(), case.id, *STUDIES[SECTION_MODE_STUDY.name],
                                           inputs, task.id, connections=links)
        return await asyncio.to_thread(evaluate_overlap, layout, record, args)
    if name == "analyze_path_power":
        layout = args.layout
        revision_id = None
        if layout is None:
            ref = args.revision_id or (working or {}).get("revision_id")
            revision = read_project(db, case.id, ref) if ref or working is None else None
            if not revision:
                return {"status":"needs_input", "message":"请先保存光路，或提供待核算的光路草稿。"}
            layout = OpticalLayout.model_validate({key:value for key,value in revision["project"].items() if key in OpticalLayout.model_fields})
            revision_id = revision["revision_id"]
        located = locate_planes(layout, host_positions(layout.system))
        issues = [{"uid":p.uid, "name":p.name, "message":located[p.uid][1]} for p in layout.observation_planes if located[p.uid][1]]
        budgets = power_for_layout(layout, located)
        if not budgets:
            return {"status":"needs_input","message":"请为入射支路指定要检查的功率收支及适用波长。"}
        status = "completed" if not issues and all(b["status"]=="completed" for b in budgets) else "partial" if any(b.get("rows") for b in budgets) else "needs_input"
        return {"status":status,"revision_id":revision_id,"power_budgets":budgets,**({"unresolved_planes":issues} if issues else {})}
    if name == "record_beam_reading":
        return {"status": "completed", **register_reading(db, case.id, args, "agent")}
    if name == "check_prediction_reading":
        return await asyncio.to_thread(agent_check_prediction, db.get_bind(), case.id, args, task.id)
    if name == "read_beam_readings":
        if args.all_revisions:
            if args.revision_id:
                read_project(db, case.id, args.revision_id)
            return {"status":"completed", **observation_index(db, case.id, args.before_id)}
        ref = args.revision_id or (None if args.case_head else (working or {}).get("revision_id"))
        revision = read_project(db, case.id, ref) if ref or working is None or args.case_head else None
        if not revision:
            return {"status": "needs_input", "message": "请先保存光学工程。"}
        return {"status": "completed", "readings": readings_for_revision(db, case.id, revision["revision_id"])}
    if name in {"compare_beam_reading", "compare_beam_predictions"}:
        ref = args.revision_id or (working or {}).get("revision_id")
        if ref is None and working is None:
            current = read_project(db, case.id)
            ref = current["revision_id"] if current else None
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("任务已停止。")
        compare = agent_compare_predictions if name == "compare_beam_predictions" else agent_compare_reading
        return await asyncio.to_thread(compare, db.get_bind(), case.id, args, ref, task.id)
    if name == "scan_optical_system":
        return await run_scan(db, case.id, args, task_id=task.id, working=working)
    if name == "list_inventory":
        items = inventory_snapshot(db)
        filtered = [item for item in items if (not args.category or item["category"] == args.category)
                    and (not args.query or args.query.lower() in f"{item['name']} {item['raw_spec']}".lower())]
        return {"status": "completed", "total": len(filtered), "offset": args.offset,
                "items": filtered[args.offset:args.offset + args.limit]}
    if name == "match_components":
        return evaluate_candidates(db, args.requirement)
    if name == "search_knowledge":
        return search_payload(db, args.query, case.id, args.top_k, task.id)
    if name == "search_literature":
        return await asyncio.to_thread(run_literature_search, args.model_dump(exclude_none=True))
    if name == "read_history":
        query = db.query(AgentChatMessage).filter_by(session_id=session_id)
        if args.before_id:
            query = query.filter(AgentChatMessage.id < args.before_id)
        rows = query.order_by(AgentChatMessage.id.desc()).limit(args.limit + 1).all()
        return {"messages": [{"id": row.id, "role": row.role, "content": row.content, "metadata": row.metadata_json}
                             for row in reversed(rows[:args.limit])], "has_older": len(rows) > args.limit}
    if name == "read_artifact":
        if args.content_id is None:
            query = db.query(GeneratedContent).filter_by(case_id=case.id)
            if args.content_type:
                query = query.filter_by(content_type=args.content_type)
            if args.before_id:
                query = query.filter(GeneratedContent.id < args.before_id)
            rows = query.with_entities(GeneratedContent.id, GeneratedContent.content_type,
                                       GeneratedContent.content["summary"].as_string()).order_by(GeneratedContent.id.desc()).limit(26).all()
            return {"items": [{"id": i, "type": kind, "summary": (summary or "")[:500]} for i, kind, summary in rows[:25]],
                    "next_before_id": rows[24].id if len(rows) > 25 else None}
        item = db.get(GeneratedContent, args.content_id)
        if not item or item.case_id != case.id:
            raise ValueError("Artifact does not belong to this case")
        content = item.content
        keys = args.path if isinstance(args.path, list) else args.path.split(".")
        receipt_keys = ("solver_receipt", "installed_solver_receipt")
        receipt_path = keys[0] if keys and keys[0] in receipt_keys and item.content_type == "scientific_study" else None
        if item.content_type == "scientific_study" and (not args.path or receipt_path):
            saved_solver = content.get("solver_receipt")
            installed_solver = installed_study_receipt(content.get("definition"), saved_solver, STUDIES)
            content = {**content, "solver_receipt":saved_solver, "installed_solver_receipt":installed_solver}
        if item.content_type == "scientific_study" and (not args.path or keys[:1] == ["project_basis"]):
            content = {**content, "project_basis":study_project_basis(db, case.id, item.id, content.get("connections"))}
        if item.content_type == "scientific_study" and (not args.path or keys[:1] == ["layout_inputs"]):
            content = {**content, "layout_inputs": study_layout_inputs(item.id, content)}
        if item.content_type == "scientific_study" and keys[:1] == ["search_parameters"]:
            content = {"search_parameters":layout_search_parameters(db, case.id, content)}
        if item.content_type == "study_search" and keys[:1] == ["assessment_options"]:
            baseline = db.query(GeneratedContent).filter_by(case_id=case.id, id=content["baseline_id"],
                                                           content_type="scientific_study").one_or_none()
            if baseline is None:
                raise ValueError("此工程缺少原始基线计算，无法核对可评价的指标。")
            content = {"assessment_options":{**content["measures"], **search_measures(baseline.content)}}
        value = None if receipt_path and content[receipt_path] is None else _result_at_path(content, args.path, tool_envelope=False)
        value = _record_page(value, args.offset, args.limit)
        notes = []
        if item.content_type == "study_search" and not item.content.get("ranking_mode"):
            if not any(m.get("direction") for m in item.content["recipe"]["measures"]):
                notes.append("此历史比较只设了限值。旧记录的 preferred 和 rank=1 均表示通过筛选，未评定候选间的优劣。")
        if "engineering_design" in item.content:
            notes = ["Legacy thermal f is a physical focal length INSIDE crystal; general optical-system air-equivalent focal length is f/n. Compare only after conversion.",
                     "Saved pump.points are a coarse trace; read trace.waists from analyze_saved_design for exact waist locations. Absence of a point in compacted output does not prove a displaced focus."]
        result = {"content_id":item.id, "path":args.path, "content":value, "interpretation_notes":notes}
        if item.content_type == "scientific_study":
            definition = item.content.get("definition") or {}
            if not args.path or receipt_path or keys[:1] == ["definition"]:
                result["receipt_sources"] = {
                    "definition":"方法摘要中的名称、提供者和标称版本来自原计算保存的方法定义。",
                    "solver_receipt":"原计算执行时采集，随结果保存。缺失表示当时的运行环境未知。",
                    "installed_solver_receipt":"本次读取时现场查询本机安装，仅供选择复算环境及对照。method_only 只查到方法本身，历史元件依赖未知。",
                }
            if definition.get("native_grid") and item.content.get("output",{}).get("native_grid_sha256"):
                result["native_grid_access"] = native_grid_access(case.id, item.id)
            if definition.get("provider"):
                result["method"] = {"title":definition.get("title"), "version":definition.get("version"),
                                    "provider_display_name":provider_display_name(definition["provider"])}
                if not args.path and (saved_solver is not None or installed_solver is not None):
                    result["method"].update(solver_receipt=saved_solver, installed_solver_receipt=installed_solver)
        return result
    if name == "read_attachment":
        if args.attachment_id is None:
            return attachment_page(db, case.id, query=args.query, before_id=args.before_id, page_size=args.page_size)
        item = db.get(Attachment, args.attachment_id)
        if not item or item.case_id != case.id:
            raise ValueError("Attachment does not belong to this case")
        return await asyncio.to_thread(read_file, item, args.offset, args.limit)
    if name == "inspect_tool":
        if args.name not in REGISTRY:
            raise ValueError("Unknown tool; choose a name from the available registry")
        help_text = REGISTRY[args.name][1]
        if args.name == "compute_coating":
            help_text += ("\nconfig: mode ('auto'|'exact_stack'|'vendor_curve'|'archetype'), query_wavelengths_nm, "
                          "aoi_deg (default 0), polarization ('s'|'p'|'avg'). Exact stack requires stack.incident "
                          "and stack.substrate (catalog name, numeric n or {n,k}), and stack.layers in incident order. "
                          "Each layer has thickness_nm and either material or fixed n with optional k >= 0. An explicit empty layers list denotes a bare interface. "
                          "vendor_curve={wl_nm:[...],R:[fractions 0..1],T:[fractions 0..1],source:'document id'}. "
                          "nominal={function:'HR',design_wavelength_nm:1064,target_R:0.995} evaluates a representative archetype. "
                          "Measured curves and catalog dispersion models are used within their stated ranges.")
        if args.name == "match_components":
            help_text += "\nrequirement: category, quantity, roc_mm ('flat' or signed mm), roc_tol_pct, min_diameter_mm, max_results; surfaces: [{surface:'S1', wavelength_nm:1064, function:'HR', min_R_pct:99.8}]; phase_match: {crystal:'lbo',lambda1_nm:1064,pm_type:'I'}."
        result = {"name": args.name, "description": help_text, "parameters": REGISTRY[args.name][0].model_json_schema()}
        if args.name in STUDIES:
            study, provider_name = STUDIES[args.name]
            result["study"] = study.presentation(provider_name)
            if study.element_law:
                device = device_definition(study, provider_name)
                result["study"]["element_law"].update(prescription=device["prescription"], length_mm=device["length_mm"])
        return result
    if name == "design_linear_laser":
        model_case = case_for_planning(db, case, working)
        previous, projection, model_revision = specialized_projection(db, model_case, revision_id=(working or {}).get("revision_id"))
        if projection:
            projection["assumption_policy"] = args.assumption_policy
            return {"status":"unsupported","engineering_design":projection,
                    "next_tools":["read_optical_project","analyze_observation_planes","analyze_optical_system","scan_optical_system","scan_optical_system_2d"]}
        inventory = inventory_snapshot(db, model_case)
        # Extract from actual human input, not the model-generated tool arguments.
        context = {"title": case.title, "description": case.description or "", "message": task.goal,
                   "existing_facts": previous, "inventory": inventory[:150]}
        images, readings = await read_design_files(db, case.id, attachment_ids)
        context["selected_originals"] = readings
        extraction = await provider.extract_design_facts(context, images)
        facts, changes, notes = merge_facts(context["existing_facts"], extraction,
                                            {key: context[key] for key in ("title", "description", "message")},
                                            [image["id"] for image in images], readings)
        design = await asyncio.to_thread(solve_design, facts, inventory, assumption_policy=args.assumption_policy)
        record_projection(design, previous, model_revision)
        db.refresh(task)
        if task.status == "cancelled":
            raise ValueError("Task stopped; no new design or case facts were saved")
        design.update({"changes": changes, "intake_notes": notes,
                       "intake_files": [{key: value for key, value in page.items() if key != "text"} for page in readings]})
        content = {"engineering_design": design, "summary": design.get("summary", ""), "_usage": extraction.get("_usage", {})}
        artifact = GeneratedContent(case_id=case.id, content_type="plan", content=content, prompt_version="workbench-linear-tool-v1")
        apply_usage_to_generated(artifact, content)
        db.add(artifact)
        db.flush()
        if model_case is case:
            db.refresh(case, with_for_update=True)
            head = read_project(db, case.id) if working else None
            if not working or working["revision_id"] == (head["revision_id"] if head else None):
                case.parameters = {**(case.parameters or {}), "design_facts": accepted_updates(
                    (case.parameters or {}).get("design_facts", {}), previous, facts)}
        return {"status": design["status"], "content_id": artifact.id, "engineering_design": design}
    raise ValueError("Unknown executable tool")

"""Task workbench: submit, inspect, stop and continue through ordinary replies."""
import json
from hashlib import sha256
from threading import Lock
from tempfile import SpooledTemporaryFile
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Discriminator, Field, RootModel, Tag
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from ..auth.acl import assert_case_edit, assert_case_view
from ..auth.security import Principal, get_current_principal
from ..database import get_db
from ..run_ownership import RunLease, reconcile
from ..models import AgentChatMessage, AgentChatSession, AgentTask, AgentToolCall, Attachment, GeneratedContent
from ..study_api import ResultReference
from ..workbench.component_models import CaptureModel, LibraryConflict, LibraryQuery, PlacementPreview, capture_model, library, place_model
from ..workbench.study_inputs import WorkingDraft, SaveVariant, ChooseStudyInput, basis, capture_snapshot, adopt_snapshot, save_variant
from ..workbench import service
from ..workbench.context import ContextRefs, InputRefs, validate_context_refs, workspace_view
from ..workbench.tools import DIRECTORY, STUDIES, STUDY_ISSUES, capability_title
from ..workbench.discovery import BrowserSearch
from ..workbench.studies import StudyFailure, StudyInvocation, UnpublishedOutput, export_native_grid, frozen_quantity, installed_study_receipt, preview_connected_study, run_study, study_history, view_native_grid
from ..workbench.grid_export import GridViewport
from ..workbench.study_replay import StudyReplay, replay_study, study_project_basis
from ..workbench.study_layouts import begin_study_input, layout_objects, study_layout_inputs
from ..workbench.study_usage import StudyUsage, study_usage
from ..workbench.study_tolerance import StudyTolerance, inspect_tolerance
from ..workbench.adjustment_policy import AdjustmentPolicy, inspect_adjustment_policy
from ..workbench.policy_records import save_policy_comparison
from ..workbench.tolerance_refinement import RefineStudyTolerance, refine_tolerance
from ..workbench.study_search import StudyReassessment, StudySearch, layout_search_parameters, reassess_search, run_search, search_measures
from ..workbench.project_outputs import ProjectReference, project_outputs
from ..workbench.editable_results import canvas_models, editable_results, last_model_run
from ..workbench.model_comparison import CanvasInputComparison, compare_project_inputs, read_recheck_source, recheck_project
from ..workbench.projects import current_project_input, normalize_project, read_project_inputs, reconcile_legacy_indices
from ..workbench.projects import PreviewReplacement, ReplacementCandidates
from ..workbench.component_selection import stock_candidates
from ..workbench.measurement_view import InspectMeasurement, inspect_measurement
from ..workbench.projects import OpticalProject, ProjectRevisionConflict, ReplaceComponent, ReplaceCrystal, SaveProject, read_project, replace_component, revision_summaries, model_version_index, save_project
from ..workbench.optics import OpticalScan2D, OpticalSystem, analyze_system, element_catalog
from ..workbench.observation import OpticalLayout
from ..workbench.authoring import EditableLayout, analyze_editable
from ..workbench.layout_prefix import LayoutPrefixes, select_prefix_endpoint
from ..workbench.mode_overlap import ModeOverlap, evaluate_overlap, overlap_input
from ..workbench.beam_readings import BeamReading, register_reading, readings_for_revision
from ..workbench.beam_comparisons import CompareBeam, compare_reading, observation_index, comparison_history
from ..workbench.prediction_comparisons import DraftPredictionComparison, PredictionComparison, compare_predictions, compare_working_predictions
from ..workbench.prediction_checks import PredictionCheck, check_prediction
from ..workbench.power_measurements import MeasurePower, measure_power, meter_history
from ..workbench.beam_studies import BeamStudy, PredictionPositions, beam_study_history, run_beam_study
from ..workbench.measured_launch import MeasuredLaunch, bind_measured_launch
from ..workbench.analyses import run_scan, scans_for_revision
from ..workbench.station_outputs import with_station_links
from .cases import draft_scope, get_case_or_404
from .scientific_json import scientific_json

router = APIRouter()
_start_lock = Lock()


@router.get("/{case_id}/studies/catalog")
def available_studies(case_id: int, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    return {"items": [study.describe(provider) for study, provider in STUDIES.values()], "issues": STUDY_ISSUES}


@router.post("/{case_id}/capabilities/search")
def search_capabilities(case_id: int, request: BrowserSearch, db: Session = Depends(get_db),
                        principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return {**DIRECTORY.search_case(db, case_id, request, full_scope=True), "issues": STUDY_ISSUES}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/studies/catalog/{name}")
def study_definition(case_id: int, name: str, db: Session = Depends(get_db),
                     principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    if name not in STUDIES:
        raise HTTPException(404, "这个计算未安装。仍可查看已存的结果。")
    study, provider = STUDIES[name]
    return study.describe(provider)


@router.get("/{case_id}/studies/project-outputs")
def scientific_project_outputs(case_id: int, revision_id: int, port: str | None = Query(None, min_length=1, max_length=2600), db: Session = Depends(get_db),
                               principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    if revision_id <= 0:
        raise HTTPException(422, "请选择有效的工程版本。")
    try:
        result = project_outputs(db, case_id, revision_id, compact=port is None, only_port=port)
    except ValueError as exc:
        raise HTTPException(404, "当前案例中找不到此工程版本。") from exc
    if port is not None and not result["outputs"]:
        raise HTTPException(404, "此工程版本没有发布所选物理量。可从此版本的取值目录重新选择。")
    return result


@router.post("/{case_id}/studies/layout-objects")
def list_layout_objects(case_id: int, reference: ProjectReference | ResultReference, db: Session = Depends(get_db),
                        principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        port, value, _ = frozen_quantity(db, case_id, reference)
        return layout_objects(port, value)
    except UnpublishedOutput as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/studies/runs")
def list_study_runs(case_id: int, before_id: int | None = None, name: str | None = Query(None, min_length=1, max_length=200),
                    input_key: str | None = Query(None,min_length=1,max_length=200),
                    revision_id: int | None = Query(None,gt=0), port: str | None = Query(None,min_length=1,max_length=2000),
                    db: Session = Depends(get_db),
                    principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return study_history(db, case_id, before_id, name=name, input_key=input_key, revision_id=revision_id, port=port)
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


@router.post("/{case_id}/studies/usage")
def find_study_usage(case_id: int, request: StudyUsage, db: Session = Depends(get_db),
                     principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return study_usage(db.get_bind(), case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/studies/replay")
def repeat_study(case_id: int, request: StudyReplay, db: Session = Depends(get_db),
                 principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    (assert_case_edit if request.execute else assert_case_view)(db, case, principal)
    try:
        return replay_study(db.get_bind(), case_id, request, STUDIES)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/studies/runs/{run_id}")
def read_study_run(case_id: int, run_id: int, http: Request, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    row = db.query(GeneratedContent).filter_by(case_id=case_id, id=run_id, content_type="scientific_study").one_or_none()
    if row is None:
        raise HTTPException(404, "当前案例中找不到这次计算。")
    origins = study_project_basis(db, case_id, row.id, row.content.get("connections"))
    installed = installed_study_receipt(row.content.get("definition"), row.content.get("solver_receipt"), STUDIES)
    return scientific_json(http, {"id": row.id, **row.content, "project_basis": origins, "installed_solver_receipt":installed,
                                  "layout_inputs": study_layout_inputs(run_id, row.content)})


@router.post("/{case_id}/studies/runs/{run_id}/project")
def continue_study_layout(case_id: int, run_id: int, request: ChooseStudyInput, db: Session = Depends(get_db),
                          principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return begin_study_input(db, case_id, run_id, request.input_key, request.variant_name)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/studies/runs/{run_id}/output")
def read_study_output(case_id: int, run_id: int, request: Request, port: str = Query(min_length=1),
                      db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        offer, value, _ = frozen_quantity(db, case_id, ResultReference(run_id=run_id, port=port))
    except UnpublishedOutput as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return scientific_json(request, {"port": offer.model_dump(mode="json"), "value": value})


@router.get("/{case_id}/studies/runs/{run_id}/native-grid")
def download_native_grid(case_id: int, run_id: int, db: Session = Depends(get_db),
                         principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    archive = SpooledTemporaryFile(max_size=8 * 1024**2, mode="w+b")
    try:
        export_native_grid(db, case_id, run_id, STUDIES, archive)
        size = archive.seek(0, 2)
        archive.seek(0)
        return StreamingResponse(iter(lambda: archive.read(1024**2), b""), media_type="application/octet-stream",
                                 headers={"Content-Disposition": f'attachment; filename="grid-{run_id}.npz"',
                                          "Content-Length": str(size)},
                                 background=BackgroundTask(archive.close))
    except Exception as exc:
        archive.close()
        if isinstance(exc, UnpublishedOutput):
            raise HTTPException(404, str(exc)) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(422, str(exc)) from exc
        if isinstance(exc, StudyFailure):
            raise HTTPException(500, str(exc)) from exc
        raise


@router.get("/{case_id}/studies/runs/{run_id}/native-grid/view")
def native_grid_view(case_id: int, run_id: int, request: Request, options: Annotated[GridViewport, Query()],
                     db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return scientific_json(request, view_native_grid(db, case_id, run_id, STUDIES, options))
    except UnpublishedOutput as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except StudyFailure as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/{case_id}/studies/search")
def search_saved_study(case_id: int, request: StudySearch, db: Session = Depends(get_db),
                       principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return run_search(db.get_bind(), case_id, request, STUDIES)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/studies/runs/{run_id}/searches")
def saved_searches(case_id: int, run_id: int, before_id: int | None = None, search_id: int | None = None,
                   db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    record = GeneratedContent
    query = db.query(record).filter(record.case_id==case_id, record.content_type=="study_search",
                                    record.content["baseline_id"].as_integer()==run_id)
    if search_id is not None:
        row = query.filter(record.id==search_id).one_or_none()
        if row is None:
            raise HTTPException(404, "本次基线计算中找不到这组搜索。")
        return {"id":row.id, **row.content}
    base = db.query(record).filter_by(id=run_id,case_id=case_id,content_type="scientific_study").one_or_none()
    if base is None:
        raise HTTPException(404, "本案例中找不到所选计算。")
    if before_id is not None:
        query = query.filter(record.id<before_id)
    rows = query.with_entities(record.id, record.content["feasible"].as_integer(), record.content["preferred"].as_integer(),
                               record.content["ranking_mode"].as_string(), record.content["source_search_id"].as_integer(),
                               record.content["adjustment_evaluation"]["search_id"].as_integer()
                               ).order_by(record.id.desc()).limit(26).all()
    return {"measures":search_measures(base.content), "layout_parameters":layout_search_parameters(db,case_id,base.content),
            "items":[{"id":r.id,"feasible":r[1],"preferred":r[2],"ranking_mode":r[3],"source_search_id":r[4],
                      **({"policy_source_id":r[5]} if r[5] is not None else {})} for r in rows[:25]],
            "next_before_id":rows[24].id if len(rows)>25 else None}


@router.post("/{case_id}/studies/reassess")
def reassess_saved_search(case_id: int, request: StudyReassessment, db: Session = Depends(get_db),
                          principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return reassess_search(db.get_bind(), case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/studies/tolerance")
def inspect_adjustment_range(case_id: int, request: StudyTolerance, db: Session = Depends(get_db),
                            principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return inspect_tolerance(db.get_bind(), case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/studies/adjustment-policy")
def inspect_assembly_settings(case_id: int, request: AdjustmentPolicy, db: Session = Depends(get_db),
                              principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return inspect_adjustment_policy(db.get_bind(), case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/studies/adjustment-policy/records")
def save_assembly_settings(case_id: int, request: AdjustmentPolicy, db: Session = Depends(get_db),
                           principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return save_policy_comparison(db.get_bind(), case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/studies/tolerance/refine")
def refine_adjustment_range(case_id: int, request: RefineStudyTolerance, db: Session = Depends(get_db),
                            principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return refine_tolerance(db.get_bind(), case_id, request, STUDIES)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/studies/preview/{name}")
def preview_study(case_id: int, name: str, request: StudyInvocation, http: Request, db: Session = Depends(get_db),
                  principal: Principal = Depends(get_current_principal),
                  prefix_plane_uid: str | None = Query(None,min_length=1,max_length=100),
                  prefix_path_uid: str | None = Query(None,min_length=1,max_length=100)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    if name not in STUDIES or not STUDIES[name][0].live_layout:
        raise HTTPException(404, "该计算未提供随动预览。")
    study, provider = STUDIES[name]
    if (request.version, request.provider) != (study.version, provider):
        raise HTTPException(409, "方法版本已更新，请刷新工作台。")
    if study.live_layout in request.connections or request.reuse_inputs_from is not None:
        raise HTTPException(422, "随动预览使用当前画布的光路。请将该光路放入输入，仅为其他物理量选择已存来源。")
    try:
        inputs = request.inputs
        if prefix_plane_uid is not None or prefix_path_uid is not None:
            selectors = study.layout_prefix_inputs
            if not selectors or prefix_plane_uid is None:
                raise ValueError("请选择支持从光源计算到单个观察面的方法，并指定该观察面。")
            layout, _ = LayoutPrefixes.from_document(inputs.get(study.live_layout)).project(prefix_plane_uid,prefix_path_uid or "primary")
            inputs = select_prefix_endpoint(selectors,{**inputs,study.live_layout:layout},prefix_plane_uid)
        return scientific_json(http, lambda: preview_connected_study(db, case_id, study, provider, inputs, request.connections))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except StudyFailure as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/{case_id}/studies/run/{name}")
def submit_study(case_id: int, name: str, request: StudyInvocation, http: Request, db: Session = Depends(get_db),
                 principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    if name not in STUDIES:
        raise HTTPException(404, "此计算未安装。可以从历史记录查看原结果。")
    study, provider = STUDIES[name]
    if (request.version, request.provider) != (study.version, provider):
        raise HTTPException(409, "求解器版本或来源已变更，请刷新能力列表后重新核对输入。")
    try:
        return scientific_json(http, lambda: run_study(db.get_bind(), case_id, study, provider, request.inputs,
                               connections=request.connections, reuse_inputs_from=request.reuse_inputs_from))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except StudyFailure as exc:
        raise HTTPException(500, str(exc)) from exc


@router.post("/{case_id}/optical-project/power-measurements")
def compare_meter_reading(case_id: int, request: MeasurePower, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return measure_power(db, case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/optical-project/{revision_id}/power-measurements")
def read_meter_readings(case_id: int, revision_id: int, measurement_id: int | None = None, db: Session = Depends(get_db),
                        principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return meter_history(db, case_id, revision_id, measurement_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{case_id}/measurement-view")
def view_measurement_table(case_id: int, request: InspectMeasurement, db: Session = Depends(get_db),
                           principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return inspect_measurement(db, case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/beam-studies")
def create_beam_study(case_id: int, request: BeamStudy, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return run_beam_study(db, case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/beam-studies")
def list_beam_studies(case_id: int, study_id: int | None = None, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return beam_study_history(db, case_id, study_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{case_id}/beam-studies/{study_id}/predict")
def predict_saved_fit(case_id: int, study_id: int, request: PredictionPositions, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return beam_study_history(db, case_id, study_id, request.positions_mm)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/component-models")
def get_model_library(case_id: int, request: LibraryQuery = Depends(), db: Session = Depends(get_db),
                      principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return library(db, principal, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/component-models")
def capture_component(case_id: int, request: CaptureModel, db: Session = Depends(get_db),
                      principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return capture_model(db, principal, case_id, request)
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, LibraryConflict) else 422, str(exc)) from exc


@router.post("/{case_id}/component-models/preview-placement")
def preview_model_placement(case_id: int, request: PlacementPreview, db: Session = Depends(get_db),
                            principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return place_model(db, principal, case_id, request, preview=True)
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, LibraryConflict) else 422, str(exc)) from exc


@router.get("/{case_id}/optical-project")
def get_project(case_id: int, request: Request, revision_id: int | None = None, latest_scan_only: bool = False,
                db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    try:
        current = read_project(db, case_id, revision_id)
        if current and current.get("analysis"):
            current = {**current, "analysis": with_station_links(current["project"], current["analysis"])}
        scans = scans_for_revision(db, case_id, current["revision_id"], latest_2d=latest_scan_only) if current else []
        scan = next((s for s in scans if s["dimensions"] == 2), None)
        return scientific_json(request, {"draft_scope": draft_scope(case, principal), "current": current, "scan": scan,
                **({} if latest_scan_only else {"scans":scans}),
                "revisions": revision_summaries(db, case_id), "element_catalog": element_catalog(),
                "live_studies":[{"name":s.name,"title":s.title,"selectors":s.layout_selectors}
                                for s,_ in STUDIES.values() if s.live_layout]})
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{case_id}/optical-project/model-input")
def get_model_input(case_id: int, revision_id: int | None = None, db: Session = Depends(get_db),
                    principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        record = (read_project_inputs(db, case_id, [revision_id], summary=True)[revision_id] if revision_id is not None
                  else current_project_input(db, case_id, summary=True))
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {**basis(record), "project": record["project"] if record else None,
            "change_note": record.get("change_note") if record else None}


class AdoptRequest(BaseModel):
    snapshot_id: int = Field(gt=0)
    base_revision_id: int | None = Field(..., gt=0)
    reviewed_against: int | None = Field(default=None, gt=0)
    reviewed_project: OpticalProject | None = None


@router.post("/{case_id}/optical-project/variants")
def store_variant(case_id: int, request: SaveVariant, db: Session = Depends(get_db),
                  principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return save_variant(db, case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/measured-launch")
def measured_launch(case_id: int, request: MeasuredLaunch, db: Session = Depends(get_db),
                    principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return bind_measured_launch(db, case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/adopt-snapshot")
def adopt_study_model(case_id: int, request: AdoptRequest, db: Session = Depends(get_db),
                     principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return adopt_snapshot(db, case_id, request.snapshot_id, request.base_revision_id,
                              reviewed_against=request.reviewed_against, reviewed_project=request.reviewed_project)
    except ProjectRevisionConflict as exc:
        raise HTTPException(409, "主工程在试算后已被修改。请核对新版本和这份快照的差异。") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/normalize")
def normalize_editor_input(case_id: int, project: OpticalProject, revision_id: int | None = None,
                           db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        source = read_project_inputs(db,case_id,[revision_id])[revision_id] if revision_id is not None else None
        return normalize_project(project,source).model_dump()
    except ValueError as exc:
        raise HTTPException(422,str(exc)) from exc


def _preview_kind(value):
    if isinstance(value, OpticalLayout) or (isinstance(value, dict) and "system" in value):
        return "layout"
    return "path"


class PreviewInput(RootModel[Annotated[
    Annotated[OpticalSystem, Tag("path")] | Annotated[EditableLayout, Tag("layout")],
    Discriminator(_preview_kind),
]]):
    """Existing flat and nested request bodies, with validation only against the submitted form."""


@router.post("/{case_id}/optical-project/preview")
def preview_project(case_id: int, request: PreviewInput, http: Request,
                    revision_id: int | None = Header(default=None, gt=0, alias="X-BeamWeft-Source-Revision"),
                    db: Session = Depends(get_db),
                    principal: Principal = Depends(get_current_principal)):
    """Read-only geometry preview. Use the same propagation and stability calculation as a revision."""
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    layout = request.root if isinstance(request.root, EditableLayout) else None
    system = layout.system if layout else request.root
    display_system = system.model_copy(update={"samples_per_segment": min(16, system.samples_per_segment)})
    try:
        if revision_id is not None and layout and (
            any("shared_index_wavelength_nm" not in path.model_fields_set and path.shared_index_values()
                for path in layout.incident_paths)
            or any(e.medium_role and not e.index_reference
                   for train in (layout.system, *layout.incident_paths) for e in train.elements)
        ):
            original = read_project_inputs(db, case_id, [revision_id])[revision_id]["project"]
            layout = reconcile_legacy_indices(layout, original)
        result = analyze_editable(layout, samples_per_segment=16, include_sections=True) if layout else analyze_system(display_system)
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(422, str(exc)) from exc
    planes = {}
    for name, plane in result["planes"].items():
        profile, breaks = [], []
        start_segment = False
        boundaries = set(plane.get("profile_breaks", ()))
        for index, point in enumerate(plane["samples"]):
            start_segment = start_segment or index in boundaries
            if point.get("radius_mm") is None:
                start_segment = True
                continue
            if start_segment and profile:
                breaks.append(len(profile))
            profile.append([point["z_mm"], point["radius_mm"]])
            start_segment = False
        planes[name] = {
            "stable": plane.get("stable"), "m": plane.get("m"), "marginal": plane.get("marginal"),
            **{key:plane[key] for key in ("collimated_focus_from_exit_mm", "collimated_focus_note", "message",
                                          "profile_omissions",
                                          "effective_wavelength_nm", "beam_quality_m2", "width_basis",
                                          "center_basis", "center_profile", "center_profile_breaks") if key in plane},
            "profile": profile,
            **({"profile_breaks": breaks} if breaks else {}),
            "radii_mm": [node.get("radius_mm") for node in plane["elements"]],
        }
    return scientific_json(http, {"mode": result["mode"], "planes": planes, "model_warnings": result.get("model_warnings", []),
            **{key: result[key] for key in ("material_indices", "sections", "network", "status", "input_issues", "solver_receipt", "primary_center_axes", "profile_sampling_version") if key in result}})


@router.get("/{case_id}/optical-project/{revision_id}/beam-readings")
def list_beam_readings(case_id: int, revision_id: int, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return readings_for_revision(db, case_id, revision_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{case_id}/optical-project/mode-overlap")
def mode_overlap(case_id: int, request: ModeOverlap, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        layout, record = overlap_input(db, case_id, request)
        return evaluate_overlap(layout, record, request)
    except (ValueError, ArithmeticError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/optical-project/beam-observations")
def case_beam_observations(case_id: int, before_id: int | None = None, db: Session = Depends(get_db),
                           principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    return observation_index(db, case_id, before_id)


@router.get("/{case_id}/optical-project/versions")
def comparison_versions(case_id: int, before_id: int | None = None, variant_name: str | None = None, db: Session = Depends(get_db),
                        principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    return model_version_index(db, case_id, before_id, variant_name)


@router.post("/{case_id}/optical-project/{revision_id}/recheck")
def recheck_frozen_model(case_id: int, revision_id: int, db: Session = Depends(get_db),
                         principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return recheck_project(read_recheck_source(db, case_id, revision_id))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/input-comparison")
def compare_model_inputs(case_id: int, request: CanvasInputComparison, db: Session = Depends(get_db),
                         principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return compare_project_inputs(db, case_id, request, draft=request.working_draft)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/optical-project/{revision_id}/prediction-comparisons")
def prediction_history(case_id: int, revision_id: int, before_id: int | None = None, db: Session = Depends(get_db),
                       principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return comparison_history(db, case_id, revision_id, before_id, kind="beam_prediction_comparison")
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get("/{case_id}/optical-project/prediction-comparisons")
def case_prediction_history(case_id: int, before_id: int | None = None, db: Session = Depends(get_db),
                            principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    return comparison_history(db, case_id, None, before_id, kind="beam_prediction_comparison", all_versions=True)


@router.get("/{case_id}/optical-project/prediction-checks")
def prediction_check_history(case_id: int, prediction_id: int = Query(gt=0), before_id: int | None = Query(None, gt=0),
                             db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    records = db.query(GeneratedContent).filter_by(case_id=case_id)
    if records.filter_by(id=prediction_id, content_type="beam_prediction_comparison").with_entities(GeneratedContent.id).first() is None:
        raise HTTPException(404, "本工程中找不到指定的预测比较。")
    checks = records.filter_by(content_type="prediction_check").filter(
        GeneratedContent.content["prediction_id"].as_integer() == prediction_id)
    if before_id is not None:
        checks = checks.filter(GeneratedContent.id < before_id)
    page = checks.order_by(GeneratedContent.id.desc()).limit(26).all()
    return {"items": [{"id": row.id, **row.content} for row in page[:25]],
            "next_before_id": page[24].id if len(page) > 25 else None}


@router.post("/{case_id}/optical-project/prediction-checks")
def check_frozen_prediction(case_id: int, request: PredictionCheck, db: Session = Depends(get_db),
                            principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return check_prediction(db, case_id, request)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _comparison_draft_key(request: Request):
    # Hash the submitted draft before model validation assigns identities to legacy anonymous optics.
    payload = await request.json()
    draft = payload.get("working_draft") if isinstance(payload, dict) else None
    canonical = json.dumps(draft, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "width_draft_v1:" + sha256(canonical.encode()).hexdigest()


@router.post("/{case_id}/optical-project/prediction-comparisons")
def compare_canvas_draft(case_id: int, request: DraftPredictionComparison, draft_key: str = Depends(_comparison_draft_key),
                         db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return compare_working_predictions(db, case_id, request, draft_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/{revision_id}/prediction-comparisons")
def plan_detector_positions(case_id: int, revision_id: int, request: PredictionComparison,
                            db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return compare_predictions(db, case_id, request, revision_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/{case_id}/optical-project/{revision_id}/beam-comparisons")
def beam_comparisons(case_id: int, revision_id: int, before_id: int | None = None, db: Session = Depends(get_db),
                     principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return comparison_history(db, case_id, revision_id, before_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/{case_id}/optical-project/{revision_id}/beam-comparisons")
def compare_beam_observation(case_id: int, revision_id: int, request: CompareBeam, db: Session = Depends(get_db),
                             principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return compare_reading(db, case_id, request, revision_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/beam-readings")
def create_beam_reading(case_id: int, request: BeamReading, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    assert_case_edit(db, get_case_or_404(case_id, db), principal)
    try:
        return register_reading(db, case_id, request, "manual")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project")
def update_project(case_id: int, request: SaveProject, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    try:
        # A user editing the UI can explicitly check the unlock option. Agent saves
        # use the separate dispatcher and must provide evidence from its user turn.
        return save_project(db, case_id, request, allow_unlock=request.unlock_quote == "用户在工程编辑器明确解锁")
    except ValueError as exc:
        if isinstance(exc, ProjectRevisionConflict):
            raise HTTPException(409, "工程已有新版本。请先核对两边的改动，再保存草稿。") from exc
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/replace-component")
def replace_project_component(case_id: int, request: ReplaceComponent, db: Session = Depends(get_db),
                              principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    with _start_lock:
        try:
            return replace_component(db, case_id, request)
        except ValueError as exc:
            message = str(exc)
            raise HTTPException(409 if isinstance(exc, ProjectRevisionConflict) or "正在运行" in message else 422, message) from exc


@router.get("/{case_id}/optical-project/replacement-candidates")
def replacement_candidates(case_id: int, request: ReplacementCandidates = Depends(), db: Session = Depends(get_db),
                           principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        project = read_project(db, case_id, request.revision_id)["project"]
        return {"revision_id": request.revision_id, **stock_candidates(db, project, request.target_path_uid,
                request.target_element_uid, request.target_role, request.search, request.skip, request.limit)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{case_id}/optical-project/replace-component/preview")
def preview_replacement(case_id: int, request: PreviewReplacement, db: Session = Depends(get_db),
                        principal: Principal = Depends(get_current_principal)):
    assert_case_view(db, get_case_or_404(case_id, db), principal)
    try:
        return replace_component(db, case_id, request, preview=True)
    except ValueError as exc:
        raise HTTPException(409 if isinstance(exc, ProjectRevisionConflict) else 422, str(exc)) from exc


@router.post("/{case_id}/optical-project/replace-crystal")
def replace_project_crystal(case_id: int, request: ReplaceCrystal, db: Session = Depends(get_db),
                            principal: Principal = Depends(get_current_principal)):
    return replace_project_component(case_id, ReplaceComponent(**request.model_dump(), target_role="crystal"), db, principal)


@router.post("/{case_id}/optical-project/{revision_id}/scan-2d")
async def scan_project(case_id: int, revision_id: int, request: OpticalScan2D, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    try:
        return await run_scan(db, case_id, request, revision_id=revision_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=24000)
    attachment_ids: list[int] = Field(default_factory=list, max_length=8)
    context_refs: InputRefs = Field(default_factory=InputRefs)
    working_draft: WorkingDraft | None = None
    viewed_empty_project: bool = False


def recover(db, case_id, task_id=None):
    return reconcile(db, case_id, "workbench", task_id)


def task_json(task, include_tools=True, receipt_basis=None, ownership=None, model_outputs=None):
    artifact = task.final_content
    if artifact is not None and artifact.case_id != task.case_id:
        artifact = None
    payload = artifact.content if artifact else {}
    result = payload.get("workbench")
    origin = payload.get("result_origin")
    if result and not origin:
        origin = "archived"
        if include_tools and service.recorded_finish(task, matching=result) is not None:
            origin = "recorded_model_finish"
    unresolved = ownership == "unverified" and task.status in {"pending", "running"}
    if not result and include_tools and (unresolved or task.status in {"failed", "cancelled"}):
        result, origin = service.result_from_trace(task, unverified=unresolved)
    original = payload.get("model_basis") or receipt_basis
    profile = payload.get("agent_profile")
    request_refs = payload.get("request_references")
    if include_tools:
        receipt = next((c for c in task.tool_calls if c.tool_name == "workbench_request"), None)
        request = (receipt.input_json or {}) if receipt else {}
        original = original or request.get("model_basis")
        profile = profile or request.get("agent_profile")
        if receipt:
            request_refs = {"attachment_ids":request.get("attachment_ids") or [], "context_refs":request.get("context_refs") or {}}
    return {"id": task.id, "goal": task.goal, "status": task.status, "created_at": task.created_at.isoformat() if task.created_at else None,
            "model_basis": original, "result_model": payload.get("result_model"),
            **(model_outputs or {}),
            **({"agent_profile": profile} if profile else {}),
            **({"request_references": request_refs} if request_refs is not None else {}),
            "final_content_id": artifact.id if artifact else None, "active": ownership == "live",
            **({"execution_owner_state":ownership} if ownership and task.status in {"pending","running"} else {}),
            "tools_loaded": include_tools,
            "result": result, "result_origin":origin,
            "steps": [{"title": s.title, "label": capability_title(s.title), "status": s.status, "purpose": s.rationale, "summary": s.result_summary} for s in task.steps],
            "tools": [{"id": c.id, "name": c.tool_name, "label": capability_title(c.tool_name), "status": c.status, "input": c.input_json,
                       "output": c.output_json, "error": c.error_message, "latency_ms": c.latency_ms}
                      for c in sorted(task.tool_calls, key=lambda c: c.id) if c.tool_name != "workbench_request"] if include_tools else []}


def _conversation_page(db, case, limit, before_id=None):
    sid = (case.parameters or {}).get("workbench_session_id") or (case.parameters or {}).get("design_session_id")
    rows = []
    if sid:
        query = (db.query(AgentChatMessage).join(AgentChatSession)
                 .filter(AgentChatMessage.session_id == sid, AgentChatSession.case_id == case.id))
        if before_id is not None:
            query = query.filter(AgentChatMessage.id < before_id)
        query = query.order_by(AgentChatMessage.id.desc())
        rows = (query.limit(limit + 1) if limit is not None else query).all()
    has_earlier = limit is not None and len(rows) > limit
    page = list(reversed(rows[:limit]))
    outputs = editable_results(db, case.id, [
        m.metadata_json["workbench_run_id"] for m in page if m.role == "assistant"
        and isinstance((m.metadata_json or {}).get("workbench_run_id"), int)
    ])
    messages = []
    for m in page:
        metadata = m.metadata_json
        run_id = (metadata or {}).get("workbench_run_id")
        if m.role == "assistant" and isinstance(run_id, int) and run_id in outputs:
            metadata = {**metadata, **outputs[run_id]}
        messages.append({"id": m.id, "role": m.role, "content": m.content, "metadata": metadata})
    return {"messages": messages, "message_page": {
        "session_id": sid if messages else None, "next_before_id": messages[0]["id"] if has_earlier else None}}


@router.get("/{case_id}/workbench/messages")
def conversation_messages(case_id: int, request: Request, before_id: int = Query(gt=0), limit: int = Query(20, ge=1, le=100),
                          db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    return scientific_json(request, _conversation_page(db, case, limit, before_id))


@router.get("/{case_id}/workbench")
def get_workbench(case_id: int, request: Request, include_capabilities: bool = True, include_latest_tools: bool = True, include_attachments: bool = True,
                  message_limit: int | None = Query(None, ge=1, le=100),
                  db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    owners = recover(db, case_id)
    conversation = _conversation_page(db, case, message_limit)
    tasks = (db.query(AgentTask).options(selectinload(AgentTask.steps), selectinload(AgentTask.final_content))
             .filter_by(case_id=case_id, mode="workbench").order_by(AgentTask.id.desc()).limit(25).all())
    latest_basis = None
    if not include_latest_tools and tasks and not tasks[0].final_content_id:
        latest_basis = db.query(AgentToolCall.input_json["model_basis"]).filter_by(
            task_id=tasks[0].id, tool_name="workbench_request").limit(1).scalar()
    workspace = workspace_view(db, case, tasks[0] if tasks else None, include_attachments)
    outputs = editable_results(db, case_id, [task.id for task in tasks])
    no_main = workspace["project_revision_id"] is None
    choices = canvas_models(db, case_id, outputs, whole_project=no_main)
    # A long sequence of read-only studies can push the latest design off the task-history page.
    older_design = last_model_run(db, case_id, tasks[-1].id) if not no_main and not outputs and len(tasks) == 25 else None
    count, newest = db.query(func.count(Attachment.id),func.max(Attachment.id)).filter(Attachment.case_id==case_id).one()
    return scientific_json(request, {"draft_scope": draft_scope(case, principal), **conversation,
            **({"canvas_run": older_design} if older_design else {}),
            **({"canvas_models": choices} if no_main else {}),
            "runs": [task_json(task, include_tools=(index == 0 and include_latest_tools),receipt_basis=latest_basis if index==0 else None,
                               ownership=owners.get(task.id), model_outputs=outputs.get(task.id)) for index, task in enumerate(tasks)],
            "workspace": workspace, "archive_index":{"count":count,"newest_id":newest},
            **({"capabilities": [{"name": name, "description": c["scope"], "title":c["title"]}
                                 for name,c in DIRECTORY.entries.items()]} if include_capabilities else {}),
            **({"attachments":workspace["attachments"]} if include_attachments else {})})


@router.get("/{case_id}/workbench/runs/{task_id}")
def get_run(case_id: int, task_id: int, request: Request, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_view(db, case, principal)
    owners = recover(db, case_id, task_id)
    task = db.get(AgentTask, task_id)
    if not task or task.case_id != case_id or task.mode != "workbench":
        raise HTTPException(404, "任务不存在")
    outputs = editable_results(db, case_id, [task_id], follow_edits=True)
    return scientific_json(request, task_json(task, ownership=owners.get(task_id), model_outputs=outputs.get(task_id)))


@router.post("/{case_id}/workbench/runs", status_code=202)
def start_run(case_id: int, request: RunRequest, background_tasks: BackgroundTasks,
              db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    if not request.message.strip():
        raise HTTPException(422, "请描述目标、现有信息或回复 Agent 的问题。")
    refs = request.context_refs.model_dump()
    draft_selection = bool(request.working_draft and request.context_refs.selects_objects() and refs["project_revision_id"] is None)
    if not draft_selection:
        try:
            validate_context_refs(db, case_id, ContextRefs.model_validate(refs))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if sum((request.working_draft is not None, request.context_refs.project_revision_id is not None, request.viewed_empty_project)) > 1:
        raise HTTPException(422, "请选择一个本轮输入：屏幕草稿、指定工程版本或空工程。")
    request.attachment_ids = list(dict.fromkeys(request.attachment_ids + request.context_refs.attachment_ids))
    if len(request.attachment_ids) > 8:
        raise HTTPException(422, "每轮最多引用8个附件，请减少选择后重试。")
    attachments = db.query(Attachment).filter(Attachment.id.in_(request.attachment_ids)).all()
    if len(attachments) != len(set(request.attachment_ids)) or any(a.case_id != case_id for a in attachments):
        raise HTTPException(404, "附件不存在或不属于当前案例。")
    with _start_lock:
        recover(db, case_id)
        if db.query(AgentTask).filter(AgentTask.case_id == case_id, AgentTask.id.in_(list(service.ACTIVE_RUNS))).first():
            raise HTTPException(409, "上一轮仍在结束当前调用，请稍后继续，已有结果会保留。")
        if db.query(AgentTask).filter(AgentTask.case_id == case_id, AgentTask.mode.in_(["workbench", "design"]), AgentTask.status.in_(["pending", "running"])).first():
            raise HTTPException(409, "此案例正在运行，请完成或停止当前任务后继续。")
        session = service.session_for_case(db, case, principal.user_id)
        task = AgentTask(case_id=case_id, user_id=principal.user_id, mode="workbench", goal=request.message.strip(), status="pending")
        db.add(task)
        db.flush()
        try:
            if request.working_draft is not None:
                model = capture_snapshot(db, case_id, request.working_draft)
                if draft_selection:
                    refs["project_revision_id"] = model["revision_id"]
                    validate_context_refs(db, case_id, ContextRefs.model_validate(refs))
            elif request.viewed_empty_project:
                model = None
            elif request.context_refs.project_revision_id is not None:
                selected_id = request.context_refs.project_revision_id
                model = read_project_inputs(db, case_id, [selected_id], summary=True)[selected_id]
            else:
                model = current_project_input(db, case_id, summary=True)
        except (ArithmeticError, ValueError) as exc:
            db.rollback()
            raise HTTPException(422, f"这份光路无法作为本轮输入。{exc}") from exc
        model_basis = basis(model)
        db.add(AgentChatMessage(session_id=session.id, role="user", content=task.goal,
                                metadata_json={"workbench_run_id": task.id, "model_basis":model_basis, "attachment_ids": request.attachment_ids,
                                               "context_refs": refs}))
        lease = RunLease(db.get_bind())
        db.add(AgentToolCall(task_id=task.id, tool_name="workbench_request", input_json={"session_id": session.id, "model_basis":model_basis,
                            "execution_owner":lease.stamp,
                            "attachment_ids": request.attachment_ids, "context_refs": refs,
                            "principal": {"actor": principal.actor, "role": principal.role, "user_id": principal.user_id}}, output_json={}))
        service.ACTIVE_RUNS.add(task.id)
        try:
            db.commit()
        except Exception:
            service.ACTIVE_RUNS.discard(task.id)
            lease.close(remove=True)
            raise
        background_tasks.add_task(lease.execute, service.execute_run, task.id)
        return {"task_id": task.id, "status": task.status, "model_basis":model_basis}


@router.post("/{case_id}/workbench/runs/{task_id}/stop")
def stop_run(case_id: int, task_id: int, db: Session = Depends(get_db), principal: Principal = Depends(get_current_principal)):
    case = get_case_or_404(case_id, db)
    assert_case_edit(db, case, principal)
    task = db.get(AgentTask, task_id)
    if not task or task.case_id != case_id or task.mode != "workbench":
        raise HTTPException(404, "任务不存在")
    db.query(AgentTask).filter(AgentTask.id == task_id, AgentTask.case_id == case_id,
                               AgentTask.status.in_(["pending", "running"])).update(
        {AgentTask.status:"cancelled"}, synchronize_session=False)
    db.commit()
    return {"task_id": task.id, "status": task.status}

"""An optional scalar planner cannot discard the current engineering model."""

import asyncio
from copy import deepcopy

import pytest
from sqlalchemy.orm import sessionmaker

from app.design import service
from app.models import AgentTask, ExperimentCase
from app.workbench.contracts import LinearDesign
from app.workbench.projects import SaveProject, read_project, save_project
from app.workbench.tools import execute


def current_project(db, kind):
    case = ExperimentCase(
        title="通用实际工程",
        goal="按实际完整光路计算",
        cavity_type="custom",
        parameters={"design_facts": {"pump_power_w": {"value": 6}}},
    )
    db.add(case)
    db.commit()
    elements = [
        {"name": "M1", "kind": "mirror", "radius_mm": 200},
        {"name": "空气", "kind": "space", "length_mm": 3},
        {"name": "晶体1", "kind": "space", "length_mm": 5, "n": 1.8},
        {"name": "后间隙", "kind": "space", "length_mm": 12},
        {"name": "M2", "kind": "mirror"},
    ]
    system = {"mode": "standing_wave", "wavelength_nm": 1064, "elements": elements}
    parts = [{"element_index": 2, "role": "crystal", "source": "实物"}]
    if kind == "single_pass":
        system = {
            "mode": "single_pass",
            "wavelength_nm": 808,
            "input_radius_mm": 0.1,
            "elements": [
                {"name": "透镜前", "kind": "space", "length_mm": 50},
                {"name": "透镜", "kind": "lens", "focal_mm": 100},
                {"name": "后路", "kind": "space", "length_mm": 50},
            ],
        }
        parts = []
    elif kind == "ring":
        system["mode"] = "ring"
    elif kind == "multi_crystal":
        elements.insert(4, {"name": "晶体2", "kind": "space", "length_mm": 7, "n": 1.8})
        parts.append({"element_index": 4, "role": "crystal", "source": "另一独立实物"})
    elif kind == "internal_lens":
        elements.insert(3, {"name": "腔内透镜", "kind": "lens", "focal_mm": 100})
    elif kind == "split_one_crystal":
        elements.insert(3, {"name": "同一晶体端帽", "kind": "space", "length_mm": 2, "n": 1.8})
        parts.append({"element_index": 3, "role": "crystal", "source": "同一实物端帽"})
    saved = save_project(
        db,
        case.id,
        SaveProject(project={"title": "完整模型", "system": system, "parts": parts}, change_note="保存工程"),
    )
    return case, saved


@pytest.mark.parametrize("kind", ["single_pass", "ring", "multi_crystal", "internal_lens"])
def test_agent_and_legacy_entry_share_nonlossy_projection_guard(kind, client, db, monkeypatch):
    case, saved = current_project(db, kind)
    before = deepcopy(case.parameters)
    task = AgentTask(case_id=case.id, mode="workbench", status="completed", goal="使用当前工程")
    db.add(task)
    db.commit()
    result = asyncio.run(
        execute(
            "design_linear_laser",
            LinearDesign(message="使用当前工程"),
            db=db,
            case=case,
            task=task,
            provider=None,
            session_id=None,
            attachment_ids=[],
        )
    )
    assert result["status"] == "unsupported"
    assert result["engineering_design"]["candidates"] == []
    assert result["engineering_design"]["project_revision_id"] == saved["revision_id"]
    assert "analyze_optical_system" in result["next_tools"]
    assert case.parameters == before
    monkeypatch.setattr(service, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))

    def no_model_call():
        raise AssertionError("Known incompatible topology must not be extracted into scalar facts")

    monkeypatch.setattr(service, "get_ai_provider", no_model_call)
    response = client.post(f"/api/cases/{case.id}/design/runs", json={"message": "按当前实际工程重新规划"})
    assert response.status_code == 202, response.text
    state = client.get(f"/api/cases/{case.id}/design").json()
    assert state["runs"][0]["status"] == "completed", state
    design = state["revisions"][0]["content"]["engineering_design"]
    assert design["status"] == "unsupported" and design["candidates"] == []
    assert design["project_revision_id"] == saved["revision_id"]
    assert read_project(db, case.id)["revision_id"] == saved["revision_id"]


@pytest.mark.parametrize("kind", ["plain", "split_one_crystal"])
def test_single_physical_crystal_two_mirror_project_remains_eligible(kind, db):
    case, _ = current_project(db, kind)
    assert service.specialized_projection_guard(db, case) is None


def coupled_bench(db):
    from app.design.solver import solve_design
    from app.workbench.design_import import saved_geometry

    inputs = dict(cavity_type="linear", input_mirror_flat=True, output_roc_mm=210, laser_wavelength_nm=1010,
                  crystal_index=1.64, crystal_front_mm=15.5, crystal_length_mm=12, front_cap_mm=2, active_length_mm=7,
                  rear_cap_mm=3, min_output_gap_mm=40, max_output_gap_mm=50, pump_wavelength_nm=935, pump_index=1.71,
                  collimator_f_mm=45, focus_f_mm=32, source_z_mm=-150, collimator_z_mm=-101, focus_lens_z_mm=-34,
                  pump_exit_waist_radius_um=140, pump_farfield_half_angle_mrad=5.5)
    receipts = {k: {"value": v, "source": "historical_label"} for k, v in inputs.items()}
    design = solve_design(receipts, [], assumption_policy="evidence_only")
    project = saved_geometry(design, 123, 4, target="coupled_layout")
    case = ExperimentCase(title="同轴共用晶体", goal="调整观察目标", cavity_type="linear", parameters={"design_facts": receipts})
    db.add(case)
    db.commit()
    first = save_project(db, case.id, SaveProject(project=project, change_note="导入扫描的选定行"))
    return case, first


def test_selected_coupled_geometry_drives_agent_and_public_solver(client, db, monkeypatch):
    case, first = coupled_bench(db)
    body = deepcopy(first["project"])
    lens = body["incident_paths"][0]["elements"]
    lens[0]["length_mm"] += 4.5
    lens[2]["length_mm"] -= 4.5
    edited = save_project(db, case.id, SaveProject(project=body, change_note="移动准直镜",
                                                  base_revision_id=first["revision_id"]))
    task = AgentTask(case_id=case.id, mode="workbench", status="completed", goal="试算半径 121 µm")
    db.add(task)
    db.commit()

    class GoalOnly:
        async def extract_design_facts(self, context, images):
            return {"facts": [{"key": "target_mode_radius_um", "value": 121, "source": "message",
                               "quote": "121 µm"}]}

    for selected, collimator in ((first, -101), (edited, -96.5)):
        result = asyncio.run(execute("design_linear_laser", LinearDesign(), db=db, case=case, task=task,
                                     provider=GoalOnly(), session_id=None, attachment_ids=[],
                                     working={"revision_id": selected["revision_id"]}))
        design = result["engineering_design"]
        assert design["project_revision_id"] == selected["revision_id"]
        assert design["facts"]["collimator_z_mm"]["source"] == f"optical_project:{selected['revision_id']}"
        assert design["resolved_inputs"]["collimator_z_mm"] == pytest.approx(collimator)
        assert design["resolved_inputs"]["source_z_mm"] == pytest.approx(-150)
        assert design["resolved_inputs"]["pump_farfield_half_angle_mrad"] == pytest.approx(5.5)
        assert design["resolved_inputs"]["target_mode_radius_um"] == 121
        assert design["evidence_assessment"]["pump_imaging"]["status"] == "modeled"
        rows = design["evidence_assessment"]["cold_cavity"]["rows"]
        assert len(rows) == 1 and rows[0]["output_gap_mm"] == 43
    db.refresh(case)
    assert case.parameters["design_facts"]["collimator_z_mm"] == {"value": -101, "source": "historical_label"}

    monkeypatch.setattr(service, "SessionLocal", sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    monkeypatch.setattr(service, "get_ai_provider", GoalOnly)
    response = client.post(f"/api/cases/{case.id}/design/runs", json={"message": task.goal, "assumption_policy": "evidence_only",
                                                                     "constraints": {"focus_lens_z_mm": 5.25}})
    assert response.status_code == 202
    revision = client.get(f"/api/cases/{case.id}/design").json()["revisions"][0]["content"]["engineering_design"]
    assert revision["project_revision_id"] == edited["revision_id"]
    assert revision["resolved_inputs"]["collimator_z_mm"] == pytest.approx(-96.5)
    assert revision["resolved_inputs"]["focus_lens_z_mm"] == pytest.approx(5.25)
    assert revision["evidence_assessment"]["pump_imaging"]["status"] == "modeled"


@pytest.mark.parametrize("change", ["extra_lens", "second_source", "off_axis", "removed_path", "removed_source", "nominal"])
def test_project_projection_never_backfills_stale_beam_or_skips_current_optics(db, change):
    from app.design.projection import coupled_inputs
    case, saved = coupled_bench(db)
    project = deepcopy(saved["project"])
    path = project["incident_paths"][0]
    if change == "extra_lens":
        path["elements"].append({"kind": "lens", "name": "中继镜", "focal_mm": 35})
    elif change == "second_source":
        project["incident_paths"].append({**path, "uid": "additional-source"})
    elif change == "off_axis":
        path["launch_ray"]["s_angle_mrad"] = 2.5
    elif change == "removed_path":
        project["incident_paths"] = []
    elif change == "removed_source":
        path["tangential"]["radius_mm"] = None
    else:
        project["component_context"]["design_origin"]["assumption_policy"] = "exploratory"
        project["component_context"]["design_coordinates"] = {"source_basis": "nominal_source_scenario"}
    if change in {"nominal", "removed_source"}:
        actual = coupled_inputs(project, saved["revision_id"], case.parameters["design_facts"])
        assert "pump_exit_waist_radius_um" not in actual and "pump_farfield_half_angle_mrad" not in actual
    else:
        with pytest.raises(ValueError):
            coupled_inputs(project, saved["revision_id"], case.parameters["design_facts"])

"""Agent and visual scans share saved models, artifacts and scope boundaries."""

import asyncio
from copy import deepcopy

import pytest

from app.models import AgentTask, ExperimentCase, GeneratedContent
from app.workbench.analyses import ProjectScan, ProjectScan2D, run_scan
from app.workbench.projects import SaveProject, read_project, save_project
from app.workbench.tools import execute


def setup_project(db):
    case = ExperimentCase(title="工程扫描", goal="扫描几何与稳定性", cavity_type="custom", parameters={})
    db.add(case)
    db.commit()
    saved = save_project(
        db,
        case.id,
        SaveProject(
            project={
                "title": "真实腔模型",
                "system": {
                    "mode": "standing_wave",
                    "wavelength_nm": 1064,
                    "elements": [
                        {"name": "M1", "kind": "mirror", "radius_mm": 200},
                        {"name": "前间隙", "kind": "space", "length_mm": 3},
                        {"name": "后间隙", "kind": "space", "length_mm": 30},
                        {"name": "M2", "kind": "mirror"},
                    ],
                },
                "component_context": {"scope_warnings": ["包含吸收体；冷腔不计算脉冲动态。"]},
            },
            change_note="初始工程",
        ),
    )
    task = AgentTask(case_id=case.id, goal="扫描当前工程", mode="workbench", status="running")
    db.add(task)
    db.commit()
    return case, saved, task


def map_config(saved):
    return {
        "system": saved["project"]["system"],
        "x": {"element_index": 1, "parameter": "length_mm", "start": 1, "stop": 5, "count": 3},
        "y": {"element_index": 2, "parameter": "length_mm", "start": 10, "stop": 50, "count": 3},
    }


def test_agent_scan_is_visible_with_same_artifact_contract_as_frontend(client, db):
    case, saved, task = setup_project(db)
    args = ProjectScan2D(**map_config(saved), project_revision_id=saved["revision_id"])
    output = asyncio.run(
        execute(
            "scan_optical_system_2d",
            args,
            db=db,
            case=case,
            task=task,
            provider=None,
            session_id=None,
            attachment_ids=[],
        )
    )
    assert output["project_revision_id"] == saved["revision_id"]
    assert output["grid_points"] == 9
    assert "吸收体" in output["scope_warnings"][0]
    state = client.get(f"/api/cases/{case.id}/optical-project").json()
    assert state["scan"]["artifact_id"] == output["artifact_id"]
    api = client.post(f"/api/cases/{case.id}/optical-project/{saved['revision_id']}/scan-2d", json=map_config(saved))
    assert api.status_code == 200, api.text
    assert api.json()["maps"] == output["maps"]
    assert api.json()["artifact_id"] != output["artifact_id"]
    canvas = client.get(f"/api/cases/{case.id}/optical-project", params={"latest_scan_only":True}).json()
    assert canvas["scan"]["artifact_id"] == api.json()["artifact_id"] and "scans" not in canvas
    single = asyncio.run(
        execute(
            "scan_optical_system",
            ProjectScan(
                system=saved["project"]["system"], element_index=2, parameter="length_mm", start=10, stop=210, count=3
            ),
            db=db,
            case=case,
            task=task,
            provider=None,
            session_id=None,
            attachment_ids=[],
        )
    )
    assert single["project_revision_id"] == saved["revision_id"]
    state = client.get(f"/api/cases/{case.id}/optical-project").json()
    assert len(state["scans"]) == 3
    assert state["scans"][0]["dimensions"] == 1
    assert state["scan"]["artifact_id"] == api.json()["artifact_id"]
    assert client.get(f"/api/cases/{case.id}/optical-project?latest_scan_only=true").json()["scan"] == state["scan"]
    artifact = db.get(GeneratedContent, single["artifact_id"])
    assert artifact.content["configuration"]["system"] == saved["project"]["system"]
    assert artifact.content["task_id"] == task.id


def test_scan_cannot_attach_different_geometry_and_proposal_does_not_install(client, db):
    case, saved, task = setup_project(db)
    changed = deepcopy(map_config(saved))
    changed["system"]["elements"][1]["length_mm"] = 8
    denied = client.post(f"/api/cases/{case.id}/optical-project/{saved['revision_id']}/scan-2d", json=changed)
    assert denied.status_code == 422
    assert db.query(GeneratedContent).filter_by(content_type="optical_scan").count() == 0
    output = asyncio.run(run_scan(db, case.id, ProjectScan2D(**changed), task_id=task.id))
    assert output["project_revision_id"] is None
    assert any("探索模型" in w for w in output["scope_warnings"])
    assert read_project(db, case.id)["revision_id"] == saved["revision_id"]
    assert client.get(f"/api/cases/{case.id}/optical-project").json()["scan"] is None


def test_cancelled_scan_never_saves_an_artifact(db):
    case, saved, task = setup_project(db)
    task.status = "cancelled"
    db.commit()
    with pytest.raises(ValueError, match="stopped"):
        asyncio.run(run_scan(db, case.id, ProjectScan2D(**map_config(saved)), task_id=task.id))
    assert db.query(GeneratedContent).filter_by(content_type="optical_scan").count() == 0

"""Calibrated coordinate registration, transfer to other specimens, and frozen-source queries."""

import math

import numpy as np
import pytest
from pydantic import ValidationError

from app.models import GeneratedContent
from app.workbench.transverse_registration import RegistrationFit, TransverseRegistration, fit_registration


def calibration(observed, reference, **options):
    x, y = np.asarray(observed).T
    u, v = np.asarray(reference).T
    return RegistrationFit(
        observed_x_mm=x,
        observed_y_mm=y,
        reference_x_mm=u,
        reference_y_mm=v,
        basis="Independent survey of the same transverse plane.",
        **options,
    )


@pytest.mark.parametrize("reverse", [False, True])
def test_fit_and_holdout_use_independent_points_with_unknown_handedness(reverse):
    angle = 31 * math.pi / 180
    observed = [(0, 0), (2, 1), (-1, 3), (5, -2), (4, 2)]

    def survey(x, y):
        y = -y if reverse else y
        return (13 + math.cos(angle) * x - math.sin(angle) * y, -4 + math.sin(angle) * x + math.cos(angle) * y)

    targets = [survey(*point) for point in observed]
    x, y = targets[-1]
    targets[-1] = (x + 0.003, y - 0.004)
    passed = fit_registration(calibration(observed, targets, check_rows=[5], max_error_mm=0.006))
    exploratory = fit_registration(calibration(observed, targets, check_rows=[5]))
    assert exploratory["registration"] and any("未设位置误差限值" in note for note in exploratory["warnings"])
    transform = TransverseRegistration(**passed["registration"])
    assert transform.flip_y == reverse
    assert transform.rotation_deg == pytest.approx(31)
    assert transform.map_point(3, 2) == pytest.approx(survey(3, 2))
    assert passed["diagnostics"]["fit_max_mm"] < 1e-12
    assert passed["diagnostics"]["check_rms_mm"] == pytest.approx(0.005)
    assert passed["rows"][-1]["dx_mm"] == pytest.approx(0.003)
    assert passed["rows"][-1]["dy_mm"] == pytest.approx(-0.004)
    invalid = {**passed["registration"], "fit_evidence": {**passed["diagnostics"], "check_new_location_count": 2}}
    with pytest.raises(ValidationError, match="新检验位置"):
        TransverseRegistration(**invalid)
    rejected = fit_registration(calibration(observed, targets, check_rows=[5], max_error_mm=0.004))
    assert "registration" not in rejected and rejected["candidate"]["flip_y"] == reverse


def test_holdout_cannot_select_hand_or_fix_nonidentifiable_rotation():
    obs = [(0, 0), (2, 0), (0, 1)]
    ref = [(0, 4), (2, 4), (0, 3)]
    assert fit_registration(calibration(obs, ref, check_rows=[3]))["status"] == "needs_input"
    good = fit_registration(calibration(obs, ref, check_rows=[3], handedness="reverse"))
    assert good["diagnostics"]["check_max_mm"] == 0
    assert "registration" not in fit_registration(calibration([(1, 2)] * 3, ref))
    ring = [(1, 0), (0, 1), (-1, 0), (0, -1)]
    assert fit_registration(calibration(ring, ring, handedness="reverse"))["status"] == "needs_input"
    assert fit_registration(calibration(ring, ring, max_error_mm=3))["status"] == "needs_input"
    assert fit_registration(calibration(ring, ring, max_error_mm=0.01))["registration"]["flip_y"] is False


def test_scale_change_is_diagnosed_and_repeated_target_only_checks_repeatability():
    obs = [(0, 0), (1, 0), (0, 3), (1, 0)]
    ref = [(8, 9), (10, 9), (8, 15), (10, 9)]
    assert "registration" not in fit_registration(calibration(obs, ref, max_error_mm=0.02))
    move = [(x + 1e8, y - 2e8) for x, y in obs]
    report = fit_registration(calibration(move, obs, check_rows=[4], max_error_mm=1e-5))
    assert report["diagnostics"]["check_new_location_count"] == 0
    assert "重复性" in report["warnings"][0]
    assert TransverseRegistration(**report["registration"]).map_point(*move[1]) == pytest.approx(obs[1])


@pytest.mark.parametrize("rows", [[0], [1, 1], [5], [1, 2, 3]])
def test_reject_bad_check_rows(rows):
    points = [(0, 0), (1, 0), (0, 1), (3, 4)]
    with pytest.raises(ValidationError):
        calibration(points, points, check_rows=rows)
    with pytest.raises(ValidationError):
        calibration(points, points[:-1])


def test_history_filters_by_method_and_exact_frozen_binding_before_pagination(client, db):
    first, other = [client.post("/api/cases", json={"title": name}).json()["id"] for name in ("A", "B")]
    port = "station-center-v1/a%2Fb/primary/center"
    method = "compare_transverse_centers"

    def save(owner=first, revision=91, key="reference", station=port, name=method):
        row = GeneratedContent(
            case_id=owner,
            content_type="scientific_study",
            content={
                "definition": {"name": name, "version": "1", "title": "Same displayed title"},
                "connections": {key: {"revision_id": revision, "port": station}},
                "available_outputs": {},
            },
        )
        db.add(row)
        db.flush()
        return row.id

    ids = [save() for _ in range(27)]
    for _ in range(28):
        save(revision=92)
    save(owner=other)
    save(station="station-center-v1/other/primary/center")
    save(key="observed")
    save(name="another_method")
    db.commit()
    url = f"/api/cases/{first}/studies/runs"
    filters = dict(input_key="reference", port=port, name=method, revision_id=91)
    top = client.get(url, params=filters).json()
    assert [row["id"] for row in top["items"]] == ids[::-1][:25]
    last = client.get(url, params={**filters, "before_id": top["next_before_id"]}).json()
    assert [row["id"] for row in last["items"]] == ids[1::-1]
    assert last["next_before_id"] is None
    assert client.get(url, params={"input_key": "reference", "port": port}).status_code == 422

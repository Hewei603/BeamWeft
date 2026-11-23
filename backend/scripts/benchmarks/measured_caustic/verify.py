"""Compare both camera axes with the registered BeamWeft study using an isolated project API."""

import argparse
import csv
import json
import logging
import math
import os
import sys
import tempfile
from pathlib import Path

from reference import DEFAULT_CACHE, write_series


def check(condition, explanation):
    if not condition:
        raise AssertionError(explanation)


def compare(value, reference, tolerance, label):
    check(
        math.isclose(value, reference, abs_tol=tolerance, rel_tol=0),
        f"{label}: BeamWeft={value:.12g}, independent={reference:.12g}, absolute tolerance={tolerance:.6g}",
    )


def response_json(response):
    check(
        response.is_success,
        f"{response.request.method} {response.request.url.path}: HTTP {response.status_code}: {response.text[:1500]}",
    )
    return response.json()


def check_axis(result, reference, input_rows):
    metrics = {
        "waist_z_mm": (result["waist_z_mm"], reference["waist_z_mm"], 1e-4),
        "waist_radius_mm": (result["waist_radius_mm"], reference["waist_diameter_um"] / 2000, 1e-7),
        "divergence_half_angle_rad": (
            result["divergence_half_angle_rad"],
            reference["divergence_full_mrad"] / 2000,
            1e-8,
        ),
        "m2": (result["m2"], reference["M2_unconstrained"], 1e-5),
        "station_rms_radius_mm": (
            result["holdout"]["station_rms_radius_mm"],
            reference["holdout_radius_rmse_um"] / 1000,
            1e-7,
        ),
    }
    for label, (actual, expected, tolerance) in metrics.items():
        compare(actual, expected, tolerance, label)
    predicted = reference["predictions"]
    positions = [p["z_mm"] for p in predicted]
    training = [float(row["z_mm"]) for row in input_rows if float(row["z_mm"]) not in positions]
    check(result["data"]["z_mm"] == training, "Fitted stations disagree with the declared split.")
    check(result["data"]["held_z_mm"] == positions, "Plotted held-out stations changed.")
    for point, ref in zip(result["holdout"]["points"], predicted, strict=True):
        check(
            point["z_mm"] == ref["z_mm"], "Held-out prediction and observation refer to different stations."
        )
        raw = input_rows[point["input_index"] - 1]
        check(
            raw["acquisition"] == point["acquisition_id"] and float(raw["z_mm"]) == point["z_mm"],
            "Held-out point does not refer to its input frame.",
        )
        compare(point["radius_mm"], ref["observed_diameter_um"] / 2000, 1e-10, "observed radius / mm")
        compare(
            point["predicted_radius_mm"], ref["predicted_diameter_um"] / 2000, 1e-7, "predicted radius / mm"
        )
        compare(
            point["residual_mm"],
            -ref["prediction_minus_observation_um"] / 2000,
            1e-7,
            "measured minus predicted / mm",
        )
    if reference["sub_diffraction_fit"]:
        check(
            result["m2"] < 1 and any("小于 1" in msg for msg in result["warnings"]),
            "The sub-diffraction fit should be reported along with its measurement-quality warning.",
        )
    return {
        label: {"product": actual, "independent": expected, "abs_tol": tol}
        for label, (actual, expected, tol) in metrics.items()
    }


def run_series(client, cache, series, independent):
    name = "fit_beam_caustic"
    case_id = response_json(client.post("/api/cases", json={"title": f"Prahl {series.upper()} scan"}))["id"]
    base = f"/api/cases/{case_id}"
    method = response_json(client.get(f"{base}/studies/catalog/{name}"))
    path = cache / f"prahl-{series}.csv"
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    with path.open("rb") as stream:
        file_id = response_json(
            client.post(f"{base}/attachments", files={"file": (path.name, stream, "text/csv")})
        )["id"]
    view = response_json(
        client.post(f"{base}/measurement-view", json={"attachment_id": file_id, "x_column": 1, "y_column": 2})
    )
    checksum = view["source"]["sha256"]
    check(
        checksum == independent["csv_sha256"], "The uploaded original differs from the independent reduction."
    )
    original = {"table": view["selection"], "sha256": checksum}
    held_stations = independent["holdout_z_mm"]
    check(
        held_stations == {"k": [300, 490, 550, 650], "sb": [251, 292]}[series],
        "The preregistered split changed.",
    )
    results = {}
    for axis, column in [("camera_x", 2), ("camera_y", 3)]:
        ref = independent[axis]
        bindings = {
            "z_mm": {**original, "column": 1, "unit": "mm"},
            "radius_mm": {
                **original,
                "column": column,
                "unit": "µm",
                "calibration": {
                    "gain": 0.5,
                    "offset": 0,
                    "note": "Fixed camera-axis D4σ diameter to second-moment radius.",
                },
            },
        }
        payload = {
            "provider": method["provider"],
            "version": method["version"],
            "connections": bindings,
            "inputs": {
                "loss": "radius",
                "holdout_positions_mm": held_stations,
                "acquisition_ids": [row["acquisition"] for row in rows],
                "wavelength_nm": independent["wavelength_nm"],
                "medium_n": 1,
                "data_source": f"Prahl {series}, {axis}, D4σ full diameter from measured frames.",
            },
        }
        response = client.post(f"{base}/studies/run/{name}", json=payload)
        if series == "sb":
            check(
                ref["training_count"] == 4 and response.status_code == 422,
                f"Expected four-station fit rejection; got HTTP {response.status_code}: {response.text[:250]}",
            )
            detail = response.json()["detail"]
            check(
                "4 个不同位置" in detail and "至少需要 5 个" in detail,
                f"Incorrect rejection reason: {detail}",
            )
            results[axis] = {"http_status": 422, "training_stations": 4, "detail": detail}
            print(f"{series}/{axis}: HTTP 422, four training stations, minimum five.")
        else:
            data = response_json(response)
            metrics = check_axis(data, ref, rows)
            receipt = response_json(client.get(f"{base}/studies/runs/{data['study_record']['id']}"))[
                "file_evidence"
            ]
            check(
                len(receipt) == 1
                and receipt[0]["source"]["sha256"] == checksum
                and receipt[0]["row_numbers"] == list(range(2, len(rows) + 2)),
                "Original table row receipt mismatch.",
            )
            results[axis] = {
                "training_stations": ref["training_count"],
                "held_stations": held_stations,
                "metrics": metrics,
            }
            print(
                f"{series}/{axis}: PASS, M^2={data['m2']:.8f}, held-out radius RMSE={metrics['station_rms_radius_mm']['product']:.9f} mm"
            )
    runs = response_json(client.get(f"{base}/studies/runs"))["items"]
    check(len(runs) == (0 if series == "sb" else 2), f"Unexpected number of saved fits in {series}.")
    return {"study": name, "provider": method["provider"], "version": method["version"], **results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--offline", action="store_true", help="Read only the 22 verified frames already in the cache."
    )
    parser.add_argument(
        "--report", type=Path, help="Save independent and product results with tolerances as JSON."
    )
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    reference = {key: write_series(args.cache, key, offline=args.offline) for key in ("k", "sb")}
    with tempfile.TemporaryDirectory(prefix="beamweft-caustic-validation-") as folder:
        lab = Path(folder)
        os.environ.update(
            LASERCLAW_NO_DOTENV="1",
            DATABASE_URL="sqlite:///" + (lab / "case.db").as_posix(),
            UPLOAD_DIR=str(lab / "uploads"),
            VECTOR_STORE_DIR=str(lab / "vectors"),
            AGENT_SETTINGS_FILE=str(lab / "settings.json"),
            MIGRATE_LEGACY_ON_START="false",
            AUTO_CREATE_TABLES="true",
            REQUIRE_AUTH="false",
            AI_PROVIDER="mock",
            EMBEDDING_PROVIDER="local",
            RETRIEVAL_BACKEND="sql_json",
        )
        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from app.main import app, engine
        from fastapi.testclient import TestClient

        logging.getLogger("httpx").setLevel(logging.WARNING)
        try:
            with TestClient(app) as client:
                product = {key: run_series(client, args.cache, key, ref) for key, ref in reference.items()}
        finally:
            engine.dispose()
    if args.report:
        args.report.write_text(
            json.dumps({"independent": reference, "product": product}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


if __name__ == "__main__":
    main()

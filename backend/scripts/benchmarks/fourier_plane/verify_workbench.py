"""Verify the real study and its native-grid download against the frozen camera experiment."""

import argparse
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from time import perf_counter

import numpy as np

from archive import CACHE, digest, verified_archive
from benchmark import INNER, corr, observed, project, rms


def checked(response):
    response.raise_for_status()
    return response.json()


def verify(client, cache, package=None, figure=None):
    freeze = json.loads((cache / "freeze.json").read_text(encoding="utf-8"))
    score = json.loads((cache / "score.json").read_text(encoding="utf-8"))
    if digest(cache / "freeze.json") != score["freeze_sha256"]:
        raise ValueError("The measured score belongs to a different freeze receipt.")
    for filename, expected in {**freeze["files_sha256"], "capture_3.npy": score["capture_sha256"]}.items():
        if digest(cache / filename) != expected:
            raise ValueError(f"Changed benchmark original or frozen prediction: {filename}")
    root = "/api/cases/" + str(checked(client.post("/api/cases", json={"title": "剑桥光学台架 · 更换入射光场"}))["id"])
    name = "lens_focal_intensity"
    method = checked(client.get(f"{root}/studies/catalog/{name}"))
    input_refs = {}

    def original(filename):
        if filename not in input_refs:
            with (cache / filename).open("rb") as stream:
                source = checked(client.post(root + "/attachments", files={"file": (filename, stream)}))
            input_refs[filename] = {"attachment_id": source["id"], "sha256": digest(cache / filename)}
        return input_refs[filename]

    def run(inputs):
        start = perf_counter()
        result = checked(
            client.post(
                f"{root}/studies/run/{name}",
                json={
                    "provider": method["provider"],
                    "version": method["version"],
                    "inputs": {
                        "wavelength_nm": 658,
                        "focal_length_mm": 60,
                        "pitch_x_um": 8,
                        "pitch_y_um": 8,
                        "medium_n": 1,
                        "row_direction": "positive_y",
                        "basis": "Wojcik et al., frozen fixed-camera calibration.",
                        **inputs,
                    },
                },
            )
        )
        computed = perf_counter()
        record_id = result["study_record"]["id"]
        reply = client.get(f"{root}/studies/runs/{record_id}/native-grid")
        reply.raise_for_status()
        exported = perf_counter()
        with np.load(BytesIO(reply.content), allow_pickle=False) as grid:
            cells = grid["cell_integrals"]
            np.testing.assert_equal(cells.shape, (1200, 1920))
            np.testing.assert_equal(grid["grid_sha256"].item(), result["native_grid_sha256"])
            center_x = (grid["x_edges_mm"][:-1] + grid["x_edges_mm"][1:]) / 2
            center_y = (grid["y_edges_mm"][:-1] + grid["y_edges_mm"][1:]) / 2
            row, col = np.unravel_index(cells.argmax(), cells.shape)
            peak = [center_x[col], center_y[row]]
            np.testing.assert_allclose(peak, [result["peak_x_mm"], result["peak_y_mm"]], atol=1e-12)
            np.testing.assert_allclose(cells.sum(), result["input_integral"], rtol=1e-12)
            provenance = json.loads(grid["provenance_json"].item())
        recorded = result["study_record"]["solver_receipt"]
        assert provenance["verified_against_saved_grid"]
        assert provenance["saved_solver_receipt"] == provenance["verification_solver_receipt"] == recorded
        return project(cells, freeze["transform"]), {
            "id": record_id,
            "method": name,
            "version": method["version"],
            "native_grid_sha256": result["native_grid_sha256"],
            "solver_sha256": recorded["sha256"],
            "shape": list(cells.shape),
            "peak_xy_mm": peak,
            "parseval_relative_error": result["parseval_relative_error"],
            "rectangle_fraction": float(cells[595:822, 650:915].sum() / cells.sum()),
            "originals": provenance["originals"],
            "post_compute_seconds": computed - start,
            "get_export_seconds": exported - computed,
            "download_bytes": len(reply.content),
        }

    captures = {}
    records = {}
    for setting in (0, 3):
        phase = original(f"phase_{setting}_rad.npy")
        image, info = run(
            {
                "complex_field": original("incident_pupil.npy"),
                "phase_layers": [{"image": phase, "scale_rad_per_unit": 1}],
            }
        )
        frozen_prediction = np.load(cache / f"prediction_{setting}.npy", allow_pickle=False)
        np.testing.assert_allclose(image, frozen_prediction, atol=2e-10, rtol=2e-10)
        independent = freeze["native_metrics"][str(setting)]
        np.testing.assert_allclose(info["peak_xy_mm"], independent["peak_xy_mm"], atol=1e-12)
        np.testing.assert_allclose(
            info["rectangle_fraction"],
            independent["energy_fraction_in_native_rectangle_cols650_915_rows595_822_half_open"],
            atol=1e-12,
        )
        captures[setting], records[str(setting)] = image, info

    # Use the archived drive counts and measured amplitude/phase directly as a separate operator would.
    with verified_archive(cache, offline=True) as deposit:
        for archive_path, expected in freeze["raw_member_sha256"].items():
            filename = Path(archive_path).name
            if filename in {"amp_wavefront.npy", "phase_wavefront.npy", "holo_3.npy"} or package:
                raw_bytes = deposit.read(archive_path)
                if hashlib.sha256(raw_bytes).hexdigest() != expected:
                    raise ValueError(f"Raw instrument array differs from the pinned deposit: {filename}")
                uploaded = checked(client.post(root + "/attachments", files={"file": (filename, raw_bytes)}))
                input_refs[filename] = {"attachment_id": uploaded["id"], "sha256": expected}
    raw, raw_info = run(
        {
            "amplitude": original("amp_wavefront.npy"),
            "phase": original("phase_wavefront.npy"),
            "phase_scale_rad_per_unit": freeze["source_phase_sign"],
            "phase_layers": [{"image": original("holo_3.npy"), "scale_rad_per_unit": 2 * np.pi / 1024}],
        }
    )
    np.testing.assert_allclose(raw, captures[3], atol=5e-6, rtol=5e-6)
    truth = observed(np.load(cache / "capture_3.npy", allow_pickle=False))
    baseline = np.load(cache / "training_observation.npy", allow_pickle=False)
    actual_change = truth - baseline
    model_change = captures[3] - captures[0]
    relative_gain = 1 - rms((actual_change - model_change)[INNER]) / rms(actual_change[INNER])
    shape_score = corr(truth, captures[3])
    np.testing.assert_allclose(
        [shape_score, relative_gain], [score["shape_pearson"], score["change_rmse_improvement_fraction"]], atol=1e-9
    )
    if shape_score < 0.80 or relative_gain < 0.05:
        raise ValueError("The product prediction did not meet the two preregistered criteria.")
    report = {
        "freeze_sha256": score["freeze_sha256"],
        "settings": records,
        "direct_from_instrument_arrays": raw_info,
        "direct_vs_frozen_max_abs": float(np.max(np.abs(raw - captures[3]))),
        "held_shape_pearson": shape_score,
        "change_rmse_improvement_fraction": relative_gain,
        "baseline_shape_pearson": corr(truth, baseline),
    }
    if figure:
        from plot import plot_comparison

        plot_comparison([captures[0], captures[3]], [baseline, truth], shape_score, relative_gain, figure)
    if package:
        camera_method = checked(client.get(root + "/studies/catalog/measure_image_moments"))
        report["camera_records"] = {}
        for setting in (0, 3):
            measurement = checked(
                client.post(
                    root + "/studies/run/measure_image_moments",
                    json={
                        "provider": camera_method["provider"],
                        "version": camera_method["version"],
                        "inputs": {
                            "image": original(f"capture_{setting}.npy"),
                            "window": [430, 523, 276, 276],
                            "pitch_x_um": 3.45,
                            "pitch_y_um": 3.45,
                            "response": "assumed_linear",
                            "background_level": 3,
                            "wavelength_nm": 658,
                            "basis": "Original CMOS array. 3 DN fixed training-frame background. Conditional camera linearity; absolute power uncalibrated.",
                            "plane": "Wojcik et al., fixed camera, f6cm_exact series.",
                        },
                    },
                )
            )
            report["camera_records"][str(setting)] = measurement["study_record"]["id"]
        guide = (
            "# 打开这个工程\n\n"
            f"进入「分析记录」。计算 #{records['0']['id']} 和 #{records['3']['id']} 分别保存原设置和更换输入后的预测，"
            f"计算 #{raw_info['id']} 从仪器原始驱动计数和测得的波前直接建立后一份预测。"
            "在预测图中框选 x 约 −0.82 至 −0.12 mm、y 约 −0.02 至 0.92 mm，可查看辐条结构。\n\n"
            f"计算 #{report['camera_records']['0']}、#{report['camera_records']['3']} 保存两个设置的相机测量。"
            "相机图的像素坐标以传感器原点为基准。对照图已按设置 0 冻结的配准和归一化处理。\n\n"
        )
        documents = [
            ("freeze.json", (cache / "freeze.json").read_bytes()),
            ("score.json", (cache / "score.json").read_bytes()),
            ("实验说明.md", guide.encode("utf-8") + Path(__file__).with_name("README.md").read_bytes()),
        ]
        if figure:
            documents.append(("预测与相机对照.png", figure.read_bytes()))
        for filename, data in documents:
            checked(
                client.post(
                    root + "/attachments",
                    files={"file": (filename, data)},
                    data={"relative_path": "benchmark/" + filename},
                )
            )
        payload = client.get(root + "/project-file")
        payload.raise_for_status()
        package.write_bytes(payload.content)
        report["package_bytes"] = package.stat().st_size
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--figure", type=Path)
    parser.add_argument(
        "--package", type=Path, help="Save a portable example project and verify the held native grid after reopening."
    )
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="beamweft-fourier-api-") as temp:
        lab = Path(temp)
        os.environ.update(
            LASERCLAW_NO_DOTENV="1",
            DATABASE_URL="sqlite:///" + (lab / "lab.db").as_posix(),
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
                report = verify(client, args.cache, args.package, args.figure)
        finally:
            engine.dispose()
        if args.package:
            from sqlalchemy import create_engine
            from sqlalchemy.engine import URL
            from app.open_case import open_package
            from app.workbench.grid_export import InspectNativeGrid
            from app.workbench.studies import inspect_native_grid
            from app.workbench.tools import STUDIES

            folder, metadata = open_package(args.package, lab / "reopened")
            local = create_engine(URL.create("sqlite", database=str(folder / "laserclaw.db")))
            try:
                saved = report["settings"]["3"]
                check = inspect_native_grid(
                    local,
                    metadata["case_id"],
                    InspectNativeGrid(run_id=saved["id"], window=(650, 595, 265, 227)),
                    STUDIES,
                )
                if check["grid_sha256"] != saved["native_grid_sha256"]:
                    raise ValueError("The reopened project produced a different native grid.")
                if check["saved_solver_receipt"]["sha256"] != saved["solver_sha256"]:
                    raise ValueError("The reopened project lost the original solver receipt.")
                report["reopened"] = {
                    "verified_grid_sha256": check["grid_sha256"],
                    "saved_solver_sha256": check["saved_solver_receipt"]["sha256"],
                    "verification_solver_sha256": check["verification_solver_receipt"]["sha256"],
                    "originals": check["originals"],
                    "native_roi_integral": check["integral"],
                }
            finally:
                local.dispose()
    if args.report:
        args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(
        "Held-out Pearson =",
        report["held_shape_pearson"],
        "; improvement =",
        report["change_rmse_improvement_fraction"],
    )


if __name__ == "__main__":
    main()

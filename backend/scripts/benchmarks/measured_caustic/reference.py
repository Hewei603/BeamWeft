"""Download pinned public camera frames into a user cache; recover fixed-axis D4σ widths and fit withheld planes."""

import argparse
import csv
import hashlib
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import tempfile

import numpy as np
from PIL import Image
from scipy.optimize import least_squares


MANIFEST = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))
ORIGIN = f"https://raw.githubusercontent.com/{MANIFEST['repository']}/{MANIFEST['commit']}/{MANIFEST['directory']}/"
DEFAULT_CACHE = Path(tempfile.gettempdir()) / "beamweft-measured-caustic"


def read_frame(cache, record, settings, offline):
    filename = record["file"]
    path = cache / filename

    def verified_bytes(source):
        data = source.read_bytes()
        oid = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if oid != record["git_blob_sha1"] or hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError(
                f"Frame checksum mismatch: {source}. Move the suspect file aside and download again."
            )
        return data

    if path.exists():
        data = verified_bytes(path)
    else:
        if offline:
            raise FileNotFoundError(
                f"Missing cached frame {path}. Run once without --offline to download it."
            )
        # curl also works with the Windows system proxy in the native desktop environment.
        with tempfile.TemporaryDirectory(dir=cache) as temp:
            incoming = Path(temp) / filename
            subprocess.run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--location",
                    "--retry",
                    "2",
                    "--max-time",
                    "60",
                    "--output",
                    str(incoming),
                    ORIGIN + filename,
                ],
                check=True,
            )
            data = verified_bytes(incoming)
            incoming.replace(path)
    with Image.open(io.BytesIO(data)) as image:
        frame = np.array(image)
    frame = frame[settings["crop_top_rows"] :, :] >> settings["right_shift_bits"]
    adc_max = (1 << settings["adc_bits"]) - 1
    return frame, {
        "sha256": record["sha256"],
        "detector_max_count": int(frame.max()),
        "adc_ceiling_pixels": int(np.sum(frame == adc_max)),
    }


def diameters(frame):
    """Compute 4σ in two fixed camera axes, retaining the axis labels even if their ordering changes."""
    h, w = frame.shape
    ch, cw = int(0.035 * h), int(0.035 * w)
    corners = np.concatenate(
        [r.ravel() for r in (frame[:ch, :cw], frame[:ch, -cw:], frame[-ch:, :cw], frame[-ch:, -cw:])]
    )
    background = frame[frame <= corners.mean() + 3 * corners.std()]
    net = frame.astype(float) - background.mean()
    net[net < 3 * background.std()] = 0.0
    previous = None
    x, y = np.arange(w), np.arange(h)
    crop = net
    for _ in range(25):
        mx, my = crop.sum(axis=0), crop.sum(axis=1)
        total = mx.sum()
        if total <= 0:
            raise ValueError("No signal left in the measurement rectangle.")
        xc, yc = x @ mx / total, y @ my / total
        dx = 4 * math.sqrt((x - xc) ** 2 @ mx / total)
        dy = 4 * math.sqrt((y - yc) ** 2 @ my / total)
        state = np.array([xc, yc, dx, dy])
        if previous is not None and np.max(np.abs(state - previous)) < 1:
            return dx * MANIFEST["pixel_pitch_um"], dy * MANIFEST["pixel_pitch_um"]
        previous = state
        x = np.arange(max(0, math.ceil(xc - 1.5 * dx)), min(w, math.floor(xc + 1.5 * dx) + 1))
        y = np.arange(max(0, math.ceil(yc - 1.5 * dy)), min(h, math.floor(yc + 1.5 * dy) + 1))
        crop = net[y[0] : y[-1] + 1, x[0] : x[-1] + 1]
    raise ValueError("The integration rectangle did not converge.")


def solve(rows, axis):
    training = [row for row in rows if not row["heldout"]]
    test = [row for row in rows if row["heldout"]]
    z, d = np.array([(r["z_mm"], r[axis] / 1000) for r in training]).T
    initial = [d.min(), z[d.argmin()], max(0.0001, (d.max() - d.min()) / np.ptp(z))]

    def diameter(v, positions):
        return np.hypot(v[0], v[2] * (positions - v[1]))

    result = least_squares(
        lambda v: diameter(v, z) - d,
        initial,
        bounds=([1e-8, -np.inf, 1e-10], np.inf),
        xtol=1e-14,
        ftol=1e-14,
        gtol=1e-14,
        x_scale="jac",
    )
    if not result.success:
        raise RuntimeError(result.message)
    d0, z0, theta = result.x
    m2 = np.pi * d0 * theta / (4 * MANIFEST["wavelength_nm"] * 1e-6)
    observed = np.array([r[axis] for r in test])
    prediction = diameter(result.x, np.array([r["z_mm"] for r in test])) * 1000
    delta = prediction - observed
    return {
        "training_count": len(training),
        "holdout_count": len(test),
        "waist_diameter_um": 1000 * d0,
        "waist_z_mm": z0,
        "divergence_full_mrad": theta * 1000,
        "M2_unconstrained": m2,
        "sub_diffraction_fit": bool(m2 < 1),
        "training_diameter_rmse_um": float(1000 * np.sqrt(np.mean(result.fun**2))),
        "holdout_diameter_rmse_um": float(np.sqrt(np.mean(delta**2))),
        "holdout_radius_rmse_um": float(np.sqrt(np.mean(delta**2)) / 2),
        "holdout_diameter_mae_um": float(np.mean(np.abs(delta))),
        "holdout_diameter_max_abs_um": float(np.max(np.abs(delta))),
        "holdout_mean_abs_relative_pct": float(100 * np.mean(np.abs(delta) / observed)),
        "predictions": [
            {
                "z_mm": r["z_mm"],
                "observed_diameter_um": r[axis],
                "predicted_diameter_um": p,
                "prediction_minus_observation_um": e,
            }
            for r, p, e in zip(test, prediction, delta)
        ],
    }


def write_series(cache, prefix, offline=False):
    settings = MANIFEST["series"][prefix]

    def measure(record):
        frame, diagnostics = read_frame(cache, record, settings, offline)
        x, y = diameters(frame)
        return {
            "z_mm": record["z_mm"],
            "d4sigma_x_um": x,
            "d4sigma_y_um": y,
            "acquisition": record["file"],
            **diagnostics,
        }

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = sorted(pool.map(measure, settings["frames"]), key=lambda r: r["z_mm"])
    positions = sorted({row["z_mm"] for row in rows})
    held_stations = {z for i, z in enumerate(positions) if i % 4 == 1}
    for row in rows:
        row["heldout"] = row["z_mm"] in held_stations
    table = cache / f"prahl-{prefix}.csv"
    fields = ["z_mm", "d4sigma_x_um", "d4sigma_y_um", "acquisition"]
    with tempfile.TemporaryDirectory(dir=cache) as temp:
        generated = Path(temp) / table.name
        with generated.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
        generated.replace(table)
    return {
        "source_commit": MANIFEST["commit"],
        "csv_sha256": hashlib.sha256(table.read_bytes()).hexdigest(),
        "wavelength_nm": MANIFEST["wavelength_nm"],
        "pixel_pitch_um": MANIFEST["pixel_pitch_um"],
        "holdout_z_mm": sorted(held_stations),
        "frames": [
            {k: r[k] for k in ["acquisition", "sha256", "detector_max_count", "adc_ceiling_pixels"]}
            for r in rows
        ],
        "camera_x": solve(rows, "d4sigma_x_um"),
        "camera_y": solve(rows, "d4sigma_y_um"),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--offline", action="store_true", help="Use only previously downloaded, verified camera frames."
    )
    parser.add_argument("--series", choices=("k", "sb", "all"), default="all")
    parser.add_argument(
        "--report", type=Path, help="Optional JSON for numerical results and acquisition checksums."
    )
    args = parser.parse_args()
    args.cache.mkdir(parents=True, exist_ok=True)
    report = {}
    for prefix in ["k", "sb"] if args.series == "all" else [args.series]:
        report[prefix] = write_series(args.cache, prefix, offline=args.offline)
        print(args.cache / f"prahl-{prefix}.csv", flush=True)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

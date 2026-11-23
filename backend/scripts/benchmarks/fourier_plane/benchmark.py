"""Calibrate on Cambridge setting 0, freeze predictions, then open the independent setting 3 camera image."""

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates
from scipy.optimize import least_squares

from archive import ARCHIVE_SHA256, CACHE, IMAGE, WAVE, array, digest, verified_archive

HERE = Path(__file__).resolve().parent
TRAIN, HELD = 0, 3
# Examination of the training camera only: one positive diffraction order, the DC and twin orders excluded.
X0, X1, Y0, Y1 = 430, 706, 523, 799
CX, CY = 568.0, 661.0
BLACK = 3.0  # Median DN in the training camera image.
SMOOTH_PX = 2.5
YY, XX = np.mgrid[Y0:Y1, X0:X1]
INNER = (XX - CX) ** 2 + (YY - CY) ** 2 <= 103**2


def dump(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


def preprocess(image):
    blurred = gaussian_filter(np.asarray(image, dtype=float), SMOOTH_PX)
    return blurred / blurred.mean()


def corr(a, b):
    return float(np.corrcoef(a.ravel(), b.ravel())[0, 1])


def rms(a):
    return float(np.sqrt(np.mean(np.square(a))))


def observed(capture):
    window = capture[Y0:Y1, X0:X1]
    return preprocess(np.maximum(window - BLACK, 0.0))


def native_fft(pupil, additional_phase):
    # Field amplitude is relative. Constant FFT factors cancel in the fixed ROI mean normalization.
    weighted = pupil.astype(np.complex128) * np.exp(1j * additional_phase.astype(np.float64))
    return np.abs(np.fft.fftshift(np.fft.fft2(weighted))) ** 2


def project(native, transform):
    # Uniform native frequency bins of the unpadded (1200,1920) FFT. x increases right, y down.
    fx, fy, sx, sy, angle = transform
    dx, dy = XX - CX, YY - CY
    cs, sn = np.cos(angle), np.sin(angle)
    x = fx + sx * (dx * cs - dy * sn)
    y = fy + sy * (dx * sn + dy * cs)
    on_sensor = map_coordinates(native, [y, x], order=1, mode="constant", prefilter=False)
    return preprocess(on_sensor)


def calibrate(amplitude, measured_phase, drive, truth):
    # At 658 nm, f=60 mm, the expected inverse sampling is ~1.22 and ~0.84 native bins/camera pixel.
    init = [787.2, 708.0, 1.22, 0.84, 0.0]
    lower, upper = [771, 694, 1.06, 0.73, -0.065], [804, 722, 1.38, 0.96, 0.065]
    candidates = []
    for sign in (1, -1):
        pupil = (amplitude * np.exp(1j * sign * measured_phase)).astype(np.complex64)
        power = native_fft(pupil, drive)

        def residual(t):
            return (project(power, t) - truth)[::4, ::4].ravel()

        solved = least_squares(residual, init, bounds=(lower, upper), max_nfev=90)
        candidates.append((corr(project(power, solved.x), truth), sign, solved.x.tolist()))
    return max(candidates), candidates


def phase_mask(original, index):
    gray = array(original, IMAGE + f"holo_{index}.npy")
    if gray.shape != (1200, 1920) or gray.dtype != np.uint16 or gray.max() >= 1024:
        raise ValueError("Unexpected native Santec SLM input.")
    return (gray.astype(float) * (2 * np.pi / 1024)).astype(np.float32)


def design(cache, original, *, training_only=False):
    amplitude = array(original, WAVE + "amp_wavefront.npy")
    measured_phase = array(original, WAVE + "phase_wavefront.npy")
    raw_train = array(original, IMAGE + f"capture_{TRAIN}.npy")
    if (
        amplitude.shape != (1200, 1920)
        or measured_phase.shape != amplitude.shape
        or raw_train.shape != (1080, 1440)
    ):
        raise ValueError("The complex pupil and/or the camera arrays have unexpected dimensions.")
    drive0 = phase_mask(original, TRAIN)
    truth = observed(raw_train)
    winner, choices = calibrate(amplitude, measured_phase, drive0, truth)
    fit_corr, sign, parameters = winner
    print("Training registration (correlation, measured phase sign, transform):", choices, flush=True)
    if training_only:
        return
    mask3 = phase_mask(original, HELD)
    pupil = (amplitude * np.exp(1j * sign * measured_phase)).astype(np.complex64)
    native = {str(index): native_fft(pupil, phase) for index, phase in ((TRAIN, drive0), (HELD, mask3))}
    native_metrics = {}
    for setting, intensity in native.items():
        row, col = np.unravel_index(intensity.argmax(), intensity.shape)
        dx, dy = 0.000658 * 60 / (1920 * 0.008), 0.000658 * 60 / (1200 * 0.008)
        native_metrics[setting] = {
            "peak_index_yx": [int(row), int(col)],
            "peak_xy_mm": [(int(col) - 960) * dx, (int(row) - 600) * dy],
            "energy_fraction_in_native_rectangle_cols650_915_rows595_822_half_open": float(
                intensity[595:822, 650:915].sum() / intensity.sum()
            ),
            "native_pitch_xy_mm": [dx, dy],
        }
    outputs = {
        "incident_pupil.npy": pupil,
        "phase_0_rad.npy": drive0,
        "phase_3_rad.npy": mask3,
        "prediction_0.npy": project(native[str(TRAIN)], parameters),
        "prediction_3.npy": project(native[str(HELD)], parameters),
        "training_observation.npy": truth,
    }
    for name, data in outputs.items():
        np.save(cache / name, data, allow_pickle=False)
    freeze = {
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive_sha256": ARCHIVE_SHA256,
        "code_sha256": {name: digest(HERE / name) for name in ["benchmark.py", "archive.py"]},
        "source_train": IMAGE + "capture_0.npy",
        "source_holdout": IMAGE + "capture_3.npy",
        "train_description": "uncorrected",
        "holdout_description": "amplitude and phase corrected hologram",
        "source_holdout_crc32_from_directory": f"{original.getinfo(IMAGE + 'capture_3.npy').CRC:08x}",
        "source_phase_sign": sign,
        "training_correlation": fit_corr,
        "transform": parameters,
        "native_metrics": native_metrics,
        "camera_roi_xyxy": [X0, Y0, X1, Y1],
        "camera_center_xy": [CX, CY],
        "inner_radius_px": 103,
        "black_detector_counts": BLACK,
        "camera_blur_sigma_px": SMOOTH_PX,
        "normalization": "Subtract fixed dark offset from camera and clip at zero. Smooth each ROI and divide each by its own mean. Unitless shape comparison.",
        "baselines": ["unchanged observed training image", "training observation plus predicted change"],
        "tests": {
            "shape": "Full first-order ROI Pearson r >= 0.80",
            "change": "Inside fixed disk the RMS error using observed train + predicted change must be at least 5% below the unchanged observed train baseline.",
            "joint_pass": "Both criteria must pass, unchanged registration and full pixel mask including saturated observations.",
        },
        "files_sha256": {name: digest(cache / name) for name in outputs},
        "raw_member_sha256": {
            key: hashlib.sha256(original.read(key)).hexdigest()
            for key in [
                WAVE + "amp_wavefront.npy",
                WAVE + "phase_wavefront.npy",
                IMAGE + "holo_0.npy",
                IMAGE + "holo_3.npy",
                IMAGE + "capture_0.npy",
                IMAGE + "info.txt",
            ]
        },
    }
    dump(cache / "freeze.json", freeze)
    print("Frozen, without opening the held-out camera data:", digest(cache / "freeze.json"), flush=True)


def score(cache, original):
    freeze = json.loads((cache / "freeze.json").read_text(encoding="utf-8"))
    for name, expected in freeze["files_sha256"].items():
        if digest(cache / name) != expected:
            raise RuntimeError(f"Frozen input or prediction changed: {name}")
    for name, expected in freeze["code_sha256"].items():
        if digest(HERE / name) != expected:
            raise RuntimeError(f"Frozen analysis code changed: {name}")
    # This is the first read/decode of capture_3. No registration, phase choice or tuning occurs below.
    raw = array(original, freeze["source_holdout"])
    truth = observed(raw)
    np.save(cache / "capture_3.npy", raw, allow_pickle=False)
    train = np.load(cache / "training_observation.npy", allow_pickle=False)
    predicted = np.load(cache / "prediction_3.npy", allow_pickle=False)
    train_pred = np.load(cache / "prediction_0.npy", allow_pickle=False)
    pred_change, actual_change = predicted - train_pred, truth - train
    baseline_error = rms(actual_change[INNER])
    corrected_error = rms((pred_change - actual_change)[INNER])
    delta_score = 1 - corrected_error / baseline_error
    shape = corr(truth, predicted)
    camera_roi = raw[Y0:Y1, X0:X1]
    report = {
        "scored_at_utc": datetime.now(timezone.utc).isoformat(),
        "freeze_sha256": digest(cache / "freeze.json"),
        "capture_sha256": hashlib.sha256(original.read(freeze["source_holdout"])).hexdigest(),
        "shape_pearson": shape,
        "unchanged_training_shape_pearson": corr(truth, train),
        "absolute_shape_rms": rms(truth - predicted),
        "unchanged_shape_rms": rms(truth - train),
        "change_pearson_in_disk": corr(pred_change[INNER], actual_change[INNER]),
        "change_rms_in_disk": corrected_error,
        "unchanged_rms_in_disk": baseline_error,
        "change_rmse_improvement_fraction": delta_score,
        "training_roi_clipped_fraction": float(
            np.mean(array(original, freeze["source_train"])[Y0:Y1, X0:X1] >= 1022)
        ),
        "held_roi_counts_at_or_above_1022_fraction": float(np.mean(camera_roi >= 1022)),
        "held_roi_max_dn": float(camera_roi.max()),
        "shape_pass": shape >= 0.80,
        "change_pass": delta_score >= 0.05,
        "joint_pass": bool(shape >= 0.80 and delta_score >= 0.05),
    }
    dump(cache / "score.json", report)
    print(json.dumps(report, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=["calibrate", "freeze", "score"])
    parser.add_argument("--cache", type=Path, default=CACHE)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    with verified_archive(args.cache, args.offline) as original:
        if args.stage == "score":
            score(args.cache, original)
        elif args.stage == "calibrate":
            design(args.cache, original, training_only=True)
        elif (args.cache / "freeze.json").exists():
            parser.error("This cache already has a freeze record. Use score, or rerun in a new cache.")
        else:
            design(args.cache, original)


if __name__ == "__main__":
    main()

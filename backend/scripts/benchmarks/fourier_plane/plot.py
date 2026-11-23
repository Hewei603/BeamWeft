"""Plot the frozen model and both camera measurements in the same registered coordinates."""

import argparse
import json
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from archive import CACHE
from benchmark import CX, CY, X0, X1, Y0, Y1, observed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=CACHE)
    args = parser.parse_args()
    cache = args.cache
    report = json.loads((cache / "score.json").read_text(encoding="utf-8"))
    simulated = [np.load(cache / f"prediction_{i}.npy") for i in (0, 3)]
    camera = [np.load(cache / "training_observation.npy"), observed(np.load(cache / "capture_3.npy"))]
    plot_comparison(simulated, camera, report["shape_pearson"], report["change_rmse_improvement_fraction"],
                    cache / "camera-comparison.png")


def plot_comparison(simulated, camera, shape_pearson, change_improvement, path):
    extent = [(X0 - 0.5 - CX) * 3.45, (X1 - 0.5 - CX) * 3.45, (Y1 - 0.5 - CY) * 3.45, (Y0 - 0.5 - CY) * 3.45]
    fig, axes = plt.subplots(2, 3, figsize=(11.4, 7.6), layout="constrained", sharex=True, sharey=True)
    fig.suptitle("Cambridge SLM bench: one held-out hologram", fontsize=17)
    for i, pair in enumerate((simulated, camera)):
        for j, data in enumerate((pair[0], pair[1], pair[1] - pair[0])):
            ax = axes[i, j]
            art = ax.imshow(
                data,
                extent=extent,
                interpolation="nearest",
                cmap="RdBu_r" if j == 2 else "magma",
                vmin=-2 if j == 2 else 0,
                vmax=2 if j == 2 else 4,
            )
            if j == 2:
                ring = plt.Circle((0, 0), 103 * 3.45, fill=False, color="#555555", linewidth=0.7, linestyle=":")
                ax.add_patch(ring)
            if i == 0:
                ax.set_title(["Training · setting 0", "Held out · setting 3", "Change · 3 minus 0"][j])
            if i == 1:
                ax.set_xlabel("Camera x offset (µm)")
            if j == 0:
                ax.set_ylabel(("Model" if i == 0 else "Camera") + "\nCamera y offset (µm)")
            if i == 1 and j in (1, 2):
                fig.colorbar(
                    art,
                    ax=axes[:, j] if j == 2 else list(axes[:, :2].flat),
                    location="bottom",
                    shrink=0.75,
                    label="Change in relative intensity" if j == 2 else "Relative intensity (ROI mean = 1)",
                )
    fig.text(
        0.5,
        -0.035,
        f"Held-out shape correlation {shape_pearson:.3f}. Change RMS improvement "
        f"{100 * change_improvement:.1f}% inside the dotted disk. "
        "Camera data: Wojcik et al., Cambridge Apollo, CC BY 4.0.",
        ha="center",
        fontsize=9,
    )
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    main()

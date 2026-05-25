#!/usr/bin/env python3
"""
Export monotonic PCHIP and LUT calibration artifacts from calibration_fit_table.csv.

The input run directory is expected to contain cameraXX/calibration_fit_table.csv
subfolders. Each channel is converted into:
  - temperature-level monotonic nodes
  - PCHIP interval coefficients
  - a dense linear/PCHIP LUT for simple deployment
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


CHANNELS = ("r", "g", "b")


@dataclass(frozen=True)
class PchipModel:
    x: np.ndarray
    y: np.ndarray
    slopes: np.ndarray
    coefficients: list[dict[str, float]]


def metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    residual = y_true - y_pred
    rmse = float(np.sqrt(np.mean(residual**2)))
    mae = float(np.mean(np.abs(residual)))
    ss_res = float(np.sum(residual**2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    return {"rmse": rmse, "mae": mae, "r2": r2}


def pchip_edge_slope(h0: float, h1: float, delta0: float, delta1: float) -> float:
    slope = ((2.0 * h0 + h1) * delta0 - h0 * delta1) / (h0 + h1)
    if np.sign(slope) != np.sign(delta0):
        return 0.0
    if np.sign(delta0) != np.sign(delta1) and abs(slope) > abs(3.0 * delta0):
        return float(3.0 * delta0)
    return float(slope)


def fit_pchip(x: np.ndarray, y: np.ndarray) -> PchipModel:
    if x.ndim != 1 or y.ndim != 1 or len(x) != len(y):
        raise ValueError("x and y must be one-dimensional arrays with equal length")
    if len(x) < 2:
        raise ValueError("at least two nodes are required")
    if not np.all(np.diff(x) > 0):
        raise ValueError("PCHIP x nodes must be strictly increasing")

    h = np.diff(x)
    delta = np.diff(y) / h
    slopes = np.zeros_like(x)

    if len(x) == 2:
        slopes[:] = delta[0]
    else:
        slopes[0] = pchip_edge_slope(h[0], h[1], delta[0], delta[1])
        slopes[-1] = pchip_edge_slope(h[-1], h[-2], delta[-1], delta[-2])
        for i in range(1, len(x) - 1):
            if delta[i - 1] == 0.0 or delta[i] == 0.0 or np.sign(delta[i - 1]) != np.sign(delta[i]):
                slopes[i] = 0.0
            else:
                w1 = 2.0 * h[i] + h[i - 1]
                w2 = h[i] + 2.0 * h[i - 1]
                slopes[i] = (w1 + w2) / (w1 / delta[i - 1] + w2 / delta[i])

    coefficients: list[dict[str, float]] = []
    for i in range(len(x) - 1):
        hi = h[i]
        di = delta[i]
        c0 = y[i]
        c1 = slopes[i]
        c2 = (3.0 * di - 2.0 * slopes[i] - slopes[i + 1]) / hi
        c3 = (slopes[i] + slopes[i + 1] - 2.0 * di) / (hi * hi)
        coefficients.append(
            {
                "x_left": float(x[i]),
                "x_right": float(x[i + 1]),
                "c0": float(c0),
                "c1": float(c1),
                "c2": float(c2),
                "c3": float(c3),
            }
        )
    return PchipModel(x=x, y=y, slopes=slopes, coefficients=coefficients)


def evaluate_pchip(model: PchipModel, x_values: np.ndarray) -> np.ndarray:
    xq = np.clip(np.asarray(x_values, dtype=float), model.x[0], model.x[-1])
    idx = np.searchsorted(model.x, xq, side="right") - 1
    idx = np.clip(idx, 0, len(model.coefficients) - 1)
    out = np.empty_like(xq, dtype=float)
    for interval_index in np.unique(idx):
        coef = model.coefficients[int(interval_index)]
        mask = idx == interval_index
        t = xq[mask] - coef["x_left"]
        out[mask] = coef["c0"] + coef["c1"] * t + coef["c2"] * t**2 + coef["c3"] * t**3
    return out


def build_nodes(fit_table: pd.DataFrame, channel: str) -> tuple[pd.DataFrame, list[str]]:
    x_col = f"x_{channel}"
    ref_col = f"ref_{channel}"
    work = fit_table[["blackbody_temp_c", "exposure_ms", x_col, ref_col]].dropna().copy()
    work = work.rename(columns={x_col: "x", ref_col: "ref_l_eff"})
    work = work.sort_values(["x", "blackbody_temp_c", "exposure_ms"]).reset_index(drop=True)

    grouped = work.groupby("x", as_index=False).agg(
        blackbody_temp_c=("blackbody_temp_c", "median"),
        exposure_ms=("exposure_ms", "median"),
        ref_l_eff=("ref_l_eff", "mean"),
        source_rows=("ref_l_eff", "size"),
    )
    notes: list[str] = []
    duplicate_count = int(len(work) - len(grouped))
    if duplicate_count:
        notes.append(f"merged {duplicate_count} duplicate x node(s)")

    y_raw = grouped["ref_l_eff"].to_numpy(float)
    y_iso = isotonic_non_decreasing(y_raw)
    adjusted = int(np.count_nonzero(np.abs(y_iso - y_raw) > np.maximum(np.abs(y_raw), 1.0) * 1e-12))
    if adjusted:
        notes.append(f"isotonic adjusted {adjusted} l_eff node(s)")

    nodes = grouped.copy()
    nodes["l_eff"] = y_iso
    nodes["raw_l_eff"] = y_raw
    nodes = nodes[["blackbody_temp_c", "exposure_ms", "x", "l_eff", "raw_l_eff", "source_rows"]]
    return nodes, notes


def isotonic_non_decreasing(y: np.ndarray) -> np.ndarray:
    values: list[float] = []
    weights: list[float] = []
    starts: list[int] = []
    ends: list[int] = []
    for i, value in enumerate(y.astype(float)):
        values.append(float(value))
        weights.append(1.0)
        starts.append(i)
        ends.append(i)
        while len(values) >= 2 and values[-2] > values[-1]:
            total_weight = weights[-2] + weights[-1]
            merged_value = (values[-2] * weights[-2] + values[-1] * weights[-1]) / total_weight
            values[-2] = merged_value
            weights[-2] = total_weight
            ends[-2] = ends[-1]
            values.pop()
            weights.pop()
            starts.pop()
            ends.pop()

    out = np.empty_like(y, dtype=float)
    for value, start, end in zip(values, starts, ends):
        out[start : end + 1] = value
    return out


def plot_channel(
    output_path: Path,
    fit_table: pd.DataFrame,
    channel: str,
    model: PchipModel,
    dense_x: np.ndarray,
    dense_y: np.ndarray,
    title: str,
) -> None:
    x_col = f"x_{channel}"
    ref_col = f"ref_{channel}"
    plt.figure(figsize=(7, 5))
    plt.scatter(fit_table[x_col], fit_table[ref_col], s=18, alpha=0.55, label="measurements")
    plt.plot(dense_x, dense_y, linewidth=2.0, label="PCHIP")
    plt.scatter(model.x, model.y, s=42, marker="x", label="nodes")
    plt.xlabel(f"X_{channel}")
    plt.ylabel(f"L_eff_{channel}")
    plt.title(title)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_path, dpi=160)
    plt.close()


def export_camera(camera_dir: Path, dense_points: int) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    fit_table_path = camera_dir / "calibration_fit_table.csv"
    fit_table = pd.read_csv(fit_table_path)
    out_dir = camera_dir / "monotonic_pchip_lut"
    out_dir.mkdir(parents=True, exist_ok=True)

    camera = camera_dir.name
    summary_rows: list[dict[str, object]] = []
    audit_rows: list[dict[str, object]] = []
    coeff_json: dict[str, object] = {
        "method": "monotonic_pchip",
        "node_policy": "all measurement rows sorted by X, with duplicate X merged and reference L_eff adjusted by isotonic non-decreasing regression",
        "range_policy": "clamp input X to the calibrated node range before interpolation",
        "dense_lut_points": dense_points,
        "channels": {},
    }

    dense_rows: list[dict[str, object]] = []
    node_rows: list[dict[str, object]] = []
    interval_rows: list[dict[str, object]] = []

    for channel in CHANNELS:
        nodes, notes = build_nodes(fit_table, channel)
        model = fit_pchip(nodes["x"].to_numpy(float), nodes["l_eff"].to_numpy(float))
        dense_x = np.linspace(model.x[0], model.x[-1], dense_points)
        dense_y = evaluate_pchip(model, dense_x)

        x_values = fit_table[f"x_{channel}"].to_numpy(float)
        y_true = fit_table[f"ref_{channel}"].to_numpy(float)
        pchip_pred = evaluate_pchip(model, x_values)
        lut_pred = np.interp(np.clip(x_values, model.x[0], model.x[-1]), model.x, model.y)
        pchip_metrics = metrics(y_true, pchip_pred)
        lut_metrics = metrics(y_true, lut_pred)

        summary_rows.append(
            {
                "camera": camera,
                "channel": channel,
                "node_count": len(nodes),
                "x_min": float(model.x[0]),
                "x_max": float(model.x[-1]),
                "pchip_rmse": pchip_metrics["rmse"],
                "pchip_mae": pchip_metrics["mae"],
                "pchip_r2": pchip_metrics["r2"],
                "linear_lut_rmse": lut_metrics["rmse"],
                "linear_lut_mae": lut_metrics["mae"],
                "linear_lut_r2": lut_metrics["r2"],
                "notes": "; ".join(notes),
            }
        )
        for note in notes:
            audit_rows.append({"camera": camera, "channel": channel, "note": note})

        coeff_json["channels"][channel] = {
            "x_definition": f"X_{channel} from calibration_fit_table.csv",
            "nodes": nodes.to_dict(orient="records"),
            "slopes": [float(v) for v in model.slopes],
            "interval_coefficients": model.coefficients,
            "metrics_on_fit_table": {"pchip": pchip_metrics, "linear_lut_nodes": lut_metrics},
        }

        for i, row in nodes.reset_index(drop=True).iterrows():
            node_rows.append({"camera": camera, "channel": channel, "node_index": int(i), **row.to_dict()})
        for i, coef in enumerate(model.coefficients):
            interval_rows.append({"camera": camera, "channel": channel, "interval_index": i, **coef})
        for i, (x_value, y_value) in enumerate(zip(dense_x, dense_y)):
            dense_rows.append({"camera": camera, "channel": channel, "lut_index": i, "x": float(x_value), "l_eff": float(y_value)})

        plot_channel(
            out_dir / f"pchip_fit_{channel}.png",
            fit_table,
            channel,
            model,
            dense_x,
            dense_y,
            f"{camera} {channel.upper()} monotonic PCHIP",
        )

    (out_dir / "pchip_coefficients_standard.json").write_text(
        json.dumps(coeff_json, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    pd.DataFrame(node_rows).to_csv(out_dir / "pchip_nodes_standard.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(interval_rows).to_csv(out_dir / "pchip_interval_coefficients_standard.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(dense_rows).to_csv(out_dir / f"pchip_dense_lut_{dense_points}_standard.csv", index=False, encoding="utf-8-sig")
    return summary_rows, audit_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export monotonic PCHIP and LUT calibration artifacts.")
    parser.add_argument("--run-dir", type=Path, required=True, help="calibration run folder containing cameraXX subfolders")
    parser.add_argument("--dense-points", type=int, default=512, help="number of dense LUT points per camera/channel")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.dense_points < 2:
        raise SystemExit("--dense-points must be at least 2")

    camera_dirs = sorted(
        p
        for p in args.run_dir.iterdir()
        if p.is_dir() and re.fullmatch(r"camera\d{2}", p.name) and (p / "calibration_fit_table.csv").exists()
    )
    if not camera_dirs:
        raise SystemExit(f"no cameraXX/calibration_fit_table.csv folders found in {args.run_dir}")

    summary_rows: list[dict[str, object]] = []
    audit_rows: list[dict[str, object]] = []
    for camera_dir in camera_dirs:
        rows, audit = export_camera(camera_dir, args.dense_points)
        summary_rows.extend(rows)
        audit_rows.extend(audit)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(args.run_dir / "monotonic_pchip_lut_summary.csv", index=False, encoding="utf-8-sig")
    if audit_rows:
        pd.DataFrame(audit_rows).to_csv(args.run_dir / "monotonic_pchip_lut_audit.csv", index=False, encoding="utf-8-sig")
    else:
        with (args.run_dir / "monotonic_pchip_lut_audit.csv").open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["camera", "channel", "note"])
            writer.writeheader()
    print(f"Wrote {args.run_dir / 'monotonic_pchip_lut_summary.csv'}")


if __name__ == "__main__":
    main()

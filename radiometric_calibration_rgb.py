#!/usr/bin/env python3
"""
RGB camera radiometric calibration with channel spectral responses and a 650 nm
short-pass filter.

Input measurement CSV, minimum columns:
    exposure_ms,dn_r,dn_g,dn_b,dark_r,dark_g,dark_b

Reference values can be provided in one of three ways:
  1. Direct effective radiance columns:
        l_eff_r,l_eff_g,l_eff_b
  2. A spectrum file per row:
        spectrum_file
     Each spectrum CSV must contain:
        wavelength_nm,radiance
  3. Blackbody temperature columns:
        blackbody_temp_c or blackbody_temp_k
     Optional:
        emissivity

The script computes:
    x_c = (dn_c - dark_c) / exposure_seconds
and fits one calibration curve per channel:
    l_eff_c = f_c(x_c)
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import images_to_blackbody_measurements as image_measurements


CHANNELS = ("r", "g", "b")
RESPONSE_COLUMNS = {"r": "red", "g": "green", "b": "blue"}
DIRECT_REF_COLUMNS = {"r": "l_eff_r", "g": "l_eff_g", "b": "l_eff_b"}

PLANCK_H = 6.62607015e-34
PLANCK_C = 299792458.0
PLANCK_K = 1.380649e-23


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df


def require_columns(df: pd.DataFrame, columns: Iterable[str], name: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")


def load_spectral_response(path: Path) -> pd.DataFrame:
    df = normalize_columns(pd.read_csv(path))
    require_columns(df, ["wavelength_nm", "red", "green", "blue"], "spectral response CSV")
    df = df[["wavelength_nm", "red", "green", "blue"]].dropna()
    df = df.sort_values("wavelength_nm").reset_index(drop=True)
    return df


def load_filter_transmission(
    wavelengths: np.ndarray,
    filter_csv: Optional[Path],
    cutoff_nm: float,
) -> np.ndarray:
    if filter_csv is None:
        return np.where(wavelengths <= cutoff_nm, 1.0, 0.0)

    df = normalize_columns(pd.read_csv(filter_csv))
    require_columns(df, ["wavelength_nm", "transmission"], "filter transmission CSV")
    df = df.sort_values("wavelength_nm")
    t = np.interp(
        wavelengths,
        df["wavelength_nm"].to_numpy(float),
        df["transmission"].to_numpy(float),
        left=0.0,
        right=0.0,
    )
    return np.clip(t, 0.0, 1.0)


def build_effective_response(
    response: pd.DataFrame,
    filter_csv: Optional[Path],
    cutoff_nm: float,
) -> pd.DataFrame:
    out = response.copy()
    wavelengths = out["wavelength_nm"].to_numpy(float)
    out["filter_t"] = load_filter_transmission(wavelengths, filter_csv, cutoff_nm)
    for ch, response_col in RESPONSE_COLUMNS.items():
        out[f"s_eff_{ch}"] = out[response_col].to_numpy(float) * out["filter_t"].to_numpy(float)
    return out


def blackbody_spectral_radiance_nm(
    wavelength_nm: np.ndarray,
    temperature_k: float,
    emissivity: float = 1.0,
) -> np.ndarray:
    """Planck spectral radiance in W/(m^2 sr nm)."""
    if temperature_k <= 0:
        raise ValueError(f"blackbody temperature must be positive K, got {temperature_k}")
    if emissivity < 0:
        raise ValueError(f"emissivity must be non-negative, got {emissivity}")

    wavelength_m = wavelength_nm * 1e-9
    exponent = PLANCK_H * PLANCK_C / (wavelength_m * PLANCK_K * temperature_k)
    radiance_per_m = np.zeros_like(wavelength_m, dtype=float)
    valid = exponent < 700.0
    radiance_per_m[valid] = (
        (2.0 * PLANCK_H * PLANCK_C**2)
        / (wavelength_m[valid] ** 5)
        / np.expm1(exponent[valid])
    )
    return emissivity * radiance_per_m * 1e-9


def integrate_radiance_reference(
    wavelength_nm: np.ndarray,
    radiance: np.ndarray,
    effective_response: pd.DataFrame,
    reference_kind: str,
    source_name: str,
) -> Dict[str, float]:
    wl = effective_response["wavelength_nm"].to_numpy(float)
    radiance_on_grid = np.interp(
        wl,
        wavelength_nm,
        radiance,
        left=np.nan,
        right=np.nan,
    )

    refs: Dict[str, float] = {}
    valid = np.isfinite(radiance_on_grid)
    if valid.sum() < 2:
        raise ValueError(f"radiance source has insufficient overlap with response: {source_name}")

    for ch in CHANNELS:
        weight = effective_response[f"s_eff_{ch}"].to_numpy(float)
        mask = valid & (weight > 0)
        if mask.sum() < 2:
            refs[ch] = np.nan
            continue
        numerator = np.trapezoid(radiance_on_grid[mask] * weight[mask], wl[mask])
        if reference_kind == "integrated":
            refs[ch] = float(numerator)
        else:
            denominator = np.trapezoid(weight[mask], wl[mask])
            refs[ch] = float(numerator / denominator)
    return refs


def integrate_channel_reference(
    spectrum_path: Path,
    effective_response: pd.DataFrame,
    reference_kind: str,
) -> Dict[str, float]:
    spec = normalize_columns(pd.read_csv(spectrum_path))
    require_columns(spec, ["wavelength_nm", "radiance"], f"spectrum CSV {spectrum_path}")
    spec = spec.dropna().sort_values("wavelength_nm")
    return integrate_radiance_reference(
        spec["wavelength_nm"].to_numpy(float),
        spec["radiance"].to_numpy(float),
        effective_response,
        reference_kind,
        str(spectrum_path),
    )


def integrate_blackbody_reference(
    temperature_k: float,
    emissivity: float,
    effective_response: pd.DataFrame,
    reference_kind: str,
) -> Dict[str, float]:
    wl = effective_response["wavelength_nm"].to_numpy(float)
    radiance = blackbody_spectral_radiance_nm(wl, temperature_k, emissivity)
    return integrate_radiance_reference(
        wl,
        radiance,
        effective_response,
        reference_kind,
        f"blackbody {temperature_k:.3f} K, emissivity {emissivity:.5g}",
    )


def exposure_seconds(measurements: pd.DataFrame) -> np.ndarray:
    if "exposure_s" in measurements.columns:
        return measurements["exposure_s"].to_numpy(float)
    if "exposure_ms" in measurements.columns:
        return measurements["exposure_ms"].to_numpy(float) / 1000.0
    raise ValueError("measurement CSV must contain exposure_ms or exposure_s")


def add_reference_columns(
    measurements: pd.DataFrame,
    measurement_path: Path,
    effective_response: pd.DataFrame,
    reference_kind: str,
) -> pd.DataFrame:
    df = measurements.copy()
    direct_cols = list(DIRECT_REF_COLUMNS.values())
    has_direct = all(c in df.columns for c in direct_cols) and df[direct_cols].notna().all().all()
    if has_direct:
        return df

    has_spectrum_file = "spectrum_file" in df.columns and df["spectrum_file"].notna().all()
    has_blackbody_c = "blackbody_temp_c" in df.columns and df["blackbody_temp_c"].notna().all()
    has_blackbody_k = "blackbody_temp_k" in df.columns and df["blackbody_temp_k"].notna().all()

    if not has_spectrum_file and not has_blackbody_c and not has_blackbody_k:
        raise ValueError(
            "measurement CSV must contain l_eff_r/l_eff_g/l_eff_b, spectrum_file, "
            "blackbody_temp_c, or blackbody_temp_k"
        )

    spectrum_cache: Dict[Path, Dict[str, float]] = {}
    blackbody_cache: Dict[Tuple[float, float], Dict[str, float]] = {}
    for idx, row in df.iterrows():
        if "spectrum_file" in df.columns and pd.notna(row.get("spectrum_file")) and str(row["spectrum_file"]).strip():
            spectrum_path = Path(str(row["spectrum_file"]))
            if not spectrum_path.is_absolute():
                spectrum_path = measurement_path.parent / spectrum_path
            spectrum_path = spectrum_path.resolve()
            if spectrum_path not in spectrum_cache:
                spectrum_cache[spectrum_path] = integrate_channel_reference(
                    spectrum_path, effective_response, reference_kind
                )
            refs = spectrum_cache[spectrum_path]
        else:
            if "blackbody_temp_k" in df.columns and pd.notna(row.get("blackbody_temp_k")):
                temperature_k = float(row["blackbody_temp_k"])
            else:
                temperature_k = float(row["blackbody_temp_c"]) + 273.15
            emissivity = float(row["emissivity"]) if "emissivity" in df.columns and pd.notna(row.get("emissivity")) else 1.0
            cache_key = (temperature_k, emissivity)
            if cache_key not in blackbody_cache:
                blackbody_cache[cache_key] = integrate_blackbody_reference(
                    temperature_k, emissivity, effective_response, reference_kind
                )
            refs = blackbody_cache[cache_key]
        for ch in CHANNELS:
            df.loc[idx, DIRECT_REF_COLUMNS[ch]] = refs[ch]
    return df


def prepare_fit_table(
    measurements: pd.DataFrame,
    measurement_path: Path,
    effective_response: pd.DataFrame,
    reference_kind: str,
) -> pd.DataFrame:
    df = normalize_columns(measurements)
    require_columns(
        df,
        ["dn_r", "dn_g", "dn_b", "dark_r", "dark_g", "dark_b"],
        "measurement CSV",
    )
    df = add_reference_columns(df, measurement_path, effective_response, reference_kind)

    t = exposure_seconds(df)
    if np.any(t <= 0):
        raise ValueError("exposure time must be positive")
    df["exposure_s_used"] = t

    for ch in CHANNELS:
        df[f"dn_corr_{ch}"] = df[f"dn_{ch}"].to_numpy(float) - df[f"dark_{ch}"].to_numpy(float)
        df[f"x_{ch}"] = df[f"dn_corr_{ch}"].to_numpy(float) / t
        df[f"ref_{ch}"] = df[DIRECT_REF_COLUMNS[ch]].to_numpy(float)
    return df


def exposure_group_keys(df: pd.DataFrame) -> pd.Series:
    if "level" in df.columns:
        return df["level"].fillna("").astype(str)
    ref_cols = [f"ref_{ch}" for ch in CHANNELS]
    if all(c in df.columns for c in ref_cols):
        return df[ref_cols].apply(lambda row: "_".join(f"{float(v):.12g}" for v in row), axis=1)
    return pd.Series(["all"] * len(df), index=df.index)


def safe_rel_span_percent(values: np.ndarray) -> float:
    values = np.asarray(values, float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float("nan")
    mean_abs = float(abs(np.mean(values)))
    if mean_abs == 0:
        return float("nan")
    return float((np.max(values) - np.min(values)) / mean_abs * 100.0)


def safe_cv_percent(values: np.ndarray) -> float:
    values = np.asarray(values, float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float("nan")
    mean_abs = float(abs(np.mean(values)))
    if mean_abs == 0:
        return float("nan")
    return float(np.std(values) / mean_abs * 100.0)


def estimate_exposure_intercept_for_channel(fit_table: pd.DataFrame, ch: str) -> Dict[str, object]:
    t = fit_table["exposure_s_used"].to_numpy(float)
    y = fit_table[f"dn_corr_{ch}"].to_numpy(float)
    groups = exposure_group_keys(fit_table)
    mask = np.isfinite(t) & np.isfinite(y) & (t > 0)
    t = t[mask]
    y = y[mask]
    groups = groups[mask]

    usable_groups = []
    for group in sorted(groups.unique()):
        group_t = t[groups.to_numpy() == group]
        if len(np.unique(group_t)) >= 2:
            usable_groups.append(group)

    usable = groups.isin(usable_groups).to_numpy()
    t = t[usable]
    y = y[usable]
    groups = groups[usable]
    if len(usable_groups) == 0 or len(y) < len(usable_groups) + 1:
        return {
            "available": False,
            "q_intercept": 0.0,
            "r2": float("nan"),
            "rmse": float("nan"),
            "groups_used": [],
            "note": "Need at least one group with two or more exposure times.",
        }

    design = np.zeros((len(y), 1 + len(usable_groups)), dtype=float)
    design[:, 0] = 1.0
    group_to_col = {group: idx + 1 for idx, group in enumerate(usable_groups)}
    group_values = groups.to_numpy()
    for row_idx, group in enumerate(group_values):
        design[row_idx, group_to_col[group]] = t[row_idx]

    params, *_ = np.linalg.lstsq(design, y, rcond=None)
    pred = design @ params
    residual = pred - y
    rmse = float(np.sqrt(np.mean(residual**2)))
    ss_res = float(np.sum(residual**2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    slopes = {group: float(params[group_to_col[group]]) for group in usable_groups}
    return {
        "available": True,
        "q_intercept": float(params[0]),
        "r2": r2,
        "rmse": rmse,
        "groups_used": usable_groups,
        "slopes_by_group": slopes,
        "note": "Model: DN_corr_c = slope_c_group * exposure_s + q_c",
    }


def add_intercept_corrected_x(fit_table: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, Dict[str, object]]]:
    df = fit_table.copy()
    corrections: Dict[str, Dict[str, object]] = {}
    t = df["exposure_s_used"].to_numpy(float)
    for ch in CHANNELS:
        info = estimate_exposure_intercept_for_channel(df, ch)
        corrections[ch] = info
        q = float(info["q_intercept"])
        df[f"exposure_intercept_q_{ch}"] = q
        df[f"x_intercept_{ch}"] = (df[f"dn_corr_{ch}"].to_numpy(float) - q) / t
    return df, corrections


def exposure_stability_diagnostics(
    fit_table: pd.DataFrame,
    corrections: Dict[str, Dict[str, object]],
) -> pd.DataFrame:
    groups = exposure_group_keys(fit_table)
    rows = []
    for ch in CHANNELS:
        for group in sorted(groups.unique()):
            sub = fit_table[groups == group]
            if len(sub) == 0:
                continue
            x_standard = sub[f"x_{ch}"].to_numpy(float)
            x_corrected = sub[f"x_intercept_{ch}"].to_numpy(float)
            exposures = sub["exposure_s_used"].to_numpy(float)
            q = float(corrections[ch]["q_intercept"])
            rows.append(
                {
                    "channel": ch,
                    "group": group,
                    "n": len(sub),
                    "exposure_min_s": float(np.min(exposures)),
                    "exposure_max_s": float(np.max(exposures)),
                    "q_intercept": q,
                    "intercept_model_r2": float(corrections[ch]["r2"]),
                    "standard_x_mean": float(np.mean(x_standard)),
                    "standard_x_rel_span_percent": safe_rel_span_percent(x_standard),
                    "standard_x_cv_percent": safe_cv_percent(x_standard),
                    "intercept_x_mean": float(np.mean(x_corrected)),
                    "intercept_x_rel_span_percent": safe_rel_span_percent(x_corrected),
                    "intercept_x_cv_percent": safe_cv_percent(x_corrected),
                }
            )
    return pd.DataFrame(rows)


def exposure_window_diagnostics(
    fit_table: pd.DataFrame,
    min_points: int = 2,
) -> pd.DataFrame:
    groups = exposure_group_keys(fit_table)
    rows = []
    for group in sorted(groups.unique()):
        sub_group = fit_table[groups == group].copy()
        if sub_group.empty:
            continue
        exposure_values = sorted(float(v) for v in sub_group["exposure_s_used"].dropna().unique())
        max_exposure = max(exposure_values) if exposure_values else float("nan")
        for min_exposure in exposure_values:
            window = sub_group[sub_group["exposure_s_used"] >= min_exposure]
            exposure_level_count = int(window["exposure_s_used"].nunique())
            if exposure_level_count < min_points:
                continue
            for ch in CHANNELS:
                standard_x = window[f"x_{ch}"].to_numpy(float)
                corrected_x = window[f"x_intercept_{ch}"].to_numpy(float)
                standard_span = safe_rel_span_percent(standard_x)
                corrected_span = safe_rel_span_percent(corrected_x)
                rows.append(
                    {
                        "group": group,
                        "channel": ch,
                        "min_exposure_s": float(min_exposure),
                        "max_exposure_s": float(max_exposure),
                        "min_exposure_ms": float(min_exposure * 1000.0),
                        "max_exposure_ms": float(max_exposure * 1000.0),
                        "n": len(window),
                        "exposure_level_count": exposure_level_count,
                        "standard_x_rel_span_percent": standard_span,
                        "intercept_x_rel_span_percent": corrected_span,
                        "span_improvement_percent": float(standard_span - corrected_span)
                        if np.isfinite(standard_span) and np.isfinite(corrected_span)
                        else float("nan"),
                        "standard_x_cv_percent": safe_cv_percent(standard_x),
                        "intercept_x_cv_percent": safe_cv_percent(corrected_x),
                    }
                )
    return pd.DataFrame(rows)


def finite_positive_pair(x: np.ndarray, y: np.ndarray, log_space: bool) -> Tuple[np.ndarray, np.ndarray]:
    mask = np.isfinite(x) & np.isfinite(y)
    if log_space:
        mask &= (x > 0) & (y > 0)
    return x[mask], y[mask]


def fit_channel(x: np.ndarray, y: np.ndarray, model: str, degree: int) -> Dict[str, object]:
    log_space = model == "logpoly"
    x_fit, y_fit = finite_positive_pair(x, y, log_space=log_space)
    if len(x_fit) < degree + 1:
        raise ValueError(f"not enough valid points for degree {degree}: got {len(x_fit)}")

    if model == "linear":
        coeff = np.polyfit(x_fit, y_fit, 1)
        pred = np.polyval(coeff, x_fit)
        return {"model": model, "degree": 1, "coefficients": coeff.tolist(), "x": x_fit, "y": y_fit, "pred": pred}

    if model == "poly":
        coeff = np.polyfit(x_fit, y_fit, degree)
        pred = np.polyval(coeff, x_fit)
        return {"model": model, "degree": degree, "coefficients": coeff.tolist(), "x": x_fit, "y": y_fit, "pred": pred}

    if model == "logpoly":
        lx = np.log(x_fit)
        ly = np.log(y_fit)
        coeff = np.polyfit(lx, ly, degree)
        pred = np.exp(np.polyval(coeff, lx))
        return {"model": model, "degree": degree, "coefficients": coeff.tolist(), "x": x_fit, "y": y_fit, "pred": pred}

    raise ValueError(f"unknown model: {model}")


def metrics(y: np.ndarray, pred: np.ndarray) -> Dict[str, float]:
    residual = pred - y
    rmse = float(np.sqrt(np.mean(residual**2)))
    mae = float(np.mean(np.abs(residual)))
    rel = np.where(y != 0, residual / y, np.nan)
    mape = float(np.nanmean(np.abs(rel)) * 100.0)
    ss_res = float(np.sum(residual**2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    return {"rmse": rmse, "mae": mae, "mape_percent": mape, "r2": r2}


def format_equation(ch: str, fit: Dict[str, object]) -> str:
    coeff = [float(c) for c in fit["coefficients"]]
    if fit["model"] == "linear":
        a, b = coeff
        return f"L_eff_{ch} = ({a:.10g}) * X_{ch} + ({b:.10g})"

    if fit["model"] == "poly":
        terms = []
        degree = int(fit["degree"])
        for i, c in enumerate(coeff):
            power = degree - i
            if power == 0:
                terms.append(f"({c:.10g})")
            elif power == 1:
                terms.append(f"({c:.10g}) * X_{ch}")
            else:
                terms.append(f"({c:.10g}) * X_{ch}^{power}")
        return f"L_eff_{ch} = " + " + ".join(terms)

    degree = int(fit["degree"])
    terms = []
    for i, c in enumerate(coeff):
        power = degree - i
        if power == 0:
            terms.append(f"({c:.10g})")
        elif power == 1:
            terms.append(f"({c:.10g}) * ln(X_{ch})")
        else:
            terms.append(f"({c:.10g}) * ln(X_{ch})^{power}")
    return f"ln(L_eff_{ch}) = " + " + ".join(terms)


def predict_fit_values(fit: Dict[str, object], x: np.ndarray) -> np.ndarray:
    coeff = np.asarray(fit["coefficients"], dtype=float)
    model = str(fit["model"])
    if model in {"linear", "poly"}:
        return np.polyval(coeff, x)
    if model == "logpoly":
        pred = np.full_like(x, np.nan, dtype=float)
        mask = x > 0
        pred[mask] = np.exp(np.polyval(coeff, np.log(x[mask])))
        return pred
    raise ValueError(f"unknown model: {model}")


def robust_scale(values: np.ndarray) -> float:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return float("nan")
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad > 0:
        return 1.4826 * mad
    std = float(np.std(values))
    return std if std > 0 else float("nan")


def outlier_label(row: pd.Series) -> str:
    temperature = row.get("blackbody_temp_c", "")
    exposure = row.get("exposure_ms", "")
    level = row.get("level", "")
    return f"{level} T{temperature} exposure {exposure} ms"


def detect_outliers(
    fit_table: pd.DataFrame,
    x_prefix: str,
    model: str,
    degree: int,
    threshold: float,
    min_channels: int,
) -> pd.DataFrame:
    report = pd.DataFrame(
        {
            "row_index": fit_table.index,
            "level": fit_table["level"] if "level" in fit_table.columns else "",
            "blackbody_temp_c": fit_table["blackbody_temp_c"] if "blackbody_temp_c" in fit_table.columns else "",
            "exposure_ms": fit_table["exposure_ms"] if "exposure_ms" in fit_table.columns else "",
            "outlier_channel_count": 0,
            "is_outlier": False,
            "reason": "",
        }
    )
    channel_reasons: dict[int, list[str]] = {int(idx): [] for idx in fit_table.index}
    groups = exposure_group_keys(fit_table)

    for ch in CHANNELS:
        values = fit_table[f"{x_prefix}{ch}"].to_numpy(float)
        deviation = np.full(len(fit_table), np.nan, dtype=float)
        robust_z = np.full(len(fit_table), np.nan, dtype=float)
        flagged = np.zeros(len(fit_table), dtype=bool)

        for group in sorted(groups.unique()):
            group_mask = (groups.to_numpy() == group) & np.isfinite(values)
            if int(group_mask.sum()) < 4:
                continue
            group_values = values[group_mask]
            center = float(np.median(group_values))
            scale = robust_scale(group_values - center)
            deviation[group_mask] = values[group_mask] - center
            if not np.isfinite(scale) or scale <= 0:
                continue
            robust_z[group_mask] = np.abs(deviation[group_mask]) / scale
            flagged |= group_mask & (robust_z > threshold)

        if not np.isfinite(robust_z).any():
            report[f"{ch}_deviation"] = np.nan
            report[f"{ch}_robust_z"] = np.nan
            report[f"{ch}_is_outlier"] = False
            continue

        report[f"{ch}_deviation"] = deviation
        report[f"{ch}_robust_z"] = robust_z
        report[f"{ch}_is_outlier"] = flagged
        flagged_positions = np.where(flagged)[0]
        for pos in flagged_positions:
            idx = int(fit_table.index[pos])
            channel_reasons[idx].append(f"{ch}:z={float(robust_z[pos]):.2f}")

    for idx, reasons in channel_reasons.items():
        count = len(reasons)
        report.loc[report["row_index"] == idx, "outlier_channel_count"] = count
        if count >= min_channels:
            report.loc[report["row_index"] == idx, "is_outlier"] = True
            report.loc[report["row_index"] == idx, "reason"] = "; ".join(reasons)
    return report


def plot_effective_response(eff: pd.DataFrame, output_dir: Path) -> None:
    plt.figure(figsize=(8, 4.8))
    for ch, color in [("r", "red"), ("g", "green"), ("b", "blue")]:
        plt.plot(eff["wavelength_nm"], eff[f"s_eff_{ch}"], label=f"{ch.upper()} effective", color=color)
    plt.plot(eff["wavelength_nm"], eff["filter_t"], label="filter transmission", color="black", alpha=0.5)
    plt.xlabel("Wavelength (nm)")
    plt.ylabel("Relative response")
    plt.grid(True, alpha=0.25)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / "effective_response.png", dpi=180)
    plt.close()


def plot_fit(
    ch: str,
    fit: Dict[str, object],
    output_dir: Path,
    filename_suffix: str = "",
    x_label: Optional[str] = None,
) -> None:
    x = np.asarray(fit["x"], float)
    y = np.asarray(fit["y"], float)
    pred = np.asarray(fit["pred"], float)
    order = np.argsort(x)

    plt.figure(figsize=(6.8, 4.8))
    plt.scatter(x, y, label="measurement", s=32)
    plt.plot(x[order], pred[order], label="fit", linewidth=2)
    plt.xlabel(x_label or f"X_{ch} = (DN_{ch} - Dark_{ch}) / exposure_s")
    plt.ylabel(f"L_eff_{ch}")
    plt.grid(True, alpha=0.25)
    plt.legend()
    plt.tight_layout()
    plt.savefig(output_dir / f"fit{filename_suffix}_{ch}.png", dpi=180)
    plt.close()

    rel_error = np.where(y != 0, (pred - y) / y * 100.0, np.nan)
    plt.figure(figsize=(6.8, 3.8))
    plt.axhline(0, color="black", linewidth=1)
    plt.scatter(x, rel_error, s=32)
    plt.xlabel(f"X_{ch}")
    plt.ylabel("Relative error (%)")
    plt.grid(True, alpha=0.25)
    plt.tight_layout()
    plt.savefig(output_dir / f"residual{filename_suffix}_{ch}.png", dpi=180)
    plt.close()


def results_to_serializable(results: Dict[str, Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    return {
        ch: {
            "equation": format_equation(ch, results[ch]["fit"]),
            "model": results[ch]["fit"]["model"],
            "degree": results[ch]["fit"]["degree"],
            "coefficients": results[ch]["fit"]["coefficients"],
            "metrics": results[ch]["metrics"],
        }
        for ch in CHANNELS
    }


def write_report(
    results: Dict[str, Dict[str, object]],
    output_dir: Path,
    filename: str,
    title: str,
    extra_lines: Optional[list[str]] = None,
) -> None:
    lines = [f"# {title}", ""]
    if extra_lines:
        lines.extend(extra_lines)
        lines.append("")
    for ch in CHANNELS:
        fit = results[ch]["fit"]
        met = results[ch]["metrics"]
        lines.append(f"## Channel {ch.upper()}")
        lines.append("")
        lines.append(format_equation(ch, fit))
        lines.append("")
        lines.append(f"- RMSE: {met['rmse']:.10g}")
        lines.append(f"- MAE: {met['mae']:.10g}")
        lines.append(f"- MAPE: {met['mape_percent']:.4f}%")
        lines.append(f"- R2: {met['r2']:.8f}")
        lines.append("")
    (output_dir / filename).write_text("\n".join(lines), encoding="utf-8")


def fit_all_channels(
    fit_table: pd.DataFrame,
    x_prefix: str,
    model: str,
    degree: int,
    output_dir: Path,
    filename_suffix: str,
    x_label_template: str,
) -> Dict[str, Dict[str, object]]:
    results: Dict[str, Dict[str, object]] = {}
    for ch in CHANNELS:
        model_degree = 1 if model == "linear" else degree
        fit = fit_channel(
            fit_table[f"{x_prefix}{ch}"].to_numpy(float),
            fit_table[f"ref_{ch}"].to_numpy(float),
            model,
            model_degree,
        )
        met = metrics(np.asarray(fit["y"], float), np.asarray(fit["pred"], float))
        results[ch] = {"fit": fit, "metrics": met}
        plot_fit(
            ch,
            fit,
            output_dir,
            filename_suffix=filename_suffix,
            x_label=x_label_template.format(ch=ch),
        )
    return results


def build_calibration_comparison(
    standard_results: Dict[str, Dict[str, object]],
    intercept_results: Dict[str, Dict[str, object]],
    corrections: Dict[str, Dict[str, object]],
) -> pd.DataFrame:
    rows = []
    for ch in CHANNELS:
        for label, results in [
            ("standard", standard_results),
            ("intercept_corrected", intercept_results),
        ]:
            met = results[ch]["metrics"]
            rows.append(
                {
                    "channel": ch,
                    "calibration_form": label,
                    "q_intercept": 0.0 if label == "standard" else float(corrections[ch]["q_intercept"]),
                    "exposure_intercept_model_r2": float(corrections[ch]["r2"]),
                    "rmse": met["rmse"],
                    "mae": met["mae"],
                    "mape_percent": met["mape_percent"],
                    "r2": met["r2"],
                }
            )
    return pd.DataFrame(rows)


def write_comparison_report(
    output_dir: Path,
    comparison: pd.DataFrame,
    corrections: Dict[str, Dict[str, object]],
    diagnostics: pd.DataFrame,
    window_diagnostics: pd.DataFrame,
) -> None:
    lines = ["# Calibration Form Comparison", ""]
    lines.append("This run outputs both the standard exposure normalization and the residual-intercept corrected form.")
    lines.append("")
    lines.append("Standard:")
    lines.append("")
    lines.append("    X_c = (DN_c - Dark_c) / exposure_s")
    lines.append("")
    lines.append("Intercept corrected:")
    lines.append("")
    lines.append("    X_c = (DN_c - Dark_c - q_c) / exposure_s")
    lines.append("")
    lines.append("Estimated exposure residual intercepts:")
    lines.append("")
    for ch in CHANNELS:
        info = corrections[ch]
        lines.append(
            f"- {ch.upper()}: q_{ch} = {float(info['q_intercept']):.10g}, "
            f"exposure-linearity R2 = {float(info['r2']):.8f}"
        )
    lines.append("")
    lines.append("Metric comparison:")
    lines.append("")
    lines.append("| Channel | Form | q | RMSE | MAE | MAPE % | R2 |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for _, row in comparison.iterrows():
        lines.append(
            f"| {str(row['channel']).upper()} | {row['calibration_form']} | "
            f"{float(row['q_intercept']):.6g} | {float(row['rmse']):.6g} | "
            f"{float(row['mae']):.6g} | {float(row['mape_percent']):.4f} | "
            f"{float(row['r2']):.8f} |"
        )
    lines.append("")
    lines.append("Use the corrected form only when exposure-linearity diagnostics show that a residual intercept improves cross-exposure stability.")
    lines.append("")
    lines.append("Exposure linearity and X stability by group:")
    lines.append("")
    lines.append("The exposure-linearity R2 is computed from the direct model:")
    lines.append("")
    lines.append("    DN_corr_c(level, t) = S_c(level) * t + q_c")
    lines.append("")
    lines.append("A high R2 means DN_c - Dark_c is close to linear with exposure time. The X span columns show whether direct exposure division or residual-intercept correction is more stable across exposure times.")
    lines.append("")
    lines.append("| Channel | Group | linear R2 | q | standard X span % | corrected X span % |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for _, row in diagnostics.iterrows():
        lines.append(
            f"| {str(row['channel']).upper()} | {row['group']} | "
            f"{float(row['intercept_model_r2']):.8f} | "
            f"{float(row['q_intercept']):.6g} | "
            f"{float(row['standard_x_rel_span_percent']):.4f} | "
            f"{float(row['intercept_x_rel_span_percent']):.4f} |"
        )
    if not window_diagnostics.empty:
        lines.append("")
        lines.append("Exposure-window X stability:")
        lines.append("")
        lines.append("Each row uses data from min exposure through the maximum exposure in that group.")
        lines.append("")
        lines.append("| Channel | Group | Window ms | standard X span % | corrected X span % | improvement % |")
        lines.append("| --- | --- | --- | ---: | ---: | ---: |")
        for _, row in window_diagnostics.iterrows():
            lines.append(
                f"| {str(row['channel']).upper()} | {row['group']} | "
                f"{float(row['min_exposure_ms']):.6g}-{float(row['max_exposure_ms']):.6g} | "
                f"{float(row['standard_x_rel_span_percent']):.4f} | "
                f"{float(row['intercept_x_rel_span_percent']):.4f} | "
                f"{float(row['span_improvement_percent']):.4f} |"
            )
    (output_dir / "calibration_comparison.md").write_text("\n".join(lines), encoding="utf-8")


def plot_exposure_window_diagnostics(window_diagnostics: pd.DataFrame, output_dir: Path) -> None:
    if window_diagnostics.empty:
        return
    for group in sorted(window_diagnostics["group"].unique()):
        group_df = window_diagnostics[window_diagnostics["group"] == group]
        plt.figure(figsize=(8.4, 5.0))
        for ch, color in [("r", "red"), ("g", "green"), ("b", "blue")]:
            ch_df = group_df[group_df["channel"] == ch].sort_values("min_exposure_ms")
            if ch_df.empty:
                continue
            plt.plot(
                ch_df["min_exposure_ms"],
                ch_df["standard_x_rel_span_percent"],
                marker="o",
                linestyle="--",
                color=color,
                alpha=0.55,
                label=f"{ch.upper()} standard",
            )
            plt.plot(
                ch_df["min_exposure_ms"],
                ch_df["intercept_x_rel_span_percent"],
                marker="o",
                linestyle="-",
                color=color,
                label=f"{ch.upper()} corrected",
            )
        max_exposure = float(group_df["max_exposure_ms"].max())
        plt.xlabel(f"Minimum exposure included (ms), window ends at {max_exposure:.6g} ms")
        plt.ylabel("X relative span (%)")
        plt.title(f"Exposure-window X stability: {group}")
        plt.grid(True, alpha=0.25)
        plt.legend(ncol=2, fontsize=9)
        plt.tight_layout()
        safe_group = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(group))
        plt.savefig(output_dir / f"exposure_window_stability_{safe_group}.png", dpi=180)
        plt.close()

    summary = (
        window_diagnostics.groupby(["channel", "min_exposure_ms"], as_index=False)[
            ["standard_x_rel_span_percent", "intercept_x_rel_span_percent"]
        ]
        .mean()
        .sort_values(["channel", "min_exposure_ms"])
    )
    plt.figure(figsize=(8.4, 5.0))
    for ch, color in [("r", "red"), ("g", "green"), ("b", "blue")]:
        ch_df = summary[summary["channel"] == ch]
        if ch_df.empty:
            continue
        plt.plot(
            ch_df["min_exposure_ms"],
            ch_df["standard_x_rel_span_percent"],
            marker="o",
            linestyle="--",
            color=color,
            alpha=0.55,
            label=f"{ch.upper()} standard",
        )
        plt.plot(
            ch_df["min_exposure_ms"],
            ch_df["intercept_x_rel_span_percent"],
            marker="o",
            linestyle="-",
            color=color,
            label=f"{ch.upper()} corrected",
        )
    plt.xlabel("Minimum exposure included (ms)")
    plt.ylabel("Mean X relative span across groups (%)")
    plt.title("Exposure-window X stability summary")
    plt.grid(True, alpha=0.25)
    plt.legend(ncol=2, fontsize=9)
    plt.tight_layout()
    plt.savefig(output_dir / "exposure_window_stability_summary.png", dpi=180)
    plt.close()


def write_template(path: Path) -> None:
    template = pd.DataFrame(
        [
            {
                "level": "L1",
                "exposure_ms": 1.0,
                "dn_r": 1200,
                "dn_g": 1500,
                "dn_b": 900,
                "dark_r": 64,
                "dark_g": 62,
                "dark_b": 65,
                "l_eff_r": 1.23,
                "l_eff_g": 1.18,
                "l_eff_b": 0.95,
                "spectrum_file": "",
            }
        ]
    )
    template.to_csv(path, index=False, encoding="utf-8-sig")


def write_blackbody_template(path: Path) -> None:
    template = pd.DataFrame(
        [
            {
                "level": "T900",
                "blackbody_temp_c": 900.0,
                "emissivity": 1.0,
                "exposure_ms": 1.0,
                "dn_r": 900,
                "dn_g": 650,
                "dn_b": 260,
                "dark_r": 64,
                "dark_g": 62,
                "dark_b": 65,
            },
            {
                "level": "T1000",
                "blackbody_temp_c": 1000.0,
                "emissivity": 1.0,
                "exposure_ms": 1.0,
                "dn_r": 1600,
                "dn_g": 1050,
                "dn_b": 380,
                "dark_r": 64,
                "dark_g": 62,
                "dark_b": 65,
            },
            {
                "level": "T1100",
                "blackbody_temp_c": 1100.0,
                "emissivity": 1.0,
                "exposure_ms": 1.0,
                "dn_r": 2500,
                "dn_g": 1600,
                "dn_b": 560,
                "dark_r": 64,
                "dark_g": 62,
                "dark_b": 65,
            },
        ]
    )
    template.to_csv(path, index=False, encoding="utf-8-sig")


def add_image_folder_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--image-dir", type=Path, help="folder containing blackbody light raw images")
    parser.add_argument("--dark-dir", type=Path, help="optional folder containing dark raw images")
    parser.add_argument(
        "--input-format",
        choices=["raw", "bmp"],
        default="raw",
        help="image data source to average in image-folder mode: raw or bmp; default raw",
    )
    parser.add_argument("--roi", type=image_measurements.parse_roi, help="ROI as x,y,width,height")
    parser.add_argument(
        "--roi-mode",
        choices=["manual", "auto-anchor"],
        default="manual",
        help="manual uses --roi; auto-anchor detects a stable hotspot anchor from high-temperature BMP previews",
    )
    parser.add_argument(
        "--anchor-temperatures",
        type=image_measurements.parse_float_list,
        default=image_measurements.parse_float_list("1400,1500"),
        help="comma-separated temperatures used to detect the auto ROI anchor, default 1400,1500",
    )
    parser.add_argument("--roi-size", type=int, default=120, help="square ROI size for --roi-mode auto-anchor")
    parser.add_argument("--search-radius", type=int, default=220, help="local auto ROI search radius around the anchor")
    parser.add_argument("--max-roi-shift", type=float, default=180.0, help="maximum local ROI center shift before anchor fallback")
    parser.add_argument("--anchor-min-confidence", type=float, default=0.45, help="minimum confidence for anchor detections")
    parser.add_argument("--local-min-confidence", type=float, default=0.35, help="minimum confidence for local ROI refinement")
    parser.add_argument("--roi-audit-csv", type=Path, help="optional CSV listing ROI used for every image")
    parser.add_argument("--raw-width", type=int, help="raw image width in pixels")
    parser.add_argument("--raw-height", type=int, help="raw image height in pixels")
    parser.add_argument("--raw-dtype", default="uint16", help="raw sample dtype, for example uint8 or uint16")
    parser.add_argument("--raw-channels", type=int, default=3, help="number of channels per pixel; use 1 for Bayer raw")
    parser.add_argument(
        "--raw-channel-order",
        type=image_measurements.parse_channel_order,
        default=image_measurements.parse_channel_order("rgb"),
        help="raw channel order, rgb or bgr",
    )
    parser.add_argument("--raw-format", choices=["rgb", "bayer"], default="rgb", help="raw color format")
    parser.add_argument(
        "--bayer-pattern",
        type=image_measurements.parse_bayer_pattern,
        default=image_measurements.parse_bayer_pattern("gbrg"),
        help="Bayer pattern for --raw-format bayer",
    )
    parser.add_argument("--raw-byte-order", choices=["native", "little", "big"], default="native")
    parser.add_argument("--raw-ext", default=".raw", help="raw file extension, default .raw")
    parser.add_argument(
        "--include-conditions-csv",
        type=Path,
        help="optional CSV with temperature_c/exposure_ms rows to include in image-folder mode",
    )
    parser.add_argument(
        "--expected-repeats",
        type=image_measurements.parse_repeat_list,
        default=image_measurements.parse_repeat_list("1,2,3"),
        help="comma-separated expected repeat numbers; missing ones are reported, default 1,2,3",
    )
    parser.add_argument(
        "--min-repeats",
        type=int,
        default=2,
        help="minimum available repeats required per temperature/exposure group, default 2",
    )
    parser.add_argument(
        "--missing-repeat-policy",
        choices=["warn", "error", "ignore"],
        default="warn",
        help="what to do when expected repeats are missing; default warn",
    )
    parser.add_argument("--repeat-audit-csv", type=Path, help="optional CSV listing present and missing repeats")
    parser.add_argument("--emissivity", type=float, default=1.0, help="blackbody emissivity for image-folder mode")
    parser.add_argument(
        "--bad-pixel-policy",
        choices=["none", "exclude"],
        default="none",
        help="exclude uses dark RAW frames to mask bad pixels before ROI averaging; default none",
    )
    parser.add_argument(
        "--bad-pixel-threshold",
        type=float,
        default=8192.0,
        help="dark RAW median threshold for marking bad pixels, default 8192",
    )
    parser.add_argument("--bad-pixel-audit-csv", type=Path, help="optional CSV listing bad-pixel counts by exposure")
    parser.add_argument("--dark-r", type=float, help="constant dark R value")
    parser.add_argument("--dark-g", type=float, help="constant dark G value")
    parser.add_argument("--dark-b", type=float, help="constant dark B value")
    parser.add_argument("--use-zero-dark", action="store_true", help="use zero dark values in image-folder mode")
    parser.add_argument(
        "--write-generated-measurements",
        type=Path,
        help="optional path to save the measurement CSV generated from --image-dir",
    )
    parser.add_argument(
        "--exclude-exposure-ms",
        type=image_measurements.parse_float_list,
        default=(),
        help="comma-separated exposure times to exclude from fitting, for example 0.2",
    )
    parser.add_argument(
        "--outlier-policy",
        choices=["none", "warn", "exclude"],
        default="none",
        help="detect within-temperature exposure-consistency outliers; exclude removes them before final fitting",
    )
    parser.add_argument(
        "--outlier-threshold",
        type=float,
        default=6.0,
        help="robust residual z-score threshold for outlier detection, default 6.0",
    )
    parser.add_argument(
        "--outlier-min-channels",
        type=int,
        default=2,
        help="minimum flagged channels required to mark a measurement row as an outlier, default 2",
    )


def validate_image_folder_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.outlier_threshold <= 0:
        parser.error("--outlier-threshold must be positive")
    if args.outlier_min_channels <= 0 or args.outlier_min_channels > len(CHANNELS):
        parser.error("--outlier-min-channels must be between 1 and 3")

    if args.image_dir is None:
        return

    if args.measurements is not None:
        parser.error("choose either --measurements or --image-dir, not both")
    if args.roi_mode == "manual" and args.roi is None:
        parser.error("--roi is required when using --image-dir")
    if args.input_format == "raw" and (args.raw_width is None or args.raw_height is None):
        parser.error("--raw-width and --raw-height are required for raw input")
    if args.raw_width is not None and args.raw_width <= 0:
        parser.error("--raw-width must be positive")
    if args.raw_height is not None and args.raw_height <= 0:
        parser.error("--raw-height must be positive")
    if args.roi_mode == "auto-anchor":
        if args.roi_size <= 0:
            parser.error("--roi-size must be positive")
        if args.raw_width is not None and args.raw_height is not None and args.roi_size > min(args.raw_width, args.raw_height):
            parser.error("--roi-size must fit within the image")
        if args.search_radius <= 0:
            parser.error("--search-radius must be positive")
        if args.max_roi_shift < 0:
            parser.error("--max-roi-shift must be non-negative")
    if args.input_format == "bmp" and args.raw_format == "bayer":
        parser.error("--raw-format bayer is only valid for raw input")
    if args.raw_format == "rgb" and args.raw_channels < 3:
        parser.error("--raw-channels must be at least 3 for RGB mode")
    if args.raw_format == "bayer" and args.raw_channels != 1:
        parser.error("--raw-format bayer requires --raw-channels 1")
    if args.min_repeats <= 0:
        parser.error("--min-repeats must be positive")
    if args.bad_pixel_threshold <= 0:
        parser.error("--bad-pixel-threshold must be positive")
    if args.bad_pixel_policy != "none" and args.dark_dir is None:
        parser.error("--bad-pixel-policy requires --dark-dir")

    constant_dark_values = [args.dark_r, args.dark_g, args.dark_b]
    has_partial_constant_dark = any(v is not None for v in constant_dark_values) and not all(
        v is not None for v in constant_dark_values
    )
    if has_partial_constant_dark:
        parser.error("--dark-r, --dark-g, and --dark-b must be provided together")

    dark_source_count = int(args.dark_dir is not None) + int(all(v is not None for v in constant_dark_values)) + int(
        args.use_zero_dark
    )
    if dark_source_count == 0:
        parser.error("provide --dark-dir, constant --dark-r/--dark-g/--dark-b, or --use-zero-dark with --image-dir")
    if dark_source_count > 1:
        parser.error("choose only one dark source: --dark-dir, constant dark values, or --use-zero-dark")


def image_folder_args_for_converter(args: argparse.Namespace) -> argparse.Namespace:
    return argparse.Namespace(
        image_dir=args.image_dir,
        input_format=args.input_format,
        roi=args.roi,
        roi_mode=args.roi_mode,
        anchor_temperatures=args.anchor_temperatures,
        roi_size=args.roi_size,
        search_radius=args.search_radius,
        max_roi_shift=args.max_roi_shift,
        anchor_min_confidence=args.anchor_min_confidence,
        local_min_confidence=args.local_min_confidence,
        roi_audit_csv=args.roi_audit_csv,
        width=args.raw_width,
        height=args.raw_height,
        dtype=args.raw_dtype,
        channels=args.raw_channels,
        channel_order=args.raw_channel_order,
        raw_format=args.raw_format,
        bayer_pattern=args.bayer_pattern,
        byte_order=args.raw_byte_order,
        raw_ext=args.raw_ext,
        include_conditions_csv=args.include_conditions_csv,
        expected_repeats=args.expected_repeats,
        min_repeats=args.min_repeats,
        missing_repeat_policy=args.missing_repeat_policy,
        repeat_audit_csv=args.repeat_audit_csv,
        emissivity=args.emissivity,
        bad_pixel_policy=args.bad_pixel_policy,
        bad_pixel_threshold=args.bad_pixel_threshold,
        bad_pixel_audit_csv=args.bad_pixel_audit_csv,
        dark_dir=args.dark_dir,
        dark_r=args.dark_r,
        dark_g=args.dark_g,
        dark_b=args.dark_b,
        use_zero_dark=args.use_zero_dark,
    )


def load_measurements(args: argparse.Namespace) -> tuple[pd.DataFrame, Path]:
    if args.image_dir is None:
        measurements = pd.read_csv(args.measurements)
        return filter_measurements(measurements, args), args.measurements

    rows = image_measurements.build_rows_from_image_folder(image_folder_args_for_converter(args))
    measurements = filter_measurements(pd.DataFrame(rows), args)
    measurement_path = args.write_generated_measurements or (args.output_dir / "generated_blackbody_measurements.csv")
    measurement_path.parent.mkdir(parents=True, exist_ok=True)
    measurements.to_csv(measurement_path, index=False, encoding="utf-8-sig")
    print(f"Wrote generated measurements: {measurement_path}")
    return measurements, measurement_path


def filter_measurements(measurements: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    df = measurements.copy()
    if args.exclude_exposure_ms:
        if "exposure_ms" not in df.columns:
            raise ValueError("--exclude-exposure-ms requires exposure_ms in the measurement table")
        exposure = df["exposure_ms"].to_numpy(float)
        keep = np.ones(len(df), dtype=bool)
        for excluded in args.exclude_exposure_ms:
            keep &= np.abs(exposure - float(excluded)) > image_measurements.EXPOSURE_MATCH_TOLERANCE_MS
        excluded_count = int((~keep).sum())
        if excluded_count:
            print(f"Excluded {excluded_count} measurement row(s) by --exclude-exposure-ms")
        df = df.loc[keep].copy()
    return df


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RGB camera radiometric calibration")
    parser.add_argument("--measurements", type=Path, help="measurement CSV")
    parser.add_argument("--spectral-response", type=Path, required=False, help="camera spectral response CSV")
    parser.add_argument("--filter-csv", type=Path, default=None, help="filter transmission CSV with wavelength_nm,transmission")
    parser.add_argument("--cutoff-nm", type=float, default=650.0, help="ideal short-pass cutoff when --filter-csv is not provided")
    parser.add_argument("--reference-kind", choices=["normalized", "integrated"], default="normalized")
    parser.add_argument("--model", choices=["linear", "poly", "logpoly"], default="linear")
    parser.add_argument("--degree", type=int, default=2, help="degree for poly/logpoly")
    parser.add_argument("--output-dir", type=Path, default=Path("calibration_output"))
    parser.add_argument("--write-template", type=Path, help="write a measurement CSV template and exit")
    parser.add_argument("--write-blackbody-template", type=Path, help="write a blackbody measurement CSV template and exit")
    add_image_folder_args(parser)
    args = parser.parse_args()
    validate_image_folder_args(parser, args)
    return args


def main() -> None:
    args = parse_args()

    if args.write_template:
        write_template(args.write_template)
        print(f"Wrote template: {args.write_template}")
        return

    if args.write_blackbody_template:
        write_blackbody_template(args.write_blackbody_template)
        print(f"Wrote blackbody template: {args.write_blackbody_template}")
        return

    if args.spectral_response is None or (args.measurements is None and args.image_dir is None):
        raise SystemExit(
            "--spectral-response and either --measurements or --image-dir are required unless --write-template is used"
        )

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    response = load_spectral_response(args.spectral_response)
    eff = build_effective_response(response, args.filter_csv, args.cutoff_nm)
    eff.to_csv(output_dir / "effective_response.csv", index=False, encoding="utf-8-sig")
    plot_effective_response(eff, output_dir)

    measurements, measurement_path = load_measurements(args)
    fit_table = prepare_fit_table(measurements, measurement_path, eff, args.reference_kind)
    fit_table, exposure_corrections = add_intercept_corrected_x(fit_table)
    if args.outlier_policy != "none":
        outlier_report = detect_outliers(
            fit_table,
            "x_intercept_",
            args.model,
            args.degree,
            args.outlier_threshold,
            args.outlier_min_channels,
        )
        outlier_report.to_csv(output_dir / "outlier_diagnostics.csv", index=False, encoding="utf-8-sig")
        outlier_mask = outlier_report["is_outlier"].to_numpy(bool)
        outlier_count = int(outlier_mask.sum())
        print(f"Outlier detection flagged {outlier_count} measurement row(s).")
        if args.outlier_policy == "exclude" and outlier_count:
            fit_table.to_csv(output_dir / "calibration_fit_table_before_outlier_exclusion.csv", index=False, encoding="utf-8-sig")
            fit_table = fit_table.loc[~outlier_mask].copy()
            fit_table, exposure_corrections = add_intercept_corrected_x(fit_table)
            print(f"Excluded {outlier_count} outlier row(s) before final fitting.")
    fit_table.to_csv(output_dir / "calibration_fit_table.csv", index=False, encoding="utf-8-sig")

    diagnostics = exposure_stability_diagnostics(fit_table, exposure_corrections)
    diagnostics.to_csv(output_dir / "exposure_linearity_diagnostics.csv", index=False, encoding="utf-8-sig")
    window_diagnostics = exposure_window_diagnostics(fit_table)
    window_diagnostics.to_csv(output_dir / "exposure_window_diagnostics.csv", index=False, encoding="utf-8-sig")
    plot_exposure_window_diagnostics(window_diagnostics, output_dir)
    (output_dir / "exposure_intercept_correction.json").write_text(
        json.dumps(exposure_corrections, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    standard_results = fit_all_channels(
        fit_table,
        "x_",
        args.model,
        args.degree,
        output_dir,
        filename_suffix="",
        x_label_template="X_{ch} = (DN_{ch} - Dark_{ch}) / exposure_s",
    )
    intercept_results = fit_all_channels(
        fit_table,
        "x_intercept_",
        args.model,
        args.degree,
        output_dir,
        filename_suffix="_intercept",
        x_label_template="X_{ch} = (DN_{ch} - Dark_{ch} - q_{ch}) / exposure_s",
    )

    standard_serializable = results_to_serializable(standard_results)
    intercept_serializable = results_to_serializable(intercept_results)
    for ch in CHANNELS:
        intercept_serializable[ch]["exposure_intercept_q"] = float(exposure_corrections[ch]["q_intercept"])
        intercept_serializable[ch]["x_definition"] = f"X_{ch} = (DN_{ch} - Dark_{ch} - q_{ch}) / exposure_s"

    (output_dir / "calibration_coefficients.json").write_text(
        json.dumps(standard_serializable, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "calibration_coefficients_standard.json").write_text(
        json.dumps(standard_serializable, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    (output_dir / "calibration_coefficients_intercept_corrected.json").write_text(
        json.dumps(intercept_serializable, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    comparison = build_calibration_comparison(standard_results, intercept_results, exposure_corrections)
    comparison.to_csv(output_dir / "calibration_comparison.csv", index=False, encoding="utf-8-sig")

    write_report(
        standard_results,
        output_dir,
        "calibration_report_standard.md",
        "RGB Radiometric Calibration Result - Standard",
    )
    write_report(
        intercept_results,
        output_dir,
        "calibration_report_intercept_corrected.md",
        "RGB Radiometric Calibration Result - Intercept Corrected",
        extra_lines=[
            "Exposure residual intercept correction:",
            "",
            "    X_c = (DN_c - Dark_c - q_c) / exposure_s",
        ],
    )
    write_comparison_report(output_dir, comparison, exposure_corrections, diagnostics, window_diagnostics)
    write_report(
        standard_results,
        output_dir,
        "calibration_report.md",
        "RGB Radiometric Calibration Result - Standard",
        extra_lines=[
            "This run also generated intercept-corrected calibration outputs.",
            "See calibration_report_intercept_corrected.md and calibration_comparison.md.",
        ],
    )

    print(f"Done. Output directory: {output_dir.resolve()}")
    for ch in CHANNELS:
        q = float(exposure_corrections[ch]["q_intercept"])
        print(f"{ch.upper()} exposure intercept q_{ch}: {q:.10g}")
        print("Standard: " + format_equation(ch, standard_results[ch]["fit"]))
        print("Intercept corrected: " + format_equation(ch, intercept_results[ch]["fit"]))


if __name__ == "__main__":
    main()

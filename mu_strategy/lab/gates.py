"""Phase 2 6-Gate quantitative validation and Bailey & Lopez de Prado (2014) DSR."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Literal

__all__ = [
    "GateCheck",
    "GateConfig",
    "GateStatus",
    "SixGateEvaluation",
    "Verdict",
    "compute_deflated_sharpe_ratio",
    "evaluate_six_gates",
]

GateStatus = Literal["PASS", "WARN", "FAIL"]
Verdict = Literal["GO", "CONDITIONAL", "NO_GO"]

_EULER_MASCHERONI: float = 0.5772156649015329
_SQRT2: float = math.sqrt(2.0)
_SQRT_2PI: float = math.sqrt(2.0 * math.pi)


def _is_finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _check_float(value: Any, name: str, *, lo: float = 0.0, hi: float | None = None, strict_lo: bool = False) -> float:
    if not _is_finite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    v = float(value)
    if (v <= lo if strict_lo else v < lo) or (hi is not None and v > hi):
        raise ValueError(f"{name} out of valid range, got {value!r}")
    return v


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / _SQRT2))


def _norm_ppf(p: float) -> float:
    """Inverse standard normal CDF via Acklam rational approximation + Halley step."""
    if not (0.0 < p < 1.0):
        raise ValueError(f"p must be in (0, 1), got {p!r}")
    a = (-39.69683028665376, 220.9460984245205, -275.9285104469687, 138.357751867269, -30.66479806614716, 2.506628277459239)
    b = (-54.47609879822406, 161.5858368580409, -155.6989798598866, 66.80131188771972, -13.28068155288572)
    c = (-7.784894002430293e-3, -0.3223964580411365, -2.400758277161838, -2.549732539343734, 4.374664141464968, 2.938163982698783)
    d = (7.784695709041462e-3, 0.3224671290700398, 2.445134137142996, 3.754408661907416)
    if p < 0.02425:
        q = math.sqrt(-2.0 * math.log(p))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    elif p <= 0.97575:
        q = p - 0.5
        r = q * q
        x = (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - p))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0)
    pdf = math.exp(-0.5 * x * x) / _SQRT_2PI
    if pdf > 0.0:
        u = (_norm_cdf(x) - p) / pdf
        x -= u / (1.0 + 0.5 * x * u)
    return x


def compute_deflated_sharpe_ratio(
    sharpe: float,
    n_obs: int,
    skew: float,
    excess_kurtosis: float,
    n_trials: int,
    annualization_factor: float = 8760.0,
) -> float:
    """Compute Bailey & Lopez de Prado (2014) Deflated Sharpe Ratio (DSR) in [0, 1].

    Fail-closed: returns ``0.0`` on non-finite/invalid inputs or degenerate samples
    (``n_obs < 3`` or ``n_trials < 1``).
    """
    if not (_is_finite(sharpe) and _is_finite(skew) and _is_finite(excess_kurtosis) and _is_finite(annualization_factor)):
        return 0.0
    if isinstance(n_obs, bool) or not isinstance(n_obs, int) or n_obs < 3:
        return 0.0
    if isinstance(n_trials, bool) or not isinstance(n_trials, int) or n_trials < 1:
        return 0.0
    ann = float(annualization_factor)
    if ann <= 0.0:
        return 0.0

    sr_hat = float(sharpe) / math.sqrt(ann)
    sqrt_t = math.sqrt(float(n_obs - 1))
    if n_trials == 1:
        sr_zero = 0.0
    else:
        nf = float(n_trials)
        p1 = min(1.0 - 1e-15, max(1e-15, 1.0 - (1.0 / nf)))
        p2 = min(1.0 - 1e-15, max(1e-15, 1.0 - (1.0 / (nf * math.e))))
        sr_zero = (((1.0 - _EULER_MASCHERONI) * _norm_ppf(p1)) + (_EULER_MASCHERONI * _norm_ppf(p2))) / sqrt_t

    # Raw kurtosis gamma_4 = excess_kurtosis + 3 => (gamma_4 - 1) / 4 = (excess_kurtosis + 2) / 4.
    var_num = 1.0 - (float(skew) * sr_hat) + (((float(excess_kurtosis) + 2.0) / 4.0) * sr_hat * sr_hat)
    if not math.isfinite(var_num) or var_num <= 0.0:
        return 0.0
    z = ((sr_hat - sr_zero) * sqrt_t) / math.sqrt(var_num)
    return min(1.0, max(0.0, _norm_cdf(z))) if math.isfinite(z) else 0.0


@dataclass(frozen=True, slots=True)
class GateConfig:
    """Thresholds governing the Phase 2 6-Gate quantitative Go/No-Go evaluation."""

    min_oos_days: float = 60.0
    min_oos_trades: int = 15
    min_oos_sharpe: float = 0.50
    min_dsr_go: float = 0.95
    min_dsr_warn: float = 0.80
    min_positive_fold_ratio: float = 0.60
    min_is_oos_retention: float = 0.50
    max_drawdown: float = 0.25
    min_calmar: float = 0.50
    stress_apr: float = 0.20
    spot_dd_reduction_ratio: float = 0.65
    min_neighbor_pass_ratio: float = 0.50
    min_neighbor_soft_ratio: float = 0.35

    def __post_init__(self) -> None:
        if isinstance(self.min_oos_trades, bool) or not isinstance(self.min_oos_trades, int) or self.min_oos_trades < 0:
            raise ValueError(f"min_oos_trades must be a non-negative int, got {self.min_oos_trades!r}")
        object.__setattr__(self, "min_oos_days", _check_float(self.min_oos_days, "min_oos_days"))
        object.__setattr__(self, "min_oos_sharpe", _check_float(self.min_oos_sharpe, "min_oos_sharpe"))
        dsr_go = _check_float(self.min_dsr_go, "min_dsr_go", hi=1.0)
        dsr_warn = _check_float(self.min_dsr_warn, "min_dsr_warn", hi=1.0)
        if dsr_warn > dsr_go:
            raise ValueError("min_dsr_warn must be <= min_dsr_go")
        object.__setattr__(self, "min_dsr_go", dsr_go)
        object.__setattr__(self, "min_dsr_warn", dsr_warn)
        object.__setattr__(self, "min_positive_fold_ratio", _check_float(self.min_positive_fold_ratio, "min_positive_fold_ratio", hi=1.0))
        object.__setattr__(self, "min_is_oos_retention", _check_float(self.min_is_oos_retention, "min_is_oos_retention"))
        object.__setattr__(self, "max_drawdown", _check_float(self.max_drawdown, "max_drawdown", hi=1.0))
        object.__setattr__(self, "min_calmar", _check_float(self.min_calmar, "min_calmar"))
        object.__setattr__(self, "stress_apr", _check_float(self.stress_apr, "stress_apr", strict_lo=True))
        object.__setattr__(self, "spot_dd_reduction_ratio", _check_float(self.spot_dd_reduction_ratio, "spot_dd_reduction_ratio", hi=1.0))
        nbr_pass = _check_float(self.min_neighbor_pass_ratio, "min_neighbor_pass_ratio", hi=1.0)
        nbr_soft = _check_float(self.min_neighbor_soft_ratio, "min_neighbor_soft_ratio", hi=1.0)
        if nbr_soft > nbr_pass:
            raise ValueError("min_neighbor_soft_ratio must be <= min_neighbor_pass_ratio")
        object.__setattr__(self, "min_neighbor_pass_ratio", nbr_pass)
        object.__setattr__(self, "min_neighbor_soft_ratio", nbr_soft)


@dataclass(frozen=True, slots=True)
class GateCheck:
    """Single quantitative gate outcome."""

    gate_id: str
    name: str
    status: GateStatus
    value: float | str | None
    threshold: float | str | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        val = None if (isinstance(self.value, float) and not math.isfinite(self.value)) else self.value
        thr = None if (isinstance(self.threshold, float) and not math.isfinite(self.threshold)) else self.threshold
        return {"gate_id": self.gate_id, "name": self.name, "status": self.status, "value": val, "threshold": thr, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class SixGateEvaluation:
    """Full 6-Gate verdict and per-gate diagnostic breakdown."""

    verdict: Verdict
    passed_count: int
    checks: tuple[GateCheck, ...]
    dsr: float
    failed_gates: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "passed_count": self.passed_count,
            "total_gates": len(self.checks),
            "dsr": self.dsr if math.isfinite(self.dsr) else 0.0,
            "failed_gates": list(self.failed_gates),
            "checks": [check.to_dict() for check in self.checks],
        }


def evaluate_six_gates(
    *,
    oos_days: float,
    oos_trades: int,
    oos_total_return: float,
    oos_sharpe: float,
    oos_max_drawdown: float,
    oos_calmar: float,
    liquidations: int,
    n_obs: int,
    return_skew: float,
    return_excess_kurtosis: float,
    n_trials: int,
    positive_fold_ratio: float,
    is_sharpe: float | None = None,
    is_perp: bool = True,
    funding_verified: bool = True,
    stress_passed: bool = True,
    stress_liquidations: int = 0,
    spot_oos_sharpe: float = 0.0,
    spot_oos_max_drawdown: float = 0.0,
    neighbor_pass_ratio: float = 1.0,
    annualization_factor: float = 8760.0,
    config: GateConfig | None = None,
) -> SixGateEvaluation:
    """Evaluate all 6 Phase 2 quantitative gates and derive GO / CONDITIONAL / NO_GO."""
    cfg = config if config is not None else GateConfig()

    # G1: Sample Sufficiency (Hard)
    days_ok = _is_finite(oos_days) and float(oos_days) >= cfg.min_oos_days
    trades_ok = (not isinstance(oos_trades, bool)) and isinstance(oos_trades, int) and oos_trades >= cfg.min_oos_trades
    g1_status: GateStatus = "PASS" if (days_ok and trades_ok) else "FAIL"
    g1 = GateCheck(
        "G1", "Sample Sufficiency", g1_status,
        f"{float(oos_days):.1f}d / {oos_trades}t" if _is_finite(oos_days) else "invalid",
        f">={cfg.min_oos_days:.0f}d & >={cfg.min_oos_trades}t",
        f"OOS {float(oos_days):.1f}d (min {cfg.min_oos_days:.0f}d), {oos_trades} trades (min {cfg.min_oos_trades})" if _is_finite(oos_days) else "Non-finite OOS duration",
    )

    # G2: Net-of-Cost & Real-Funding Survival (Hard)
    ret_ok, sh_ok = _is_finite(oos_total_return), _is_finite(oos_sharpe)
    funding_ok = (not is_perp) or bool(funding_verified) or bool(stress_passed)
    g2_pass = ret_ok and sh_ok and float(oos_total_return) > 0.0 and float(oos_sharpe) >= cfg.min_oos_sharpe and funding_ok
    g2_status: GateStatus = "PASS" if g2_pass else "FAIL"
    g2 = GateCheck(
        "G2", "Net-of-Cost & Funding Survival", g2_status,
        round(float(oos_sharpe), 4) if sh_ok else None,
        cfg.min_oos_sharpe,
        (
            f"Return {float(oos_total_return) * 100.0:+.2f}%, Sharpe {float(oos_sharpe):.2f}, "
            f"funding={'verified' if funding_verified else ('stress-ok' if stress_passed else 'unverified')}"
            if (ret_ok and sh_ok) else "Non-finite return or Sharpe"
        ),
    )

    # G3: Multiple-Testing DSR & Walk-Forward Consistency (Soft WARN -> CONDITIONAL)
    dsr = compute_deflated_sharpe_ratio(oos_sharpe, n_obs, return_skew, return_excess_kurtosis, n_trials, annualization_factor)
    folds_valid = _is_finite(positive_fold_ratio)
    folds_ok = folds_valid and float(positive_fold_ratio) >= cfg.min_positive_fold_ratio
    retention: float | None = None
    ret_pass = ret_warn = True
    if is_sharpe is not None and _is_finite(is_sharpe) and float(is_sharpe) > 0.0:
        retention = (float(oos_sharpe) / float(is_sharpe)) if sh_ok else 0.0
        ret_pass = retention >= cfg.min_is_oos_retention
        ret_warn = retention >= (cfg.min_is_oos_retention * 0.5)
    if dsr >= cfg.min_dsr_go and folds_ok and ret_pass:
        g3_status: GateStatus = "PASS"
    elif dsr >= cfg.min_dsr_warn and folds_ok and ret_warn:
        g3_status = "WARN"
    else:
        g3_status = "FAIL"
    ret_txt = f", IS->OOS {retention:.2f}x" if retention is not None else ""
    g3 = GateCheck(
        "G3", "Multiple-Testing DSR & WF Consistency", g3_status,
        round(dsr, 4), cfg.min_dsr_go,
        f"DSR {dsr:.3f} (N={n_trials}), pos folds {float(positive_fold_ratio) * 100.0:.0f}%{ret_txt}" if folds_valid else "Invalid fold ratio",
    )

    # G4: Tail Risk & Liquidation (Hard)
    dd_ok, cal_ok = _is_finite(oos_max_drawdown), _is_finite(oos_calmar)
    dd_mag = abs(float(oos_max_drawdown)) if dd_ok else math.inf
    liq_ok = (not isinstance(liquidations, bool)) and isinstance(liquidations, int) and liquidations == 0
    g4_status: GateStatus = "PASS" if (liq_ok and dd_ok and dd_mag <= cfg.max_drawdown and cal_ok and float(oos_calmar) >= cfg.min_calmar) else "FAIL"
    g4 = GateCheck(
        "G4", "Tail Risk & Liquidation", g4_status,
        round(dd_mag, 4) if dd_ok else None, cfg.max_drawdown,
        f"Liq={liquidations}, MaxDD={dd_mag * 100.0:.2f}% (max {cfg.max_drawdown * 100.0:.0f}%), Calmar={float(oos_calmar):.2f}" if (dd_ok and cal_ok) else "Non-finite DD/Calmar",
    )

    # G5: Stress & Spot Benchmark Value-Add (Stress=Hard, Spot Benchmark=Soft WARN)
    stress_zero_liq = (not isinstance(stress_liquidations, bool)) and isinstance(stress_liquidations, int) and stress_liquidations == 0
    stress_ok = bool(stress_passed) and stress_zero_liq
    sp_sh_ok, sp_dd_ok = _is_finite(spot_oos_sharpe), _is_finite(spot_oos_max_drawdown)
    spot_dd_mag = abs(float(spot_oos_max_drawdown)) if sp_dd_ok else 0.0
    beats_sharpe = sh_ok and sp_sh_ok and float(oos_sharpe) >= float(spot_oos_sharpe)
    cuts_dd = ret_ok and dd_ok and sp_dd_ok and float(oos_total_return) > 0.0 and dd_mag <= (spot_dd_mag * cfg.spot_dd_reduction_ratio)
    g5_status: GateStatus = "FAIL" if not stress_ok else ("PASS" if (beats_sharpe or cuts_dd) else "WARN")
    g5 = GateCheck(
        "G5", "Stress & Spot Benchmark Value-Add", g5_status,
        round(float(oos_sharpe) - float(spot_oos_sharpe), 4) if (sh_ok and sp_sh_ok) else None,
        f"stress +/-{cfg.stress_apr * 100.0:.0f}% APR & beat Spot",
        (
            f"Stress={'OK' if stress_ok else 'FAIL'} (liq={stress_liquidations}), "
            f"Sharpe {float(oos_sharpe):.2f} vs Spot {float(spot_oos_sharpe):.2f}, "
            f"DD {dd_mag * 100.0:.1f}% vs {cfg.spot_dd_reduction_ratio:.2f}x Spot ({spot_dd_mag * cfg.spot_dd_reduction_ratio * 100.0:.1f}%)"
            if (sh_ok and sp_sh_ok and dd_ok and sp_dd_ok) else "Non-finite benchmark/stress metrics"
        ),
    )

    # G6: Neighbor Parameter Stability (Soft fail >= 0.35 eligible for CONDITIONAL)
    nbr_ok = _is_finite(neighbor_pass_ratio)
    nbr_val = float(neighbor_pass_ratio) if nbr_ok else 0.0
    g6_status: GateStatus = "PASS" if (nbr_ok and nbr_val >= cfg.min_neighbor_pass_ratio) else "FAIL"
    g6 = GateCheck(
        "G6", "Neighbor Parameter Stability", g6_status,
        round(nbr_val, 4) if nbr_ok else None, cfg.min_neighbor_pass_ratio,
        f"1-step grid neighbor pass ratio {nbr_val * 100.0:.0f}% (pass>={cfg.min_neighbor_pass_ratio * 100.0:.0f}%, soft>={cfg.min_neighbor_soft_ratio * 100.0:.0f}%)",
    )

    checks = (g1, g2, g3, g4, g5, g6)
    passed_count = sum(1 for c in checks if c.status == "PASS")
    failed_gates = tuple(c.gate_id for c in checks if c.status == "FAIL")
    hard_pass = g1_status == "PASS" and g2_status == "PASS" and g4_status == "PASS" and stress_ok
    soft_ok = (
        g3_status in ("PASS", "WARN")
        and g5_status in ("PASS", "WARN")
        and (g6_status in ("PASS", "WARN") or (nbr_ok and nbr_val >= cfg.min_neighbor_soft_ratio))
    )
    verdict: Verdict = "GO" if passed_count == len(checks) else ("CONDITIONAL" if (hard_pass and soft_ok) else "NO_GO")
    return SixGateEvaluation(verdict=verdict, passed_count=passed_count, checks=checks, dsr=dsr, failed_gates=failed_gates)

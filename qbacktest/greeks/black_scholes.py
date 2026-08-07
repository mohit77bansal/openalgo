"""Black-Scholes pricing and Greeks for European options.

Notation:
    S       = spot price of underlying
    K       = strike price
    t       = time to expiry in YEARS (e.g. 7 days = 7/365)
    r       = continuously compounded risk-free rate (decimal)
    q       = continuous dividend yield (decimal); for indices ~0; for cash equity options, use yield estimate
    sigma   = volatility (annualized, decimal, e.g. 0.18 for 18%)

For Black 76 (futures options): pass S = futures price, q = r so that the discount
factor cancels and the formula reduces to the Black 76 form.

All functions handle scalar AND vectorized inputs. Vectorized variants are suffixed
with `_v` and operate on numpy arrays.

Tested against py_vollib reference values to 1e-8 tolerance (see tests/).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import numpy as np
from scipy.stats import norm

OptionFlag = Literal["c", "p", "C", "P", "CE", "PE", "call", "put"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flag(option_type) -> int:  # noqa: ANN001
    """Normalize option type to +1 (call) or -1 (put). Accepts strings or +/-1 ints."""
    if isinstance(option_type, (int, np.integer)):
        if int(option_type) == 1:
            return 1
        if int(option_type) == -1:
            return -1
        raise ValueError(f"Numeric option_type must be +1 or -1, got {option_type!r}")
    s = str(option_type).upper().strip()
    if s in ("C", "CE", "CALL"):
        return 1
    if s in ("P", "PE", "PUT"):
        return -1
    raise ValueError(f"Unknown option_type: {option_type!r}")


def _d1_d2(
    S: float | np.ndarray,
    K: float | np.ndarray,
    t: float | np.ndarray,
    r: float | np.ndarray,
    q: float | np.ndarray,
    sigma: float | np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Compute d1, d2 robustly. t and sigma must be > 0."""
    S_a = np.asarray(S, dtype=float)
    K_a = np.asarray(K, dtype=float)
    t_a = np.asarray(t, dtype=float)
    r_a = np.asarray(r, dtype=float)
    q_a = np.asarray(q, dtype=float)
    sigma_a = np.asarray(sigma, dtype=float)
    sqrt_t = np.sqrt(t_a)
    d1 = (np.log(S_a / K_a) + (r_a - q_a + 0.5 * sigma_a * sigma_a) * t_a) / (sigma_a * sqrt_t)
    d2 = d1 - sigma_a * sqrt_t
    return d1, d2


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


def bs_price(
    S: float,
    K: float,
    t: float,
    r: float,
    sigma: float,
    option_type: OptionFlag = "C",
    q: float = 0.0,
) -> float:
    """Scalar Black-Scholes price.

    Returns 0 for zero/negative time, sigma, or in-deeply-OTM degenerate cases —
    these arise for expired options and at-expiry edge cases.
    """
    if t <= 0:
        # At/after expiry — intrinsic value
        flag = _flag(option_type)
        return max(flag * (S - K), 0.0)
    if sigma <= 0:
        # Zero vol — deterministic
        flag = _flag(option_type)
        forward = S * math.exp((r - q) * t)
        return max(flag * (forward - K), 0.0) * math.exp(-r * t)
    flag = _flag(option_type)
    d1, d2 = _d1_d2(S, K, t, r, q, sigma)
    if flag == 1:
        return float(S * math.exp(-q * t) * norm.cdf(d1) - K * math.exp(-r * t) * norm.cdf(d2))
    return float(K * math.exp(-r * t) * norm.cdf(-d2) - S * math.exp(-q * t) * norm.cdf(-d1))


def bs_price_v(
    S: np.ndarray,
    K: np.ndarray,
    t: np.ndarray,
    r: np.ndarray | float,
    sigma: np.ndarray,
    option_type: np.ndarray | OptionFlag = "C",
    q: np.ndarray | float = 0.0,
) -> np.ndarray:
    """Vectorized BS price.

    All array inputs must be broadcast-compatible. `option_type` may be a single
    string or an array of +1/-1 flags or strings.
    """
    S_a = np.asarray(S, dtype=float)
    K_a = np.asarray(K, dtype=float)
    t_a = np.asarray(t, dtype=float)
    r_a = np.broadcast_to(np.asarray(r, dtype=float), S_a.shape)
    q_a = np.broadcast_to(np.asarray(q, dtype=float), S_a.shape)
    sigma_a = np.asarray(sigma, dtype=float)
    if isinstance(option_type, str) or np.isscalar(option_type):
        flag_a = np.full(S_a.shape, _flag(option_type), dtype=int)  # type: ignore[arg-type]
    else:
        flag_a = np.array([_flag(x) for x in np.asarray(option_type).flatten()], dtype=int).reshape(S_a.shape)

    out = np.zeros_like(S_a, dtype=float)

    # Edge cases: t <= 0 or sigma <= 0
    expired = t_a <= 0
    zerovol = (sigma_a <= 0) & ~expired
    normal = ~(expired | zerovol)

    # Expired: intrinsic
    if np.any(expired):
        out[expired] = np.maximum(flag_a[expired] * (S_a[expired] - K_a[expired]), 0.0)
    # Zero vol: deterministic
    if np.any(zerovol):
        fwd = S_a[zerovol] * np.exp((r_a[zerovol] - q_a[zerovol]) * t_a[zerovol])
        out[zerovol] = np.maximum(flag_a[zerovol] * (fwd - K_a[zerovol]), 0.0) * np.exp(-r_a[zerovol] * t_a[zerovol])
    # Normal case
    if np.any(normal):
        d1, d2 = _d1_d2(
            S_a[normal], K_a[normal], t_a[normal],
            r_a[normal], q_a[normal], sigma_a[normal],
        )
        is_call = flag_a[normal] == 1
        # All vectors aligned — handle calls and puts separately for clarity
        call_px = S_a[normal] * np.exp(-q_a[normal] * t_a[normal]) * norm.cdf(d1) \
                  - K_a[normal] * np.exp(-r_a[normal] * t_a[normal]) * norm.cdf(d2)
        put_px = K_a[normal] * np.exp(-r_a[normal] * t_a[normal]) * norm.cdf(-d2) \
                 - S_a[normal] * np.exp(-q_a[normal] * t_a[normal]) * norm.cdf(-d1)
        result = np.where(is_call, call_px, put_px)
        out[normal] = result
    return out


# ---------------------------------------------------------------------------
# Greeks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Greeks:
    """Risk metrics for an option position.

    Conventions (consistent with py_vollib):
        delta        — ∂P/∂S, no scaling
        gamma        — ∂²P/∂S², per unit underlying²
        theta        — ∂P/∂t (PER DAY, theta_per_day = theta_per_year / 365)
        vega         — ∂P/∂σ scaled by 1/100 → "per 1% vol move"
        rho          — ∂P/∂r scaled by 1/100 → "per 1% rate move"
        vanna        — ∂²P/∂S∂σ
        charm        — ∂²P/∂S∂t
    """
    delta: float
    gamma: float
    theta: float          # per day
    vega: float           # per 1 vol point
    rho: float            # per 1 rate point
    vanna: float = 0.0
    charm: float = 0.0


def bs_greeks(
    S: float,
    K: float,
    t: float,
    r: float,
    sigma: float,
    option_type: OptionFlag = "C",
    q: float = 0.0,
) -> Greeks:
    """Scalar Greeks. See class docstring for conventions."""
    if t <= 0 or sigma <= 0:
        return Greeks(0.0, 0.0, 0.0, 0.0, 0.0)
    flag = _flag(option_type)
    d1, d2 = _d1_d2(S, K, t, r, q, sigma)
    pdf_d1 = float(norm.pdf(d1))
    sqrt_t = math.sqrt(t)
    disc_q = math.exp(-q * t)
    disc_r = math.exp(-r * t)

    # Delta
    if flag == 1:
        delta = disc_q * float(norm.cdf(d1))
    else:
        delta = -disc_q * float(norm.cdf(-d1))

    # Gamma
    gamma = disc_q * pdf_d1 / (S * sigma * sqrt_t)

    # Vega (per 1 vol point => /100)
    vega = S * disc_q * pdf_d1 * sqrt_t / 100.0

    # Theta (per year, then /365 => per day)
    common = -(S * disc_q * pdf_d1 * sigma) / (2.0 * sqrt_t)
    if flag == 1:
        theta_year = common - r * K * disc_r * float(norm.cdf(d2)) + q * S * disc_q * float(norm.cdf(d1))
    else:
        theta_year = common + r * K * disc_r * float(norm.cdf(-d2)) - q * S * disc_q * float(norm.cdf(-d1))
    theta = theta_year / 365.0

    # Rho (per 1 rate point => /100)
    if flag == 1:
        rho = K * t * disc_r * float(norm.cdf(d2)) / 100.0
    else:
        rho = -K * t * disc_r * float(norm.cdf(-d2)) / 100.0

    # Vanna = ∂²P/∂S∂σ = -e^(-qt) phi(d1) d2 / sigma
    vanna = -disc_q * pdf_d1 * d2 / sigma

    # Charm (delta decay, per year then /365 for per-day if you want)
    # Charm formula:
    #   charm_call = -q*e^(-qt)*N(d1) + e^(-qt)*phi(d1) * (2(r-q)*t - d2*sigma*sqrt(t)) / (2*t*sigma*sqrt(t))
    # We give per-year here; convert at site of use.
    charm_term = pdf_d1 * (2 * (r - q) * t - d2 * sigma * sqrt_t) / (2.0 * t * sigma * sqrt_t)
    if flag == 1:
        charm = -q * disc_q * float(norm.cdf(d1)) + disc_q * charm_term
    else:
        charm = q * disc_q * float(norm.cdf(-d1)) - disc_q * charm_term

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho, vanna=vanna, charm=charm)


def bs_greeks_v(
    S: np.ndarray,
    K: np.ndarray,
    t: np.ndarray,
    r: np.ndarray | float,
    sigma: np.ndarray,
    option_type: np.ndarray | OptionFlag = "C",
    q: np.ndarray | float = 0.0,
) -> dict[str, np.ndarray]:
    """Vectorized Greeks. Returns dict of arrays keyed by greek name.

    Returns zeros where t<=0 or sigma<=0 — caller should mask if needed.
    """
    S_a = np.asarray(S, dtype=float)
    K_a = np.asarray(K, dtype=float)
    t_a = np.asarray(t, dtype=float)
    r_a = np.broadcast_to(np.asarray(r, dtype=float), S_a.shape)
    q_a = np.broadcast_to(np.asarray(q, dtype=float), S_a.shape)
    sigma_a = np.asarray(sigma, dtype=float)
    if isinstance(option_type, str) or np.isscalar(option_type):
        flag_a = np.full(S_a.shape, _flag(option_type), dtype=int)  # type: ignore[arg-type]
    else:
        flag_a = np.array([_flag(x) for x in np.asarray(option_type).flatten()], dtype=int).reshape(S_a.shape)

    out = {
        "delta": np.zeros_like(S_a),
        "gamma": np.zeros_like(S_a),
        "theta": np.zeros_like(S_a),
        "vega": np.zeros_like(S_a),
        "rho": np.zeros_like(S_a),
        "vanna": np.zeros_like(S_a),
        "charm": np.zeros_like(S_a),
    }

    valid = (t_a > 0) & (sigma_a > 0)
    if not np.any(valid):
        return out

    S_v = S_a[valid]
    K_v = K_a[valid]
    t_v = t_a[valid]
    r_v = r_a[valid]
    q_v = q_a[valid]
    sigma_v = sigma_a[valid]
    flag_v = flag_a[valid]
    is_call = flag_v == 1

    d1, d2 = _d1_d2(S_v, K_v, t_v, r_v, q_v, sigma_v)
    pdf_d1 = norm.pdf(d1)
    sqrt_t = np.sqrt(t_v)
    disc_q = np.exp(-q_v * t_v)
    disc_r = np.exp(-r_v * t_v)

    out["delta"][valid] = np.where(is_call, disc_q * norm.cdf(d1), -disc_q * norm.cdf(-d1))
    out["gamma"][valid] = disc_q * pdf_d1 / (S_v * sigma_v * sqrt_t)
    out["vega"][valid] = S_v * disc_q * pdf_d1 * sqrt_t / 100.0
    common = -(S_v * disc_q * pdf_d1 * sigma_v) / (2.0 * sqrt_t)
    theta_year = np.where(
        is_call,
        common - r_v * K_v * disc_r * norm.cdf(d2) + q_v * S_v * disc_q * norm.cdf(d1),
        common + r_v * K_v * disc_r * norm.cdf(-d2) - q_v * S_v * disc_q * norm.cdf(-d1),
    )
    out["theta"][valid] = theta_year / 365.0
    out["rho"][valid] = np.where(
        is_call,
        K_v * t_v * disc_r * norm.cdf(d2) / 100.0,
        -K_v * t_v * disc_r * norm.cdf(-d2) / 100.0,
    )
    out["vanna"][valid] = -disc_q * pdf_d1 * d2 / sigma_v
    charm_term = pdf_d1 * (2 * (r_v - q_v) * t_v - d2 * sigma_v * sqrt_t) / (2.0 * t_v * sigma_v * sqrt_t)
    out["charm"][valid] = np.where(
        is_call,
        -q_v * disc_q * norm.cdf(d1) + disc_q * charm_term,
        q_v * disc_q * norm.cdf(-d1) - disc_q * charm_term,
    )
    return out


# ---------------------------------------------------------------------------
# Implied volatility — Newton-Raphson with bisection fallback
# ---------------------------------------------------------------------------


def implied_volatility(
    price: float,
    S: float,
    K: float,
    t: float,
    r: float,
    option_type: OptionFlag = "C",
    q: float = 0.0,
    *,
    initial_guess: float = 0.20,
    tol: float = 1e-6,
    max_iter: int = 50,
) -> float:
    """Compute implied vol via Newton-Raphson; falls back to bisection on poor convergence.

    Returns NaN if the option price is outside the no-arbitrage bounds.
    """
    if t <= 0 or price <= 0:
        return float("nan")

    flag = _flag(option_type)
    # No-arbitrage bounds
    disc_r = math.exp(-r * t)
    disc_q = math.exp(-q * t)
    intrinsic = max(flag * (S * disc_q - K * disc_r), 0.0)
    upper = S * disc_q if flag == 1 else K * disc_r
    if price < intrinsic - 1e-10 or price > upper + 1e-10:
        return float("nan")

    sigma = max(initial_guess, 1e-4)

    for _ in range(max_iter):
        px = bs_price(S, K, t, r, sigma, option_type, q)
        diff = px - price
        if abs(diff) < tol:
            return sigma
        # vega scaled per-1-vol-point — convert back to /1.0 by *100
        g = bs_greeks(S, K, t, r, sigma, option_type, q)
        vega_per_vol = g.vega * 100.0
        if vega_per_vol < 1e-10:
            break  # stuck — fall through to bisection
        sigma -= diff / vega_per_vol
        if sigma <= 0:
            sigma = 1e-4
        elif sigma > 5.0:
            sigma = 5.0

    # Bisection fallback
    lo, hi = 1e-4, 5.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        px = bs_price(S, K, t, r, mid, option_type, q)
        if abs(px - price) < tol:
            return mid
        if px < price:
            lo = mid
        else:
            hi = mid
    return mid


def implied_volatility_v(
    price: np.ndarray,
    S: np.ndarray,
    K: np.ndarray,
    t: np.ndarray,
    r: np.ndarray | float,
    option_type: np.ndarray | OptionFlag = "C",
    q: np.ndarray | float = 0.0,
    *,
    initial_guess: float = 0.20,
    tol: float = 1e-6,
    max_iter: int = 50,
) -> np.ndarray:
    """Vectorized IV via element-wise Newton-Raphson.

    Each element converges independently; the loop runs until ALL elements have
    converged or max_iter is reached. Non-convergent or out-of-bounds inputs are
    NaN in the output.
    """
    price_a = np.asarray(price, dtype=float)
    S_a = np.asarray(S, dtype=float)
    K_a = np.asarray(K, dtype=float)
    t_a = np.asarray(t, dtype=float)
    r_a = np.broadcast_to(np.asarray(r, dtype=float), S_a.shape)
    q_a = np.broadcast_to(np.asarray(q, dtype=float), S_a.shape)
    if isinstance(option_type, str) or np.isscalar(option_type):
        flag_a = np.full(S_a.shape, _flag(option_type), dtype=int)  # type: ignore[arg-type]
    else:
        flag_a = np.array([_flag(x) for x in np.asarray(option_type).flatten()], dtype=int).reshape(S_a.shape)

    iv = np.full_like(S_a, initial_guess, dtype=float)
    iv = np.where(t_a <= 0, np.nan, iv)
    iv = np.where(price_a <= 0, np.nan, iv)

    valid = ~np.isnan(iv)

    for _ in range(max_iter):
        if not np.any(valid):
            break
        # Compute price + vega for valid elements
        px = bs_price_v(S_a, K_a, t_a, r_a, iv, flag_a, q_a)
        greeks = bs_greeks_v(S_a, K_a, t_a, r_a, iv, flag_a, q_a)
        vega_per_vol = greeks["vega"] * 100.0
        diff = px - price_a
        converged = np.abs(diff) < tol
        # Stop iterating where converged
        active = valid & ~converged
        if not np.any(active):
            break
        # Newton step where vega is reasonable
        step = np.where(vega_per_vol > 1e-10, diff / np.maximum(vega_per_vol, 1e-10), 0.0)
        iv = np.where(active, iv - step, iv)
        # Keep iv in plausible range
        iv = np.clip(iv, 1e-4, 5.0)

    # Mark non-converged or out-of-bounds as NaN
    px_final = bs_price_v(S_a, K_a, t_a, r_a, iv, flag_a, q_a)
    iv = np.where(np.abs(px_final - price_a) < 1e-3, iv, np.nan)
    iv = np.where(t_a <= 0, np.nan, iv)
    iv = np.where(price_a <= 0, np.nan, iv)
    return iv

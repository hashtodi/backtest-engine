"""Black-Scholes greeks (stdlib only — no scipy).

Delta from the option's own IV, used for iron-condor leg selection.
sigma is decimal (iv/100); T is in years (minutes_to_expiry / 525_600).
"""
import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via the error function (no scipy needed)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(option_type: str, S: float, K: float, sigma: float,
             T: float, r: float = 0.065, q: float = 0.0) -> float:
    """Black-Scholes delta. Returns NaN for degenerate inputs.

    CE: e^(-qT) * N(d1);  PE: e^(-qT) * (N(d1) - 1).
    """
    if not (S > 0 and K > 0 and sigma > 0 and T > 0):
        return float("nan")
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    disc = math.exp(-q * T)
    if option_type == "CE":
        return disc * _norm_cdf(d1)
    return disc * (_norm_cdf(d1) - 1.0)

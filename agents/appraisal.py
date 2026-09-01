#appraisal.py
"""
FTA Agent — Investment Appraisal Toolkit (Phase 1 of the FTA build).

Pure, deterministic calculation functions — NPV, IRR, MIRR, PBP (simple +
discounted), ARR, and PI. Deliberately kept separate from any LLM call:
per FTA's blueprint, letting an LLM compute IRR/NPV risks silent,
confident-sounding arithmetic errors — these numbers come from real code,
every time, and the LLM's job (in fta_agent.py, not here) is interpreting
the result, never producing it.

Every function takes plain Python floats/lists — no LITE-specific types —
so this module is independently testable and reusable outside the agent
wrapper entirely.

Sign convention throughout: cashflows[0] is the initial outlay and should
be negative (e.g. -100000 for a 100,000 initial investment); subsequent
entries are the returns for each period, positive or negative as they
actually occur.
"""
from __future__ import annotations

import numpy_financial as npf


def npv(rate: float, cashflows: list[float]) -> float:
    """Net Present Value. `rate` is the discount rate per period as a
    decimal (0.10 for 10%), not a percentage."""
    return float(npf.npv(rate, cashflows))


def irr(cashflows: list[float]) -> float | None:
    """Internal Rate of Return, as a decimal (0.10 = 10%). Returns None if
    no real root exists (e.g. all cashflows the same sign) rather than
    raising or returning a misleading NaN silently."""
    result = npf.irr(cashflows)
    if result is None or result != result:  # NaN check without importing math
        return None
    return float(result)


def mirr(cashflows: list[float], finance_rate: float, reinvest_rate: float) -> float:
    """Modified Internal Rate of Return — addresses IRR's unrealistic
    reinvestment-at-IRR assumption by using separate, explicit rates for
    financing outflows and reinvesting inflows."""
    return float(npf.mirr(cashflows, finance_rate, reinvest_rate))


def payback_period(cashflows: list[float]) -> float | None:
    """Simple (undiscounted) payback period, in periods — fractional via
    linear interpolation within the period recovery actually happens.
    Returns None if the investment never pays back within the given
    cashflow horizon."""
    cumulative = 0.0
    for i, cf in enumerate(cashflows):
        prev_cumulative = cumulative
        cumulative += cf
        if i > 0 and cumulative >= 0 and prev_cumulative < 0:
            # Recovered partway through this period — interpolate.
            fraction = -prev_cumulative / cf if cf != 0 else 0.0
            return (i - 1) + fraction
    return None


def discounted_payback_period(rate: float, cashflows: list[float]) -> float | None:
    """Same as payback_period, but on discounted cashflows — accounts for
    the time value of money, which simple payback ignores entirely."""
    discounted = [cf / ((1 + rate) ** i) for i, cf in enumerate(cashflows)]
    return payback_period(discounted)


def accounting_rate_of_return(avg_annual_profit: float, initial_investment: float) -> float:
    """ARR = average annual accounting profit / initial investment, as a
    decimal. Unlike NPV/IRR/PBP, this is NOT cashflow-based — it uses
    accounting profit (post-depreciation), which is why it takes different
    inputs than the other functions here rather than a cashflow list."""
    if initial_investment == 0:
        raise ValueError("initial_investment cannot be zero")
    return avg_annual_profit / initial_investment


def profitability_index(rate: float, cashflows: list[float]) -> float:
    """PI = PV of future cashflows / |initial investment|. PI > 1 means
    the same accept/reject signal as NPV > 0, just expressed as a ratio —
    useful for comparing/ranking projects of different scale."""
    if not cashflows or cashflows[0] == 0:
        raise ValueError("cashflows[0] (the initial outlay) must be non-zero")
    initial_outlay = abs(cashflows[0])
    future_cashflows = cashflows[1:]
    pv_future = sum(cf / ((1 + rate) ** (i + 1)) for i, cf in enumerate(future_cashflows))
    return pv_future / initial_outlay


def appraise(
    cashflows: list[float],
    rate: float,
    finance_rate: float | None = None,
    reinvest_rate: float | None = None,
    avg_annual_profit: float | None = None,
    initial_investment: float | None = None,
) -> dict:
    """
    Runs every applicable metric in one call and returns a structured dict
    — this is what fta_agent.py's investment_appraisal action actually
    calls, so a single user request produces the full picture (NPV, IRR,
    MIRR, both payback measures, PI, and ARR if the accounting-profit
    inputs are supplied) rather than requiring six separate calls.
    """
    result = {
        "npv": npv(rate, cashflows),
        "irr": irr(cashflows),
        "payback_period": payback_period(cashflows),
        "discounted_payback_period": discounted_payback_period(rate, cashflows),
        "profitability_index": profitability_index(rate, cashflows),
    }
    if finance_rate is not None and reinvest_rate is not None:
        result["mirr"] = mirr(cashflows, finance_rate, reinvest_rate)
    if avg_annual_profit is not None and initial_investment is not None:
        result["arr"] = accounting_rate_of_return(avg_annual_profit, initial_investment)
    return result

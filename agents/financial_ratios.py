#financial_ratios.py
"""
FTA Agent — Financial Ratio Toolkit (part of Phase A: statement
interpretation). Same principle as appraisal.py's NPV/IRR: ratio
computation is pure arithmetic and belongs in real code, never LLM
arithmetic. fta_agent.py's statement-interpretation flow uses the LLM to
EXTRACT structured figures from a statement (a legitimate language task)
and to INTERPRET the computed ratios (also legitimate) — but the actual
division never happens inside a model call.

Every function takes plain floats and returns None (never raises, never
divides by zero) when an input needed for that specific ratio is missing
or zero — callers should treat None as "not computable from what was
provided," not as an error.
"""
from __future__ import annotations


def _safe_div(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


# ── Liquidity ────────────────────────────────────────────────────────────

def current_ratio(current_assets: float | None, current_liabilities: float | None) -> float | None:
    return _safe_div(current_assets, current_liabilities)


def quick_ratio(current_assets: float | None, inventory: float | None, current_liabilities: float | None) -> float | None:
    if current_assets is None or inventory is None:
        return None
    return _safe_div(current_assets - inventory, current_liabilities)


def working_capital(current_assets: float | None, current_liabilities: float | None) -> float | None:
    if current_assets is None or current_liabilities is None:
        return None
    return current_assets - current_liabilities


# ── Leverage / solvency ──────────────────────────────────────────────────

def debt_to_equity(total_liabilities: float | None, total_equity: float | None) -> float | None:
    return _safe_div(total_liabilities, total_equity)


def interest_coverage(ebit: float | None, interest_expense: float | None) -> float | None:
    return _safe_div(ebit, interest_expense)


# ── Profitability ────────────────────────────────────────────────────────

def gross_margin(revenue: float | None, cogs: float | None) -> float | None:
    if revenue is None or cogs is None:
        return None
    return _safe_div(revenue - cogs, revenue)


def net_margin(net_income: float | None, revenue: float | None) -> float | None:
    return _safe_div(net_income, revenue)


def return_on_equity(net_income: float | None, total_equity: float | None) -> float | None:
    return _safe_div(net_income, total_equity)


def return_on_assets(net_income: float | None, total_assets: float | None) -> float | None:
    return _safe_div(net_income, total_assets)


# ── Combined ─────────────────────────────────────────────────────────────

def compute_all(figures: dict) -> dict:
    """
    Takes a dict of extracted statement line items (any subset — missing
    keys just mean the ratios needing them come back None) and returns
    every ratio computable from what's present. Keys expected, all
    optional: revenue, cogs, net_income, current_assets,
    current_liabilities, inventory, total_liabilities, total_equity,
    total_assets, ebit, interest_expense.
    """
    g = figures.get
    return {
        "current_ratio":       current_ratio(g("current_assets"), g("current_liabilities")),
        "quick_ratio":         quick_ratio(g("current_assets"), g("inventory"), g("current_liabilities")),
        "working_capital":     working_capital(g("current_assets"), g("current_liabilities")),
        "debt_to_equity":      debt_to_equity(g("total_liabilities"), g("total_equity")),
        "interest_coverage":   interest_coverage(g("ebit"), g("interest_expense")),
        "gross_margin":        gross_margin(g("revenue"), g("cogs")),
        "net_margin":          net_margin(g("net_income"), g("revenue")),
        "return_on_equity":    return_on_equity(g("net_income"), g("total_equity")),
        "return_on_assets":    return_on_assets(g("net_income"), g("total_assets")),
    }

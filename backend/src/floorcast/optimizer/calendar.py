"""Calendar layer for the optimizer.

Maps the optimizer's abstract plan steps (1..N) onto real consecutive calendar
months and standard fiscal quarters, so plans can be expressed as a date range
(e.g. May 2026 -> Feb 2027) and budgets can be capped per quarter.

Standard calendar quarters only:
    Jan-Mar = Q1, Apr-Jun = Q2, Jul-Sep = Q3, Oct-Dec = Q4.
Custom / offset fiscal-year starts (e.g. an FY beginning in April) are a FUTURE
ENHANCEMENT and are intentionally NOT supported here.

A date range may start or end mid-quarter, so the first and last quarters can be
PARTIAL: a quarter's step list contains only the months of that quarter that
actually fall inside the range. Per-quarter caps therefore apply to whatever
months of the quarter are present.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanMonth:
    """One step of the plan, optionally tied to a real calendar month."""

    step: int  # 1-based step index within the plan
    label: str  # "2026-05" (calendar mode) or "1" (abstract mode)
    year: int | None = None
    month: int | None = None  # 1-12
    quarter: str | None = None  # "2026-Q2" (calendar mode) or None (abstract)


def quarter_label(year: int, month: int) -> str:
    """Standard calendar quarter, e.g. (2026, 5) -> '2026-Q2'."""
    return f"{year}-Q{(month - 1) // 3 + 1}"


def abstract_calendar(horizon_months: int) -> list[PlanMonth]:
    """Calendar-free steps: no year/month/quarter, label is just the step number.

    This is the default and reproduces pre-Stage-3 behaviour exactly.
    """
    return [PlanMonth(step=t, label=str(t)) for t in range(1, horizon_months + 1)]


def calendar_from_range(
    start_year: int, start_month: int, end_year: int, end_month: int
) -> list[PlanMonth]:
    """Consecutive calendar months from start..end inclusive.

    Example: (2026, 5, 2027, 2) -> 10 steps May 2026 .. Feb 2027.
    Raises ValueError if the end precedes the start.
    """
    start = start_year * 12 + (start_month - 1)
    end = end_year * 12 + (end_month - 1)
    if end < start:
        raise ValueError(
            f"calendar end ({end_year}-{end_month:02d}) precedes start "
            f"({start_year}-{start_month:02d})"
        )
    steps: list[PlanMonth] = []
    for offset in range(end - start + 1):
        idx = start + offset
        y, m = divmod(idx, 12)
        m += 1  # divmod gives 0-based month
        steps.append(
            PlanMonth(
                step=offset + 1,
                label=f"{y}-{m:02d}",
                year=y,
                month=m,
                quarter=quarter_label(y, m),
            )
        )
    return steps

"""
cryptogent.market._utils
~~~~~~~~~~~~~~~~~~~~~~~~
Shared low-level helpers for compute-engine indicator modules.

Keep this module dependency-free (stdlib only) so it can be imported
without pandas / pandas-ta being installed.
"""
from __future__ import annotations

from decimal import Decimal, getcontext
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd

# ---------------------------------------------------------------------------
# Pin Decimal precision once for the entire process.
# 28 digits is the Python default; we make it explicit so it is never
# accidentally changed by a third-party library.
# ---------------------------------------------------------------------------
getcontext().prec = 28


# ---------------------------------------------------------------------------
# Scalar conversion
# ---------------------------------------------------------------------------

def to_decimal(value: object) -> Decimal | None:
    """
    Safely convert any scalar to ``Decimal``.

    Returns ``None`` for ``NaN``, ``±inf``, ``None``, or any value that
    cannot be converted.  Uses the ``f != f`` identity to detect NaN without
    importing ``math``.
    """
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
        if f != f or f == float("inf") or f == float("-inf"):
            return None
        return Decimal(str(f))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pandas Series helpers
# ---------------------------------------------------------------------------

def series_last(series: "pd.Series | None") -> Decimal | None:
    """
    Return the value at the *last index position* of *series* as ``Decimal``.

    Uses positional ``iloc[-1]`` on the original (non-dropna'd) series so
    the result always corresponds to the most recent bar, even if it is NaN
    (in which case ``None`` is returned).  This preserves temporal alignment.
    """
    if series is None or series.empty:
        return None
    return to_decimal(series.iloc[-1])


def series_prev(series: "pd.Series | None") -> Decimal | None:
    """
    Return the value at the *second-to-last index position* of *series*.

    Uses positional ``iloc[-2]`` on the original series so the result always
    corresponds to bar N-1, not the last *non-NaN* value (which could be
    several bars older and would break crossover detection).
    """
    if series is None or len(series) < 2:
        return None
    return to_decimal(series.iloc[-2])


def series_last_valid(series: "pd.Series | None") -> Decimal | None:
    """
    Return the last *non-NaN* value of *series* as ``Decimal``.

    Use this only when you explicitly want to skip NaN tails (e.g. when
    surfacing a scalar value that must be valid).  Do NOT use for prev/curr
    comparisons where temporal alignment matters.
    """
    if series is None or series.empty:
        return None
    clean = series.dropna()
    if clean.empty:
        return None
    return to_decimal(clean.iloc[-1])


def df_last(df: "pd.DataFrame | None", col: str) -> Decimal | None:
    """Return ``df[col].iloc[-1]`` as ``Decimal``, or ``None`` on any failure."""
    if df is None or df.empty or col not in df.columns:
        return None
    return to_decimal(df.iloc[-1][col])


# ---------------------------------------------------------------------------
# Closes list validation
# ---------------------------------------------------------------------------

def validate_closes(closes: object, caller: str) -> list[float]:
    """
    Validate and convert a ``list[Decimal]`` of closing prices to ``list[float]``.

    Parameters
    ----------
    closes:
        Expected to be a non-empty sequence of numeric values.
    caller:
        Name of the calling function, used in error messages.

    Returns
    -------
    list[float]
        Ready for ``pd.Series(..., dtype='float64')``.

    Raises
    ------
    ValueError
        On empty input, None input, or unconvertible elements.
    """
    if not closes:
        raise ValueError(f"{caller}: 'closes' must be a non-empty list, got {type(closes).__name__}.")
    result: list[float] = []
    for i, c in enumerate(closes):  # type: ignore[union-attr]
        if c is None:
            raise ValueError(f"{caller}: closes[{i}] is None — all values must be numeric.")
        try:
            result.append(float(c))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{caller}: closes[{i}]={c!r} cannot be converted to float."
            ) from exc
    return result
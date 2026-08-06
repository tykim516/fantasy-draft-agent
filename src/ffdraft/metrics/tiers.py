"""Gap-based tiering.

Tiers exist because ordinal ranks lie. When the gap between the 14th and 16th
player is a third of a point, presenting one as better than the other is false
precision dressed up as analysis. A tier says the honest thing: these players
are functionally interchangeable, take whichever you prefer.

The algorithm is deliberately simple and explainable — a break happens where the
drop to the next player is unusually large relative to the drops around it.
Anything cleverer would be harder to argue with, which is the wrong trade for a
number a human has to act on in ten seconds while on the clock.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import polars as pl

# A gap this many standard deviations above the mean gap starts a new tier.
DEFAULT_Z = 1.0
# Even without a statistical break, a tier this large is not telling you much.
DEFAULT_MAX_TIER_SIZE = 8


@dataclass(frozen=True)
class TierBreak:
    """Where a tier ended and why, so the board can show its work."""

    after_rank: int
    gap: float
    threshold: float
    reason: str


def find_breaks(
    values: Sequence[float],
    z: float = DEFAULT_Z,
    max_tier_size: int | None = DEFAULT_MAX_TIER_SIZE,
) -> list[TierBreak]:
    """Locate tier boundaries in a descending-sorted value series."""
    values = [float(v) for v in values]
    if len(values) < 3:
        return []

    gaps = [values[i] - values[i + 1] for i in range(len(values) - 1)]
    mean_gap = sum(gaps) / len(gaps)
    variance = sum((g - mean_gap) ** 2 for g in gaps) / len(gaps)
    std_gap = variance**0.5
    threshold = mean_gap + z * std_gap

    breaks: list[TierBreak] = []
    size = 0
    for index, gap in enumerate(gaps):
        size += 1
        if std_gap > 0 and gap > threshold:
            breaks.append(
                TierBreak(
                    after_rank=index + 1,
                    gap=round(gap, 3),
                    threshold=round(threshold, 3),
                    reason=f"gap exceeds mean + {z:.1f} sd",
                )
            )
            size = 0
        elif max_tier_size and size >= max_tier_size:
            # No statistical break, but a 12-deep tier is not a useful claim.
            breaks.append(
                TierBreak(
                    after_rank=index + 1,
                    gap=round(gap, 3),
                    threshold=round(threshold, 3),
                    reason=f"max tier size {max_tier_size} reached without a clear break",
                )
            )
            size = 0
    return breaks


def assign_tiers(
    values: Sequence[float],
    z: float = DEFAULT_Z,
    max_tier_size: int | None = DEFAULT_MAX_TIER_SIZE,
) -> list[int]:
    """Tier numbers (1-based) for a descending-sorted value series."""
    breaks = {b.after_rank for b in find_breaks(values, z, max_tier_size)}
    tiers, current = [], 1
    for index in range(len(values)):
        tiers.append(current)
        if index + 1 in breaks:
            current += 1
    return tiers


def add_tiers(
    df: pl.DataFrame,
    value_col: str = "vor",
    over: str | None = None,
    z: float = DEFAULT_Z,
    max_tier_size: int | None = DEFAULT_MAX_TIER_SIZE,
    out: str = "tier",
) -> pl.DataFrame:
    """Attach a tier column, either overall or within each `over` group.

    Positional tiers (`over="position"`) answer "should I take another RB now?".
    Overall tiers answer "is anyone left worth this pick?". A board needs both.
    """
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Int64).alias(out))

    if over is None:
        ordered = df.sort(value_col, descending=True, nulls_last=True)
        return ordered.with_columns(
            pl.Series(
                out, assign_tiers(ordered[value_col].fill_null(0.0).to_list(), z, max_tier_size)
            )
        )

    frames = []
    for _group, part in df.group_by([over], maintain_order=True):
        ordered = part.sort(value_col, descending=True, nulls_last=True)
        frames.append(
            ordered.with_columns(
                pl.Series(
                    out, assign_tiers(ordered[value_col].fill_null(0.0).to_list(), z, max_tier_size)
                )
            )
        )
    return pl.concat(frames).sort(value_col, descending=True, nulls_last=True)


def tier_summary(df: pl.DataFrame, value_col: str = "vor", tier_col: str = "tier") -> pl.DataFrame:
    """Per-tier size and value range — the evidence behind the tier boundaries."""
    return (
        df.group_by(tier_col)
        .agg(
            players=pl.len(),
            best=pl.col(value_col).max().round(2),
            worst=pl.col(value_col).min().round(2),
        )
        .with_columns(spread=(pl.col("best") - pl.col("worst")).round(2))
        .sort(tier_col)
    )

"""Tier picks: rank each tier's candidates from simulated season earnings."""

from collections.abc import Sequence

import numpy as np
import pandas as pd

RANK_KEYS = ("mean", "p_top")


def _quantile_column(q: float) -> str:
    return f"p{round(q * 100)}"


def rank_tiers(
    samples: np.ndarray,
    player_ids: Sequence[str],
    tiers: pd.DataFrame,
    *,
    quantiles: Sequence[float] = (0.1, 0.5, 0.9),
    rank_by: str = "mean",
) -> pd.DataFrame:
    """Rank the players of every tier by simulated earnings.

    `samples` has shape (n_players, n_draws), in dollars, rows following `player_ids`. `tiers` has
    `tier` and `player_id` columns, optionally `input_name`. Tiers come out in order of first
    appearance in `tiers`. Within a tier, projected players are ranked by `rank_by` ("mean" or
    "p_top", descending, ties keep their `tiers` order); players without draws follow, unranked by
    any statistic, with `projected=False` and NaN statistics. `p_top` is the share of draws in which
    a player has the tier's maximum, ties splitting the draw evenly.
    """
    if rank_by not in RANK_KEYS:
        raise ValueError(f"rank_by must be one of {RANK_KEYS}, got {rank_by!r}")
    samples = np.asarray(samples, dtype=float)
    if samples.ndim != 2 or samples.shape[0] != len(player_ids):
        raise ValueError("samples must have shape (len(player_ids), n_draws)")

    row_of = {pid: i for i, pid in enumerate(player_ids)}
    qcols = [_quantile_column(q) for q in quantiles]
    stat_cols = ["mean", *qcols, "p_top"]
    names = tiers["input_name"] if "input_name" in tiers else tiers["player_id"]

    frames = []
    for tier in dict.fromkeys(tiers["tier"]):
        in_tier = (tiers["tier"] == tier).to_numpy()
        members = pd.DataFrame(
            {
                "tier": tier,
                "player_id": tiers["player_id"][in_tier].to_numpy(),
                "input_name": names[in_tier].to_numpy(),
            }
        )
        rows = members["player_id"].map(row_of)
        members["projected"] = rows.notna().to_numpy()
        for col in stat_cols:
            members[col] = np.nan

        proj = members["projected"].to_numpy()
        if proj.any():
            sub = samples[rows[proj].astype(int).to_numpy()]
            is_max = sub == sub.max(axis=0)
            members.loc[proj, "mean"] = sub.mean(axis=1)
            members.loc[proj, qcols] = np.quantile(sub, quantiles, axis=1).T
            members.loc[proj, "p_top"] = (is_max / is_max.sum(axis=0)).mean(axis=1)

        # Unprojected players sort last (NaN key); mergesort keeps ties in input order.
        order = np.argsort(-members[rank_by].to_numpy(), kind="mergesort")
        members = members.iloc[order].reset_index(drop=True)
        members.insert(1, "rank", np.arange(1, len(members) + 1))
        frames.append(members)

    columns = ["tier", "rank", "player_id", "input_name", "projected", *stat_cols]
    if not frames:
        return pd.DataFrame(columns=columns)
    return pd.concat(frames, ignore_index=True)[columns]


def _dollars(value: float) -> str:
    return "-" if pd.isna(value) else f"${value:,.0f}"


def _percent(value: float) -> str:
    return "-" if pd.isna(value) else f"{value:.1%}"


def render_picks_markdown(ranked: pd.DataFrame, title: str) -> str:
    """One markdown table per tier, in the order tiers appear in `ranked`."""
    stat_cols = list(ranked.columns[list(ranked.columns).index("mean") :])
    quantile_cols = stat_cols[1:-1]
    header = ["Rank", "Player", "Mean", *(c.upper() for c in quantile_cols), "P(top)"]

    lines = [f"# {title}"]
    for tier in dict.fromkeys(ranked["tier"]):
        lines += ["", f"## Tier {tier}", ""]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "|".join(["---"] * len(header)) + "|")
        for row in ranked[ranked["tier"] == tier].itertuples(index=False):
            cells = [
                str(row.rank),
                str(row.input_name),
                _dollars(row.mean),
                *(_dollars(getattr(row, c)) for c in quantile_cols),
                _percent(row.p_top),
            ]
            lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"

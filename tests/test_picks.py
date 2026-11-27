"""Tier picks: per-tier ranking of simulated season earnings and the markdown report.

Ruling: tier order in the output (frame rows and markdown tables) is the order tiers first appear
in the `tiers` frame, not sorted order.
"""

import re

import numpy as np
import pandas as pd
import pytest

from fantasygolf.picks import rank_tiers, render_picks_markdown

COLUMNS = [
    "tier",
    "rank",
    "player_id",
    "input_name",
    "projected",
    "mean",
    "p10",
    "p50",
    "p90",
    "p_top",
]


def make_tiers(rows):
    """rows: (tier, player_id, input_name) tuples."""
    return pd.DataFrame(rows, columns=["tier", "player_id", "input_name"])


def tier_rows(out, tier):
    return out[out["tier"] == tier].sort_values("rank").reset_index(drop=True)


def brute_force_p_top(draws):
    """p_top per player (rows of draws) by looping over draws; ties split evenly."""
    n_players, n_draws = draws.shape
    p = np.zeros(n_players)
    for d in range(n_draws):
        col = draws[:, d]
        winners = np.flatnonzero(col == col.max())
        p[winners] += 1.0 / len(winners)
    return p / n_draws


@pytest.fixture
def two_tier_case():
    samples = np.array(
        [
            [3.0, 3.0, 3.0],  # a
            [1.0, 5.0, 1.0],  # b
            [10.0, 20.0, 30.0],  # c
            [40.0, 10.0, 20.0],  # d
        ]
    )
    ids = ["a", "b", "c", "d"]
    tiers = make_tiers(
        [
            ("A", "a", "Alpha"),
            ("A", "b", "Bravo"),
            ("B", "c", "Charlie"),
            ("B", "d", "Delta"),
        ]
    )
    return samples, ids, tiers


# --- broad behavior --------------------------------------------------------------------------


def test_rank_tiers_basic_two_tiers(two_tier_case):
    samples, ids, tiers = two_tier_case
    out = rank_tiers(samples, ids, tiers)

    assert list(out.columns) == COLUMNS
    assert len(out) == 4

    a_tier = tier_rows(out, "A")
    assert list(a_tier["player_id"]) == ["a", "b"]
    assert list(a_tier["rank"]) == [1, 2]
    assert list(a_tier["input_name"]) == ["Alpha", "Bravo"]
    assert a_tier["projected"].tolist() == [True, True]
    assert a_tier["mean"].tolist() == pytest.approx([3.0, 7.0 / 3.0])
    assert a_tier["p_top"].tolist() == pytest.approx([2.0 / 3.0, 1.0 / 3.0])

    # tier B: c=(10,20,30) mean 20; d=(40,10,20) mean 70/3; d ranks first, wins draw 1 only...
    # draw1 d(40)>c(10); draw2 c(20)>d(10); draw3 c(30)>d(20) -> c 2/3, d 1/3
    b_tier = tier_rows(out, "B")
    assert list(b_tier["player_id"]) == ["d", "c"]
    assert list(b_tier["rank"]) == [1, 2]
    assert b_tier["mean"].tolist() == pytest.approx([70.0 / 3.0, 20.0])
    assert b_tier["p_top"].tolist() == pytest.approx([1.0 / 3.0, 2.0 / 3.0])


def test_ranks_start_at_one_per_tier_and_follow_mean_descending():
    rng = np.random.default_rng(7)
    n_players, n_draws = 9, 400
    scales = np.array([5, 50, 20, 80, 10, 35, 65, 15, 95], dtype=float)
    samples = rng.gamma(2.0, scales[:, None], size=(n_players, n_draws))
    ids = [f"p{i}" for i in range(n_players)]
    tiers = make_tiers(
        [("T1", ids[i], f"N{i}") for i in (0, 1, 2, 3)]
        + [("T2", ids[i], f"N{i}") for i in (4, 5, 6)]
        + [("T3", ids[i], f"N{i}") for i in (7, 8)]
    )
    out = rank_tiers(samples, ids, tiers)

    for tier, members in (("T1", (0, 1, 2, 3)), ("T2", (4, 5, 6)), ("T3", (7, 8))):
        rows = tier_rows(out, tier)
        assert list(rows["rank"]) == list(range(1, len(members) + 1))
        means = {ids[i]: samples[i].mean() for i in members}
        expected = sorted(means, key=means.get, reverse=True)
        assert list(rows["player_id"]) == expected
        for _, r in rows.iterrows():
            assert r["mean"] == pytest.approx(means[r["player_id"]])


def test_p_top_sums_to_one_per_tier():
    rng = np.random.default_rng(11)
    samples = rng.normal(1000.0, 300.0, size=(8, 500))
    ids = [f"p{i}" for i in range(8)]
    tiers = make_tiers(
        [("A", "p0", "n0"), ("A", "p1", "n1"), ("A", "p2", "n2")]
        + [("B", "p3", "n3"), ("B", "p4", "n4")]
        + [("C", "p5", "n5"), ("C", "p6", "n6"), ("C", "p7", "n7")]
    )
    out = rank_tiers(samples, ids, tiers)
    sums = out.groupby("tier")["p_top"].sum()
    assert sorted(sums.index) == ["A", "B", "C"]
    assert sums.tolist() == pytest.approx([1.0, 1.0, 1.0])
    assert (out["p_top"] >= 0).all() and (out["p_top"] <= 1).all()


def test_p_top_sums_to_one_with_ties_and_unprojected_players():
    # Heavy ties (small integer draws) plus a tier member with no draws.
    rng = np.random.default_rng(3)
    samples = rng.integers(0, 3, size=(4, 60)).astype(float)
    ids = ["a", "b", "c", "d"]
    tiers = make_tiers(
        [("A", "a", "na"), ("A", "b", "nb"), ("A", "c", "nc"), ("A", "ghost", "ng")]
        + [("B", "d", "nd")]
    )
    out = rank_tiers(samples, ids, tiers)
    for tier in ("A", "B"):
        proj = out[(out["tier"] == tier) & out["projected"]]
        assert proj["p_top"].sum() == pytest.approx(1.0)


def test_p_top_matches_brute_force_on_random_draws():
    rng = np.random.default_rng(21)
    samples = np.round(rng.normal(50.0, 20.0, size=(7, 300)))  # rounding creates real ties
    ids = [f"p{i}" for i in range(7)]
    members = {"X": [0, 2, 4], "Y": [1, 3, 5, 6]}
    tiers = make_tiers([(t, ids[i], f"n{i}") for t, idx in members.items() for i in idx])
    out = rank_tiers(samples, ids, tiers)
    for tier, idx in members.items():
        expected = brute_force_p_top(samples[idx])
        got = out[out["tier"] == tier].set_index("player_id")["p_top"]
        assert got.loc[[ids[i] for i in idx]].tolist() == pytest.approx(expected.tolist())


def test_p_top_ignores_players_in_other_tiers():
    # A player in tier B with enormous draws must not change tier A's probabilities.
    samples = np.array([[1.0, 2.0, 3.0], [3.0, 2.0, 1.0], [1e9, 1e9, 1e9]])
    ids = ["a", "b", "huge"]
    tiers = make_tiers([("A", "a", "na"), ("A", "b", "nb"), ("B", "huge", "nh")])
    out = rank_tiers(samples, ids, tiers)
    a_rows = out[out["tier"] == "A"].set_index("player_id")
    # a wins draw 3, b wins draw 1, draw 2 tied
    assert a_rows.loc["a", "p_top"] == pytest.approx(0.5)
    assert a_rows.loc["b", "p_top"] == pytest.approx(0.5)
    assert out[out["tier"] == "B"]["p_top"].tolist() == pytest.approx([1.0])


def test_dominant_player_has_p_top_one():
    rng = np.random.default_rng(5)
    samples = rng.uniform(0.0, 100.0, size=(4, 200))
    samples[2] += 1000.0  # player p2 beats everyone in its tier in every draw
    ids = ["p0", "p1", "p2", "p3"]
    tiers = make_tiers([("A", "p0", "n0"), ("A", "p1", "n1"), ("A", "p2", "n2"), ("A", "p3", "n3")])
    out = rank_tiers(samples, ids, tiers)
    rows = out.set_index("player_id")
    assert rows.loc["p2", "p_top"] == pytest.approx(1.0)
    assert rows.loc["p2", "rank"] == 1
    for pid in ("p0", "p1", "p3"):
        assert rows.loc[pid, "p_top"] == pytest.approx(0.0)


def test_ties_split_p_top():
    samples = np.array(
        [
            [5.0, 1.0, 3.0],  # a
            [5.0, 2.0, 3.0],  # b: ties a in draws 1 and 3, wins draw 2
            [1.0, 1.0, 1.0],  # c
        ]
    )
    ids = ["a", "b", "c"]
    tiers = make_tiers([("A", "a", "na"), ("A", "b", "nb"), ("A", "c", "nc")])
    rows = rank_tiers(samples, ids, tiers).set_index("player_id")
    assert rows.loc["a", "p_top"] == pytest.approx(1.0 / 3.0)
    assert rows.loc["b", "p_top"] == pytest.approx(2.0 / 3.0)
    assert rows.loc["c", "p_top"] == pytest.approx(0.0)


def test_three_way_tie_splits_in_thirds_and_identical_players_split_evenly():
    samples = np.array([[7.0, 7.0]] * 3 + [[1.0, 1.0]])
    ids = ["a", "b", "c", "d"]
    tiers = make_tiers([("A", p, f"n{p}") for p in ids])
    rows = rank_tiers(samples, ids, tiers).set_index("player_id")
    for pid in ("a", "b", "c"):
        assert rows.loc[pid, "p_top"] == pytest.approx(1.0 / 3.0)
    assert rows.loc["d", "p_top"] == pytest.approx(0.0)


def test_single_player_tier_has_p_top_one():
    samples = np.array([[10.0, 20.0, 30.0, 40.0]])
    out = rank_tiers(samples, ["solo"], make_tiers([("A", "solo", "Solo")]))
    assert len(out) == 1
    row = out.iloc[0]
    assert row["rank"] == 1
    assert row["p_top"] == pytest.approx(1.0)
    assert row["mean"] == pytest.approx(25.0)


# --- quantiles -------------------------------------------------------------------------------


def test_quantiles_equal_numpy_on_same_draws():
    rng = np.random.default_rng(99)
    samples = rng.lognormal(mean=13.0, sigma=0.8, size=(6, 251))
    ids = [f"p{i}" for i in range(6)]
    tiers = make_tiers(
        [("A", ids[i], f"n{i}") for i in range(3)] + [("B", ids[i], f"n{i}") for i in range(3, 6)]
    )
    out = rank_tiers(samples, ids, tiers).set_index("player_id")
    for i, pid in enumerate(ids):
        q = np.quantile(samples[i], [0.1, 0.5, 0.9])
        assert out.loc[pid, "p10"] == pytest.approx(q[0], rel=1e-12)
        assert out.loc[pid, "p50"] == pytest.approx(q[1], rel=1e-12)
        assert out.loc[pid, "p90"] == pytest.approx(q[2], rel=1e-12)
        assert out.loc[pid, "p10"] <= out.loc[pid, "p50"] <= out.loc[pid, "p90"]
        assert out.loc[pid, "mean"] == pytest.approx(samples[i].mean(), rel=1e-12)


def test_quantiles_interpolate_between_draws():
    # 0..10 shuffled: p10 = 1, p50 = 5, p90 = 9; b has fractional-index quantiles.
    samples = np.array(
        [
            [10.0, 0.0, 5.0, 2.0, 1.0, 9.0, 3.0, 8.0, 4.0, 7.0, 6.0],
            [0.0, 10.0, 20.0, 30.0] + [0.0] * 7,
        ]
    )
    out = rank_tiers(
        samples, ["a", "b"], make_tiers([("A", "a", "na"), ("A", "b", "nb")])
    ).set_index("player_id")
    assert out.loc["a", "p10"] == pytest.approx(1.0)
    assert out.loc["a", "p50"] == pytest.approx(5.0)
    assert out.loc["a", "p90"] == pytest.approx(9.0)
    expected_b = np.quantile(samples[1], [0.1, 0.5, 0.9])
    assert [out.loc["b", c] for c in ("p10", "p50", "p90")] == pytest.approx(expected_b.tolist())


# --- ranking options -------------------------------------------------------------------------


def test_rank_by_p_top():
    samples = np.array(
        [
            [100.0, 0.0, 0.0, 0.0],  # a: mean 25, wins one draw in four
            [5.0, 5.0, 5.0, 5.0],  # b: mean 5, wins three draws in four
            [1.0, 1.0, 1.0, 1.0],  # c: never wins
        ]
    )
    ids = ["a", "b", "c"]
    tiers = make_tiers([("A", "a", "na"), ("A", "b", "nb"), ("A", "c", "nc")])

    by_mean = tier_rows(rank_tiers(samples, ids, tiers), "A")
    assert list(by_mean["player_id"]) == ["a", "b", "c"]

    by_p_top = tier_rows(rank_tiers(samples, ids, tiers, rank_by="p_top"), "A")
    assert list(by_p_top["player_id"]) == ["b", "a", "c"]
    assert list(by_p_top["rank"]) == [1, 2, 3]
    assert by_p_top["p_top"].tolist() == pytest.approx([0.75, 0.25, 0.0])
    # The statistics themselves do not depend on the ranking key.
    by_mean_stats = by_mean.set_index("player_id")[["mean", "p10", "p50", "p90", "p_top"]]
    by_p_top_stats = by_p_top.set_index("player_id")[["mean", "p10", "p50", "p90", "p_top"]]
    pd.testing.assert_frame_equal(by_mean_stats.sort_index(), by_p_top_stats.sort_index())


# --- unprojected players ---------------------------------------------------------------------


def test_unprojected_players_listed_last():
    samples = np.array([[1.0, 2.0, 3.0], [10.0, 20.0, 30.0]])
    ids = ["a", "b"]
    tiers = make_tiers(
        [
            ("A", "x_missing", "Missing X"),  # listed first in the tier but has no draws
            ("A", "a", "Alpha"),
            ("A", "y_missing", "Missing Y"),
            ("A", "b", "Bravo"),
        ]
    )
    rows = tier_rows(rank_tiers(samples, ids, tiers), "A")
    assert len(rows) == 4
    assert list(rows["player_id"][:2]) == ["b", "a"]
    assert set(rows["player_id"][2:]) == {"x_missing", "y_missing"}
    assert list(rows["rank"]) == [1, 2, 3, 4]
    assert rows["projected"].tolist() == [True, True, False, False]
    assert dict(zip(rows["player_id"], rows["input_name"], strict=True))["x_missing"] == "Missing X"
    unproj = rows[~rows["projected"]]
    for col in ("mean", "p10", "p50", "p90", "p_top"):
        assert unproj[col].isna().all(), col
    proj = rows[rows["projected"]]
    for col in ("mean", "p10", "p50", "p90", "p_top"):
        assert proj[col].notna().all(), col
    assert proj["p_top"].sum() == pytest.approx(1.0)


def test_all_nan_samples_row_is_unprojected():
    samples = np.array(
        [
            [1.0, 2.0, 3.0],
            [np.nan, np.nan, np.nan],  # b: a row in samples, but no usable draws
            [3.0, 1.0, 5.0],
        ]
    )
    ids = ["a", "b", "c"]
    # b is listed first in the tier and would win on position alone.
    tiers = make_tiers([("A", "b", "Bravo"), ("A", "a", "Alpha"), ("A", "c", "Charlie")])
    for rank_by in ("mean", "p_top"):
        rows = tier_rows(rank_tiers(samples, ids, tiers, rank_by=rank_by), "A")
        assert list(rows["player_id"]) == ["c", "a", "b"], rank_by
        assert list(rows["rank"]) == [1, 2, 3]
        assert rows["projected"].tolist() == [True, True, False]
        last = rows.iloc[2]
        for col in ("mean", "p10", "p50", "p90", "p_top"):
            assert pd.isna(last[col]), col
        proj = rows[rows["projected"]]
        for col in ("mean", "p10", "p50", "p90", "p_top"):
            assert proj[col].notna().all(), col
        # Draws: a = [1, 2, 3], c = [3, 1, 5]; c tops draws 1 and 3, a tops draw 2.
        assert proj["mean"].tolist() == pytest.approx([3.0, 2.0])
        assert proj["p_top"].sum() == pytest.approx(1.0)
        assert dict(zip(proj["player_id"], proj["p_top"], strict=True)) == pytest.approx(
            {"c": 2 / 3, "a": 1 / 3}
        )


def test_all_nan_row_does_not_disturb_other_tiers_or_players():
    samples = np.array([[np.nan, np.nan], [4.0, 8.0], [6.0, 2.0]])
    ids = ["n", "x", "y"]
    tiers = make_tiers(
        [("A", "n", "Nan"), ("A", "x", "Xi"), ("A", "y", "Yo"), ("B", "n", "Nan again")]
    )
    out = rank_tiers(samples, ids, tiers)
    a = tier_rows(out, "A")
    assert a["projected"].tolist() == [True, True, False]
    assert a["p_top"].iloc[:2].tolist() == pytest.approx([0.5, 0.5])
    assert a["p_top"].iloc[:2].sum() == pytest.approx(1.0)
    b = tier_rows(out, "B")
    assert b["projected"].tolist() == [False]
    assert pd.isna(b["mean"].iloc[0])


# --- partially failed draws -------------------------------------------------------------------
# Ruling: NaN draws are skipped. A player with at least one finite draw is projected; mean and
# quantiles use that player's finite draws; p_top is computed per draw among the tier's players
# finite in that draw (ties split evenly); draws where no tier player is finite leave the
# denominator, so p_top sums to 1 in every tier with a projected player.

STATS = ("mean", "p10", "p50", "p90", "p_top")


def brute_force_p_top_nan(draws):
    """p_top per player (rows of draws) under the NaN-skipping policy, by looping over draws."""
    n_players, n_draws = draws.shape
    p = np.zeros(n_players)
    used = 0
    for d in range(n_draws):
        col = draws[:, d]
        finite = np.flatnonzero(np.isfinite(col))
        if len(finite) == 0:
            continue
        used += 1
        best = col[finite].max()
        winners = [i for i in finite if col[i] == best]
        for i in winners:
            p[i] += 1.0 / len(winners)
    return p / used


@pytest.mark.filterwarnings("error")
def test_player_with_one_failed_draw_is_projected_and_the_draw_goes_to_the_other_player():
    samples = np.array([[3.0, np.nan, 3.0], [1.0, 5.0, 1.0]])
    ids = ["a", "b"]
    tiers = make_tiers([("A", "a", "Alpha"), ("A", "b", "Bravo")])
    rows = tier_rows(rank_tiers(samples, ids, tiers), "A")

    # a: finite draws [3, 3] -> mean 3; b: [1, 5, 1] -> mean 7/3. a ranks first.
    assert list(rows["player_id"]) == ["a", "b"]
    assert rows["projected"].tolist() == [True, True]
    assert rows["mean"].tolist() == pytest.approx([3.0, 7.0 / 3.0])
    assert rows["p50"].tolist() == pytest.approx([3.0, 1.0])
    # Draw 1: a (3 > 1). Draw 2: only b is finite, so b wins. Draw 3: a (3 > 1).
    assert rows["p_top"].tolist() == pytest.approx([2.0 / 3.0, 1.0 / 3.0])
    assert rows["p_top"].sum() == pytest.approx(1.0)
    for col in STATS:
        assert rows[col].notna().all(), col


@pytest.mark.filterwarnings("error")
def test_partial_nan_rank_by_mean_uses_finite_draws_not_zero_fill():
    # a's finite draws average 10; if NaN counted as 0 its mean would be 5 and it would trail b.
    samples = np.array([[10.0, np.nan], [6.0, 6.0]])
    tiers = make_tiers([("A", "b", "Bravo"), ("A", "a", "Alpha")])
    rows = tier_rows(rank_tiers(samples, ["a", "b"], tiers), "A")
    assert list(rows["player_id"]) == ["a", "b"]
    assert rows["mean"].tolist() == pytest.approx([10.0, 6.0])
    # Draw 1: a (10 > 6). Draw 2: only b is finite.
    assert rows["p_top"].tolist() == pytest.approx([0.5, 0.5])


@pytest.mark.filterwarnings("error")
def test_draw_that_failed_for_every_tier_player_is_left_out_of_the_denominator():
    samples = np.array(
        [
            [3.0, np.nan, 1.0],  # a
            [1.0, np.nan, 3.0],  # b
            [10.0, 20.0, 30.0],  # c: other tier, finite in draw 2
            [1.0, 2.0, 3.0],  # d
        ]
    )
    ids = ["a", "b", "c", "d"]
    tiers = make_tiers(
        [("A", "a", "Alpha"), ("A", "b", "Bravo"), ("B", "c", "Charlie"), ("B", "d", "Delta")]
    )
    out = rank_tiers(samples, ids, tiers)

    a = tier_rows(out, "A").set_index("player_id")
    # Draw 2 is excluded for tier A: a wins draw 1, b wins draw 3 -> 1/2 each (not 1/3 each).
    assert a.loc["a", "p_top"] == pytest.approx(0.5)
    assert a.loc["b", "p_top"] == pytest.approx(0.5)
    assert a["p_top"].sum() == pytest.approx(1.0)
    assert a.loc["a", "mean"] == pytest.approx(2.0)
    assert a.loc["b", "mean"] == pytest.approx(2.0)
    assert a["projected"].all()

    # The same draw is finite in tier B, so it counts there: c wins all three.
    b = tier_rows(out, "B").set_index("player_id")
    assert b.loc["c", "p_top"] == pytest.approx(1.0)
    assert b.loc["d", "p_top"] == pytest.approx(0.0)


@pytest.mark.filterwarnings("error")
def test_p_top_sums_to_one_when_a_draw_is_all_nan_for_a_tier_of_three():
    samples = np.array(
        [
            [5.0, np.nan, 5.0, 1.0],
            [5.0, np.nan, 2.0, np.nan],
            [1.0, np.nan, 9.0, 4.0],
        ]
    )
    ids = ["a", "b", "c"]
    tiers = make_tiers([("A", p, f"n{p}") for p in ids])
    rows = rank_tiers(samples, ids, tiers).set_index("player_id")
    # Draw 1: a,b tie at 5 -> 1/2 each. Draw 2 excluded. Draw 3: c. Draw 4: a=1 < c=4 -> c.
    # Three usable draws.
    assert rows.loc["a", "p_top"] == pytest.approx(1.0 / 6.0)
    assert rows.loc["b", "p_top"] == pytest.approx(1.0 / 6.0)
    assert rows.loc["c", "p_top"] == pytest.approx(2.0 / 3.0)
    assert rows["p_top"].sum() == pytest.approx(1.0)


@pytest.mark.filterwarnings("error")
def test_partial_nan_mean_and_quantiles_equal_numpy_on_the_finite_draws():
    samples = np.array(
        [
            [8.0, np.nan, 2.0, 6.0, np.nan, 4.0, 10.0, 0.0],  # a
            [np.nan, 1.0, 1.5, np.nan, 9.0, 3.0, 7.5, 2.5],  # b
            [4.0, 4.5, np.nan, 3.5, 5.0, np.nan, np.nan, 6.0],  # c
        ]
    )
    ids = ["a", "b", "c"]
    tiers = make_tiers([("A", p, f"n{p}") for p in ids])
    out = rank_tiers(samples, ids, tiers).set_index("player_id")
    for i, pid in enumerate(ids):
        finite = samples[i][np.isfinite(samples[i])]
        q = np.quantile(finite, [0.1, 0.5, 0.9])
        assert out.loc[pid, "mean"] == pytest.approx(finite.mean(), rel=1e-12), pid
        assert out.loc[pid, "p10"] == pytest.approx(q[0], rel=1e-12), pid
        assert out.loc[pid, "p50"] == pytest.approx(q[1], rel=1e-12), pid
        assert out.loc[pid, "p90"] == pytest.approx(q[2], rel=1e-12), pid
    assert out["projected"].all()


@pytest.mark.filterwarnings("error")
def test_partial_nan_custom_quantiles_use_finite_draws():
    draws = np.linspace(0.0, 1000.0, 21)
    row = draws.copy()
    row[[2, 5, 11]] = np.nan
    samples = np.vstack([row, np.full(21, 1.0)])
    tiers = make_tiers([("A", "a", "na"), ("A", "b", "nb")])
    out = rank_tiers(samples, ["a", "b"], tiers, quantiles=(0.025, 0.975)).set_index("player_id")
    finite = row[np.isfinite(row)]
    assert out.loc["a", "p2.5"] == pytest.approx(np.quantile(finite, 0.025))
    assert out.loc["a", "p97.5"] == pytest.approx(np.quantile(finite, 0.975))


@pytest.mark.filterwarnings("error")
def test_p_top_matches_nan_skipping_brute_force_on_random_draws():
    rng = np.random.default_rng(42)
    samples = np.round(rng.normal(50.0, 20.0, size=(7, 300)))  # rounding creates ties
    samples[rng.random(samples.shape) < 0.35] = np.nan
    samples[:, 17] = np.nan  # a draw that failed for everyone
    samples[[0, 2, 4], 40] = np.nan  # a draw that failed for all of tier X only
    for i in range(7):  # every row keeps at least one finite draw
        samples[i, i] = 25.0 + i
    ids = [f"p{i}" for i in range(7)]
    members = {"X": [0, 2, 4], "Y": [1, 3, 5, 6]}
    tiers = make_tiers([(t, ids[i], f"n{i}") for t, idx in members.items() for i in idx])
    out = rank_tiers(samples, ids, tiers)
    for tier, idx in members.items():
        expected = brute_force_p_top_nan(samples[idx])
        got = out[out["tier"] == tier].set_index("player_id")["p_top"]
        assert got.loc[[ids[i] for i in idx]].tolist() == pytest.approx(expected.tolist())
        assert got.sum() == pytest.approx(1.0)
    by_id = out.set_index("player_id")
    for i, pid in enumerate(ids):
        finite = samples[i][np.isfinite(samples[i])]
        assert by_id.loc[pid, "mean"] == pytest.approx(finite.mean(), rel=1e-12)
        assert by_id.loc[pid, "p50"] == pytest.approx(np.quantile(finite, 0.5), rel=1e-12)


@pytest.mark.filterwarnings("error")
def test_all_nan_row_beside_partial_nan_rows_stays_unprojected_without_warnings():
    samples = np.array(
        [
            [3.0, np.nan, 3.0],  # a: partial
            [np.nan, np.nan, np.nan],  # n: unusable
            [1.0, 5.0, 1.0],  # b
        ]
    )
    ids = ["a", "n", "b"]
    tiers = make_tiers([("A", "n", "Nan"), ("A", "a", "Alpha"), ("A", "b", "Bravo")])
    for rank_by in ("mean", "p_top"):
        rows = tier_rows(rank_tiers(samples, ids, tiers, rank_by=rank_by), "A")
        assert list(rows["player_id"]) == ["a", "b", "n"], rank_by
        assert rows["projected"].tolist() == [True, True, False]
        for col in STATS:
            assert pd.isna(rows.iloc[2][col]), col
        assert rows["p_top"].iloc[:2].tolist() == pytest.approx([2.0 / 3.0, 1.0 / 3.0])


@pytest.mark.filterwarnings("error")
def test_markdown_renders_partial_nan_players_with_values():
    samples = np.array([[3.0, np.nan, 3.0], [1.0, 5.0, 1.0]])
    tiers = make_tiers([("A", "a", "Alpha"), ("A", "b", "Bravo")])
    md = render_picks_markdown(rank_tiers(samples, ["a", "b"], tiers), "Partial")
    (block,) = table_blocks(md)
    alpha, bravo = block[2], block[3]
    assert "Alpha" in alpha and "$3" in alpha and "66.7%" in alpha
    assert "Bravo" in bravo and "$2" in bravo and "33.3%" in bravo
    assert "nan" not in md.lower()


@pytest.mark.parametrize("n_players", [1, 3])
def test_zero_draw_columns_leave_everyone_unprojected(n_players):
    ids = [f"p{i}" for i in range(n_players)]
    samples = np.empty((n_players, 0))
    tiers = make_tiers([("A", pid, f"Name {pid}") for pid in ids] + [("B", ids[0], "Again")])
    out = rank_tiers(samples, ids, tiers)
    assert len(out) == n_players + 1
    assert list(out.columns) == COLUMNS
    assert not out["projected"].any()
    for col in ("mean", "p10", "p50", "p90", "p_top"):
        assert out[col].isna().all(), col
    a = tier_rows(out, "A")
    assert sorted(a["player_id"]) == sorted(ids)
    assert list(a["rank"]) == list(range(1, n_players + 1))


def test_unprojected_last_regardless_of_rank_by():
    samples = np.array([[1.0, 1.0], [2.0, 2.0]])
    tiers = make_tiers([("A", "ghost", "G"), ("A", "a", "na"), ("A", "b", "nb")])
    rows = tier_rows(rank_tiers(samples, ["a", "b"], tiers, rank_by="p_top"), "A")
    assert list(rows["player_id"][:2]) == ["b", "a"]
    assert rows["player_id"].iloc[2] == "ghost"
    assert not rows["projected"].iloc[2]


def test_tier_with_only_unprojected_players_is_still_listed():
    samples = np.array([[1.0, 2.0]])
    tiers = make_tiers([("A", "a", "na"), ("B", "g1", "Ghost 1"), ("B", "g2", "Ghost 2")])
    out = rank_tiers(samples, ["a"], tiers)
    b = tier_rows(out, "B")
    assert sorted(b["player_id"]) == ["g1", "g2"]
    assert list(b["rank"]) == [1, 2]
    assert not b["projected"].any()
    assert b["mean"].isna().all()


def test_player_ids_order_and_extra_players_do_not_matter():
    # samples rows follow player_ids (not the tiers order); players outside every tier are ignored.
    samples = np.array([[100.0, 100.0], [1.0, 1.0], [50.0, 50.0]])
    ids = ["outsider", "low", "mid"]
    tiers = make_tiers([("A", "mid", "Mid"), ("A", "low", "Low")])
    out = rank_tiers(samples, ids, tiers)
    assert list(out["player_id"]) == ["mid", "low"]
    assert out["mean"].tolist() == pytest.approx([50.0, 1.0])
    assert out["p_top"].tolist() == pytest.approx([1.0, 0.0])


def test_tiers_without_input_name_column():
    samples = np.array([[3.0, 3.0], [1.0, 1.0]])
    tiers = pd.DataFrame({"tier": ["A", "A"], "player_id": ["a", "b"]})
    out = rank_tiers(samples, ["a", "b"], tiers)
    assert list(out.columns) == COLUMNS
    assert list(out["player_id"]) == ["a", "b"]


# --- tier order and determinism --------------------------------------------------------------


def test_tier_order_follows_first_appearance_in_tiers_frame():
    samples = np.arange(1.0, 7.0)[:, None] * np.ones((1, 5))
    ids = [f"p{i}" for i in range(6)]
    # Tier labels deliberately not in sorted order; tiers interleaved in the input.
    tiers = make_tiers(
        [
            ("C", "p0", "n0"),
            ("A", "p1", "n1"),
            ("C", "p2", "n2"),
            ("B", "p3", "n3"),
            ("A", "p4", "n4"),
            ("B", "p5", "n5"),
        ]
    )
    out = rank_tiers(samples, ids, tiers)
    order = list(dict.fromkeys(out["tier"]))
    assert order == ["C", "A", "B"]
    # Each tier's rows are contiguous.
    assert out["tier"].tolist() == ["C", "C", "A", "A", "B", "B"]
    assert out["rank"].tolist() == [1, 2, 1, 2, 1, 2]


def test_output_is_deterministic(two_tier_case):
    samples, ids, tiers = two_tier_case
    samples_before, tiers_before = samples.copy(), tiers.copy()
    first = rank_tiers(samples, ids, tiers)
    second = rank_tiers(samples, ids, tiers)
    pd.testing.assert_frame_equal(first, second)
    # Inputs are not mutated.
    np.testing.assert_array_equal(samples, samples_before)
    pd.testing.assert_frame_equal(tiers, tiers_before)


def test_deterministic_with_exact_ties_in_the_ranking_key():
    samples = np.array([[4.0, 4.0, 4.0]] * 5)
    ids = ["e", "d", "c", "b", "a"]
    tiers = make_tiers([("A", p, f"n{p}") for p in ids])
    runs = [rank_tiers(samples, ids, tiers) for _ in range(3)]
    for r in runs[1:]:
        pd.testing.assert_frame_equal(runs[0], r)
    assert list(runs[0]["rank"]) == [1, 2, 3, 4, 5]
    assert runs[0]["p_top"].tolist() == pytest.approx([0.2] * 5)


# --- quantile labels -------------------------------------------------------------------------

QUANTILE_LABELS = [
    ((0.1, 0.5, 0.9), ["p10", "p50", "p90"]),
    ((0.025, 0.5, 0.975), ["p2.5", "p50", "p97.5"]),
    ((0.05, 0.95), ["p5", "p95"]),
    ((0.125, 0.875), ["p12.5", "p87.5"]),
    ((0.07, 0.29), ["p7", "p29"]),  # 0.07 * 100 and 0.29 * 100 are not exact in floating point
]


def quantile_case():
    draws = np.array(
        [
            np.linspace(0.0, 1_000_000.0, 41),  # a
            np.linspace(0.0, 2_000_000.0, 41) + 7_000.0,  # b
        ]
    )
    ids = ["a", "b"]
    tiers = make_tiers([("A", "a", "Abe Ay"), ("A", "b", "Bo Bee")])
    return draws, ids, tiers


@pytest.mark.parametrize(("quantiles", "labels"), QUANTILE_LABELS)
def test_quantile_columns_are_labelled_without_losing_precision(quantiles, labels):
    draws, ids, tiers = quantile_case()
    out = rank_tiers(draws, ids, tiers, quantiles=quantiles)
    assert list(out.columns) == [
        "tier",
        "rank",
        "player_id",
        "input_name",
        "projected",
        "mean",
        *labels,
        "p_top",
    ]
    by_id = out.set_index("player_id")
    for pid, row in zip(ids, draws, strict=True):
        for q, label in zip(quantiles, labels, strict=True):
            assert by_id.loc[pid, label] == pytest.approx(np.quantile(row, q)), (pid, label)


@pytest.mark.parametrize(
    ("quantiles", "headers"),
    [
        ((0.025, 0.5, 0.975), ["P2.5", "P50", "P97.5"]),
        ((0.1, 0.5, 0.9), ["P10", "P50", "P90"]),
        ((0.05, 0.95), ["P5", "P95"]),
    ],
)
def test_markdown_headers_and_values_follow_quantile_labels(quantiles, headers):
    draws, ids, tiers = quantile_case()
    md = render_picks_markdown(rank_tiers(draws, ids, tiers, quantiles=quantiles), "Q")
    (block,) = table_blocks(md)
    header = [c.strip() for c in block[0].strip().strip("|").split("|")]
    assert header[2] == "Mean"
    assert header[3 : 3 + len(headers)] == headers
    assert header[-1] == "P(top)"
    for pid, line in zip(("b", "a"), block[2:], strict=True):  # b has the higher mean
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        row = draws[ids.index(pid)]
        expected = [f"${np.quantile(row, q):,.0f}" for q in quantiles]
        assert cells[3 : 3 + len(headers)] == expected, pid


# --- markdown --------------------------------------------------------------------------------


def table_blocks(md):
    """Contiguous runs of lines starting with '|' (markdown tables), each as a list of lines."""
    blocks, cur = [], []
    for line in md.splitlines():
        if line.strip().startswith("|"):
            cur.append(line)
        elif cur:
            blocks.append(cur)
            cur = []
    if cur:
        blocks.append(cur)
    return blocks


@pytest.fixture
def markdown_case():
    samples = np.array(
        [
            [1_234_567.0] * 4,  # a: constant, dominates its tier
            [900_000.0] * 4,  # b
            [2_500_000.0] * 4,  # c
            [40_000.0] * 4,  # d
        ]
    )
    ids = ["a", "b", "c", "d"]
    # Tier "2" appears first in the frame, so its table comes first.
    tiers = make_tiers(
        [
            (2, "c", "Cora Cee"),
            (2, "d", "Dan Dee"),
            (1, "a", "Abe Ay"),
            (1, "b", "Bo Bee"),
            (1, "nodraws", "Nia Nodraws"),
        ]
    )
    return rank_tiers(samples, ids, tiers)


def test_markdown_has_table_per_tier(markdown_case):
    md = render_picks_markdown(markdown_case, "Picks 2027")
    assert isinstance(md, str)
    assert "Picks 2027" in md

    blocks = table_blocks(md)
    assert len(blocks) == 2

    first, second = ("\n".join(b) for b in blocks)
    # Tier order follows the frame: tier 2 (Cora, Dan) then tier 1 (Abe, Bo, Nia).
    for name in ("Cora Cee", "Dan Dee"):
        assert name in first and name not in second
    for name in ("Abe Ay", "Bo Bee", "Nia Nodraws"):
        assert name in second and name not in first
    # Within a table: players are listed in rank order.
    assert first.index("Cora Cee") < first.index("Dan Dee")
    assert second.index("Abe Ay") < second.index("Bo Bee") < second.index("Nia Nodraws")
    # Header, separator and one row per player.
    assert len(blocks[0]) == 2 + 2
    assert len(blocks[1]) == 2 + 3
    assert re.fullmatch(r"\|?[\s:|-]+\|?", blocks[0][1].strip())
    assert "---" in blocks[0][1]


def test_markdown_dollar_formatting(markdown_case):
    md = render_picks_markdown(markdown_case, "Picks")
    for value in ("$1,234,567", "$900,000", "$2,500,000", "$40,000"):
        assert value in md
    assert "1234567" not in md
    assert "2500000" not in md


def test_markdown_table_count_matches_tier_count():
    rng = np.random.default_rng(1)
    samples = rng.uniform(1e5, 1e6, size=(10, 50))
    ids = [f"p{i}" for i in range(10)]
    tiers = make_tiers([(f"T{i // 2}", ids[i], f"Name{i}") for i in range(10)])
    md = render_picks_markdown(rank_tiers(samples, ids, tiers), "Five tiers")
    blocks = table_blocks(md)
    assert len(blocks) == 5
    for i, block in enumerate(blocks):
        text = "\n".join(block)
        assert f"Name{2 * i}" in text and f"Name{2 * i + 1}" in text
        assert len(block) == 4

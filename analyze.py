"""Describe how weekly domestic box office decays after a film opens.

Reads data/movies.csv, data/weekly.csv, data/weekend.csv and
data/scrape_meta.json (written by scrape.py) and writes figures/*.png plus
data/summary_stats.json, which REPORT.md quotes.

    python analyze.py
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.collections import LineCollection
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, NullFormatter

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
FIG_DIR = ROOT / "figures"

MAX_WEEK = 15
INCOMPLETE_RUN_WEEKS = 16
CASE_STUDY_PATH = "/movie/Fall-Guy-The-(2024)"
CONTRAST_MIN_WEEK1 = 40e6
DECOMPOSITION_WEEKS = 8
OPENING_BUCKETS = [
    ("Under $10M", 0.0, 10e6),
    ("$10M-40M", 10e6, 40e6),
    ("$40M-100M", 40e6, 100e6),
    ("$100M+", 100e6, np.inf),
]
N_GENRES = 6
ANIMATION_METHODS = {"Digital Animation", "Hand Animation", "Stop-Motion Animation"}
MIN_SHARE_REPORTING = 0.5
MIN_FILMS_REPORTING = 10
RERELEASE_GAP_WEEKS = 3
# Every theatrical release by these distributors in 2023-2026 is a fixed-length event.
EVENT_DISTRIBUTORS = ("Fathom", "AMC Theatres Distribution", "Netflix")
MANUAL_EXCLUSIONS = {
    "/movie/Kill-Bill-The-Whole-Bloody-Affair-(2025)": "older film (re-release or late theatrical debut)",
}
LOG_FLOOR = 5e-4

BLUE, ORANGE, GREEN, RED, PURPLE, GREY = "#2b6cb0", "#dd6b20", "#2f855a", "#c53030", "#6b46c1", "#718096"
GROUP_COLORS = [BLUE, ORANGE, GREEN, RED, PURPLE, "#b7791f", "#319795"]

plt.rcParams.update(
    {
        "figure.dpi": 100,
        "savefig.dpi": 150,
        "savefig.bbox": "tight",
        "font.size": 11,
        "axes.titlesize": 14,
        "axes.titleweight": "bold",
        "axes.titlepad": 12,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.3,
        "legend.frameon": False,
    }
)
PERCENT = FuncFormatter(lambda value, _: f"{value * 100:g}%")


# ---------------------------------------------------------------- calendar


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """n-th given weekday (Mon=0) of a month; n=-1 means the last one."""
    if n > 0:
        first = dt.date(year, month, 1)
        return first + dt.timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)
    return last - dt.timedelta(days=(last.weekday() - weekday) % 7)


def _fourth_of_july_weekend(year: int) -> tuple[dt.date, dt.date]:
    """The long weekend people actually get off around July 4."""
    day = dt.date(year, 7, 4)
    weekday = day.weekday()
    if weekday in (0, 1):  # Monday/Tuesday: from the Friday before
        return day - dt.timedelta(days=(weekday - 4) % 7), day
    if weekday in (3, 4):  # Thursday/Friday: through the Sunday after
        return day, day + dt.timedelta(days=6 - weekday)
    if weekday == 5:  # Saturday, observed Friday
        return day - dt.timedelta(days=1), day + dt.timedelta(days=1)
    if weekday == 6:  # Sunday, observed Monday
        return day - dt.timedelta(days=2), day + dt.timedelta(days=1)
    return day, day


def holiday_periods(years: range) -> list[tuple[dt.date, dt.date, str]]:
    """Moviegoing holiday periods: Monday holidays include the weekend before,
    Thanksgiving runs Wednesday to Sunday, and Christmas runs to New Year's Day."""
    periods = []
    for year in years:
        for month, n, name in ((1, 3, "MLK Day"), (2, 3, "Presidents Day"), (5, -1, "Memorial Day"), (9, 1, "Labor Day")):
            monday = _nth_weekday(year, month, 0, n)
            periods.append((monday - dt.timedelta(days=3), monday, name))
        periods.append((*_fourth_of_july_weekend(year), "Fourth of July"))
        thanksgiving = _nth_weekday(year, 11, 3, 4)
        periods.append((thanksgiving - dt.timedelta(days=1), thanksgiving + dt.timedelta(days=3), "Thanksgiving"))
        periods.append((dt.date(year, 12, 24), dt.date(year + 1, 1, 1), "Christmas-New Year's"))
    return periods


HOLIDAY_PERIODS = holiday_periods(range(2022, 2028))


def holiday_in_chart_week(friday: pd.Timestamp) -> str | None:
    """Holiday period overlapping the Friday-to-Thursday chart week starting `friday`."""
    start = friday.date()
    end = start + dt.timedelta(days=6)
    for period_start, period_end, name in HOLIDAY_PERIODS:
        if period_start <= end and start <= period_end:
            return name
    return None


# ---------------------------------------------------------------- data prep


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict]:
    movies = pd.read_csv(DATA_DIR / "movies.csv", parse_dates=["open_chart_date"])
    weekly = pd.read_csv(DATA_DIR / "weekly.csv", parse_dates=["week_date"])
    weekend = pd.read_csv(DATA_DIR / "weekend.csv", parse_dates=["week_date"])
    meta = json.loads((DATA_DIR / "scrape_meta.json").read_text())
    return movies, weekly, weekend, meta


def exclusion_reasons(movies: pd.DataFrame) -> pd.Series:
    """Why a chart-qualified wide opening is left out of the run-shape sample ('' = kept).

    Event cinema (one-night TV episodes, concert and stage recordings, Fathom
    engagements) is booked for a fixed handful of days, so its "decay" is a
    scheduling decision rather than audiences thinning out. Older films that
    reached theaters years late (Pixar's streaming titles in 2024) are not new
    releases, and platform releases that went wide mid-week have a first week
    that is mostly a limited run.
    """
    open_year = movies["open_chart_date"].dt.year
    title_year = pd.to_numeric(movies["page_title"].str.extract(r"\((\d{4})\)\s*$")[0], errors="coerce")
    release_year = pd.to_numeric(movies["release_date_text"].str.extract(r"(\d{4})$")[0], errors="coerce")
    older_film = (title_year < open_year - 1) | (release_year < open_year)
    event = (
        (movies["release_type"] == "Special Engagement")
        | movies["distributor"].fillna("").str.contains("|".join(EVENT_DISTRIBUTORS), regex=True)
        | (movies["genre"] == "Concert/Performance")
    )
    platform = movies["release_lines"].fillna("").str.contains("Expands Wide")
    reasons = pd.Series("", index=movies.index)
    reasons[movies["n_weeks"] == 0] = "no weekly table"
    manual = movies["movie_path"].map(MANUAL_EXCLUSIONS)
    reasons[manual.notna()] = manual[manual.notna()]
    reasons[(reasons == "") & older_film] = "older film (re-release or late theatrical debut)"
    reasons[(reasons == "") & event] = "event cinema (special engagement, concert or stage recording, event distributor)"
    reasons[(reasons == "") & platform] = "platform release (opened limited, expanded wide mid-week)"
    return reasons


def extend_with_weekends(weekly: pd.DataFrame, weekend: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Estimate full weeks that only have a weekend figure.

    Distributors sometimes report a weekend (usually the last one) but not the
    full week around it. Such a week is estimated as the weekend gross times the
    film's own week-to-weekend ratio from the nearest earlier week that has both,
    so the film is not counted as gone while it is still playing.
    """
    keys = ["movie_path", "week_date"]
    first_week = weekend["movie_path"].map(weekly.groupby("movie_path")["week_date"].min())
    unmatched = ~pd.MultiIndex.from_frame(weekend[keys]).isin(pd.MultiIndex.from_frame(weekly[keys]))
    missing = weekend[unmatched & (weekend["week_date"] >= first_week)]
    if missing.empty:
        return weekly.assign(estimated=False), 0

    both = weekly.merge(weekend[keys + ["gross"]], on=keys, suffixes=("", "_weekend"))
    both["ratio"] = both["gross"] / both["gross_weekend"]
    estimated = pd.merge_asof(
        missing.sort_values("week_date"),
        both[keys + ["ratio"]].sort_values("week_date"),
        on="week_date",
        by="movie_path",
        direction="backward",
    )
    estimated["ratio"] = estimated["ratio"].fillna(both["ratio"].median())
    weekend_gross = estimated["gross"].copy()
    estimated["gross"] = weekend_gross * estimated["ratio"]
    # Running total through Sunday plus the estimated Monday-Thursday takings.
    estimated["total_gross"] = estimated["total_gross"] + estimated["gross"] - weekend_gross
    estimated["per_theater"] = estimated["gross"] / estimated["theaters"]
    estimated[["week", "rank", "pct_change"]] = np.nan
    extended = pd.concat(
        [weekly.assign(estimated=False), estimated.drop(columns="ratio").assign(estimated=True)],
        ignore_index=True,
    )
    return extended, len(estimated)


def check_data(movies: pd.DataFrame, weekly: pd.DataFrame) -> dict:
    """Consistency checks between the weekly charts and the movie pages."""
    titles = movies.set_index("movie_path")["title"]

    merged = weekly.merge(movies[["movie_path", "open_chart_date"]], on="movie_path")
    date_week = (merged["week_date"] - merged["open_chart_date"]).dt.days // 7 + 1
    week_number_mismatch = sorted(merged.loc[date_week != merged["week"], "movie_path"].unique())

    page_week1 = weekly[weekly["week"] == 1].set_index("movie_path")["gross"]
    chart_week1 = movies.set_index("movie_path")["open_week_gross"]
    week1_gap = ((page_week1.reindex(chart_week1.index) - chart_week1).abs() / chart_week1).dropna()
    week1_mismatch = week1_gap[week1_gap > 0.01]

    week_counts = weekly.groupby("movie_path")["week"].agg(["min", "max", "nunique"])
    week_counts["gaps"] = week_counts["max"] - week_counts["min"] + 1 - week_counts["nunique"]
    films_with_gaps = week_counts[week_counts["gaps"] > 0]

    return {
        "movies": len(movies),
        "movie_weeks": len(weekly),
        "no_weekly_table": movies.loc[movies["n_weeks"] == 0, "title"].tolist(),
        "page_week1_missing": int(chart_week1.index.difference(page_week1.index).size),
        "week1_gross_matches_chart_within_1pct": int((week1_gap <= 0.01).sum()),
        "week1_gross_mismatches": {titles[path]: round(float(gap), 4) for path, gap in week1_mismatch.items()},
        "week_number_vs_date_mismatches": [titles[path] for path in week_number_mismatch],
        "films_with_week_gaps": int(len(films_with_gaps)),
        "missing_weeks_total": int(films_with_gaps["gaps"].sum()),
        "films_with_week_gaps_examples": [titles[path] for path in films_with_gaps.index[:10]],
        "duplicate_movie_weeks": int(weekly.duplicated(["movie_path", "week"]).sum()),
    }


def align_weeks(movies: pd.DataFrame, weekly: pd.DataFrame) -> pd.DataFrame:
    """One row per film and week, where week 1 is always a full opening week.

    A film that opens mid-week (a Wednesday before Thanksgiving, Christmas Day)
    shows up first in a 1-6 day chart week; those days are folded into the first
    full Friday-to-Thursday week, the way a "5-day opening" is usually quoted.
    """
    merged = weekly.merge(movies[["movie_path", "open_chart_date", "partial_opening"]], on="movie_path")
    merged["chart_week"] = (merged["week_date"] - merged["open_chart_date"]).dt.days // 7 + 1
    merged["week"] = np.where(
        merged["partial_opening"], (merged["chart_week"] - 1).clip(lower=1), merged["chart_week"]
    )
    aligned = merged.groupby(["movie_path", "week"], as_index=False).agg(
        week_date=("week_date", "max"),
        chart_weeks=("chart_week", "nunique"),
        gross=("gross", "sum"),
        theaters=("theaters", "max"),
        total_gross=("total_gross", "max"),
        rank=("rank", "min"),
        estimated=("estimated", "max"),
    )
    aligned["per_theater"] = aligned["gross"] / aligned["theaters"]
    aligned["holiday"] = [
        holiday_in_chart_week(date) if week > 1 else None
        for date, week in zip(aligned["week_date"], aligned["week"])
    ]
    return aligned


def initial_runs(aligned: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Cut each film's weeks at its first absence of RERELEASE_GAP_WEEKS or more.

    A movie page keeps later returns to theaters (an IMAX re-release, a
    sing-along version) in the same weekly table; those are not part of the
    original run.
    """
    aligned = aligned.sort_values(["movie_path", "week"])
    missing_before = aligned.groupby("movie_path")["week"].diff() - 1
    after_gap = (missing_before >= RERELEASE_GAP_WEEKS).groupby(aligned["movie_path"]).cumsum() > 0
    return aligned[~after_gap], sorted(aligned.loc[after_gap, "movie_path"].unique())


def genre_group(row: pd.Series) -> str:
    if row["production_method"] in ANIMATION_METHODS:
        return "Animation"
    return row["genre"] if isinstance(row["genre"], str) else "Unknown"


def build_films(movies: pd.DataFrame, aligned: pd.DataFrame, latest: pd.Timestamp,
                rereleased: list[str]) -> pd.DataFrame:
    films = movies.set_index("movie_path")
    week1 = aligned[aligned["week"] == 1].set_index("movie_path")
    by_film = aligned.groupby("movie_path")
    films["week1_gross"] = week1["gross"]
    films["week1_theaters"] = week1["theaters"]
    films["week1_per_theater"] = week1["per_theater"]
    # Friday that starts week 1's last (or only) chart week.
    films["week1_date"] = films["open_chart_date"] + pd.to_timedelta(films["partial_opening"].astype(int) * 7, unit="D")
    films["week1_chart_weeks"] = week1["chart_weeks"]
    films["last_week"] = by_film["week"].max()
    films["rereleased"] = films.index.isin(rereleased)
    run_total = by_film["total_gross"].max()
    # The page's domestic total also counts re-release weeks, which the run excludes.
    films["final_gross"] = run_total.where(
        films["rereleased"], pd.concat([films["domestic_total"], run_total], axis=1).max(axis=1)
    )
    films["multiple"] = films["final_gross"] / films["week1_gross"]
    films["weeks_observable"] = (latest - films["week1_date"]).dt.days // 7 + 1
    films["incomplete_run"] = films["open_chart_date"] > latest - pd.Timedelta(weeks=INCOMPLETE_RUN_WEEKS)
    films["holiday_in_week2"] = (films["week1_date"] + pd.Timedelta(weeks=1)).map(holiday_in_chart_week)
    films["genre_group"] = films.apply(genre_group, axis=1)
    films["opening_bucket"] = pd.cut(
        films["week1_gross"],
        bins=[low for _, low, _ in OPENING_BUCKETS] + [np.inf],
        labels=[name for name, _, _ in OPENING_BUCKETS],
        right=False,
    )
    films["display_title"] = films["page_title"].fillna(films["title"])
    return films


def week_matrix(aligned: pd.DataFrame, films: pd.DataFrame, column: str, max_week: int = MAX_WEEK) -> pd.DataFrame:
    """Films x weeks. Weeks after a film's last reported week count as 0 (it left
    the chart); weeks that have not happened yet are NaN, as are mid-run gaps."""
    weeks = np.arange(1, max_week + 1)
    values = (
        aligned.pivot(index="movie_path", columns="week", values=column)
        .reindex(index=films.index, columns=weeks)
        .to_numpy(dtype=float, copy=True)
    )
    last = films["last_week"].to_numpy()[:, None]
    observable = films["weeks_observable"].to_numpy()[:, None]
    values[(weeks > last) & (weeks <= observable)] = 0.0
    values[weeks > observable] = np.nan
    return pd.DataFrame(values, index=films.index, columns=weeks)


def relative_to_week1(matrix: pd.DataFrame) -> pd.DataFrame:
    return matrix.div(matrix[1], axis=0)


def cumulative_share(aligned: pd.DataFrame, films: pd.DataFrame, max_week: int = MAX_WEEK) -> pd.DataFrame:
    weeks = np.arange(1, max_week + 1)
    running_total = (
        aligned.pivot(index="movie_path", columns="week", values="total_gross")
        .reindex(index=films.index, columns=weeks)
        .ffill(axis=1)
    )
    return running_total.div(films["final_gross"], axis=0).clip(upper=1.0)


def quantiles(matrix: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "n": matrix.notna().sum(),
            "q25": matrix.quantile(0.25),
            "median": matrix.median(),
            "q75": matrix.quantile(0.75),
        }
    )


def decay_summary(rel: pd.DataFrame) -> pd.DataFrame:
    """Per week: the spread of gross relative to week 1 among films still
    reporting, and the share of films (whose week has come) still reporting.

    Taking quantiles over films still in release keeps "how fast takings fall"
    separate from "how long films stay"; counting departed films as zero would
    drag the median to zero as soon as half of them had left.
    """
    summary = quantiles(rel.where(rel > 0))
    summary["observable"] = rel.notna().sum()
    summary["share_reporting"] = summary["n"] / summary["observable"]
    summary["median_counting_departed_as_zero"] = rel.median()
    return summary


def reliable_weeks(summary: pd.DataFrame) -> pd.Series:
    """Weeks where the still-reporting films are a majority of the group."""
    return (summary["share_reporting"] >= MIN_SHARE_REPORTING) & (summary["n"] >= MIN_FILMS_REPORTING)


def week_over_week(aligned: pd.DataFrame) -> pd.DataFrame:
    """Each reported week's gross against the immediately preceding week."""
    ordered = aligned.sort_values(["movie_path", "week"]).copy()
    previous = ordered.groupby("movie_path")[["week", "gross"]].shift(1)
    consecutive = previous["week"] == ordered["week"] - 1
    ordered["wow_change"] = np.where(consecutive, ordered["gross"] / previous["gross"] - 1, np.nan)
    return ordered


# ---------------------------------------------------------------- plotting


def _log_percent_axis(ax: plt.Axes, low: float = LOG_FLOOR, high: float = 2.0) -> None:
    ax.set_yscale("log")
    ax.set_ylim(low, high)
    ax.yaxis.set_major_formatter(PERCENT)
    ax.yaxis.set_minor_formatter(NullFormatter())


def _week_axis(ax: plt.Axes, max_week: int = MAX_WEEK) -> None:
    ax.set_xlim(0.6, max_week + 0.4)
    ax.set_xticks(range(1, max_week + 1))
    ax.set_xlabel("Week of release (week 1 = opening week)")


def _money(value: float) -> str:
    return f"${value / 1e6:.1f}M" if value >= 1e6 else f"${value / 1e3:.0f}K"


def _plot_median(ax: plt.Axes, summary: pd.DataFrame, color: str, label: str, lw: float = 2.5,
                 marker: str = "o", ms: float = 4, dashed_tail: bool = False) -> None:
    """Median line over reliable weeks; optionally continue it dashed afterwards."""
    reliable = reliable_weeks(summary)
    solid = summary["median"].where(reliable)
    ax.plot(summary.index, solid, color=color, lw=lw, marker=marker, ms=ms, label=label)
    if dashed_tail and not reliable.all():
        start = reliable[reliable].index.max()
        tail = summary.loc[start:, "median"]
        ax.plot(tail.index, tail, color=color, lw=lw * 0.6, ls="--", marker=marker, ms=ms * 0.7, alpha=0.8)


def _save(fig: plt.Figure, name: str) -> None:
    FIG_DIR.mkdir(exist_ok=True)
    fig.savefig(FIG_DIR / name)
    plt.close(fig)
    print(f"  wrote figures/{name}")


def fig_case_study(aligned: pd.DataFrame, films: pd.DataFrame) -> dict:
    film = films.loc[CASE_STUDY_PATH]
    run = aligned[aligned["movie_path"] == CASE_STUDY_PATH].sort_values("week").reset_index(drop=True)
    run["change"] = np.where(run["week"].diff() == 1, run["gross"] / run["gross"].shift(1) - 1, np.nan)
    run["share_of_total"] = run["total_gross"] / film["final_gross"]
    is_holiday = run["holiday"].map(lambda h: isinstance(h, str))

    fig, (ax, theaters_ax) = plt.subplots(
        2, 1, figsize=(12, 8.5), sharex=True, gridspec_kw={"height_ratios": [3, 1.15], "hspace": 0.08}
    )
    ax.bar(run["week"], run["gross"] / 1e6, width=0.68,
           color=[ORANGE if holiday else BLUE for holiday in is_holiday], alpha=0.9)
    top = run["gross"].max() / 1e6
    for row in run.itertuples():
        label = _money(row.gross)
        if not np.isnan(row.change):
            label += f"\n{row.change:+.0%}"
        ax.text(row.week, row.gross / 1e6 + top * 0.015, label, ha="center", va="bottom", fontsize=9.5)
    ax.set_ylim(0, top * 1.18)
    ax.set_ylabel("Weekly domestic gross ($ millions)")
    ax.grid(axis="x", visible=False)
    ax.legend(
        handles=[Patch(color=BLUE, label="Weekly gross (label: change from the week before)"),
                 Patch(color=ORANGE, label="Week containing a public holiday")],
        loc="upper right",
    )

    def share_through(week: int) -> float:
        return float(run.loc[run["week"] <= week, "gross"].sum() / film["final_gross"])

    note = (
        f"Domestic total: ${film['final_gross'] / 1e6:.1f}M over {len(run)} reported weeks\n"
        f"Week 1 = {share_through(1):.0%} of the total, weeks 1-2 = {share_through(2):.0%}, "
        f"weeks 1-4 = {share_through(4):.0%}\n"
        f"Total = {film['multiple']:.2f}x the opening week"
    )
    ax.text(0.98, 0.70, note, transform=ax.transAxes, ha="right", va="top", fontsize=10,
            bbox={"boxstyle": "round,pad=0.5", "facecolor": "white", "edgecolor": "#cbd5e0"})
    ax.set_title(f"Case study: {film['display_title']}, week by week")

    theaters_ax.plot(run["week"], run["theaters"], color=GREY, marker="o", lw=2)
    theaters_ax.fill_between(run["week"], run["theaters"], color=GREY, alpha=0.12)
    for row in run.itertuples():
        theaters_ax.annotate(f"{row.theaters:,}", (row.week, row.theaters), textcoords="offset points",
                             xytext=(0, 7), ha="center", fontsize=8.5)
    theaters_ax.set_ylim(0, run["theaters"].max() * 1.3)
    theaters_ax.set_ylabel("Theaters")
    theaters_ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    theaters_ax.set_xticks(run["week"])
    short_names = {"Fourth of July": "July 4th", "Christmas-New Year's": "Holidays", "Thanksgiving": "Thanksgiv."}
    theaters_ax.set_xticklabels(
        [f"{w}\n{d:%b} {d.day}" + (f"\n{short_names.get(h, h)}" if isinstance(h, str) else "")
         for w, d, h in zip(run["week"], run["week_date"], run["holiday"])]
    )
    theaters_ax.set_xlabel("Week of release and the Friday it starts")
    _save(fig, "01_case_study.png")

    return {
        "title": film["display_title"],
        "distributor": film["distributor"],
        "genre": film["genre"],
        "budget": film["budget"],
        "final_gross": film["final_gross"],
        "multiple": round(float(film["multiple"]), 3),
        "share_week1": round(share_through(1), 4),
        "share_weeks_1_2": round(share_through(2), 4),
        "share_weeks_1_4": round(share_through(4), 4),
        "weeks": [
            {
                "week": int(row.week),
                "week_start": row.week_date.date().isoformat(),
                "gross": row.gross,
                "change": None if np.isnan(row.change) else round(row.change, 4),
                "theaters": int(row.theaters),
                "per_theater": round(row.per_theater, 1),
                "share_of_total_to_date": round(row.share_of_total, 4),
                "holiday": row.holiday if isinstance(row.holiday, str) else None,
            }
            for row in run.itertuples()
        ],
    }


def pick_contrast(films: pd.DataFrame, rel: pd.DataFrame) -> tuple[str, str, pd.DataFrame]:
    """Steepest and gentlest week-2 change among $40M+ Friday openers with finished
    runs whose second week has no holiday (a holiday flatters the hold)."""
    pool = films[
        (films["week1_gross"] >= CONTRAST_MIN_WEEK1)
        & ~films["incomplete_run"]
        & ~films["partial_opening"]
        & films["holiday_in_week2"].isna()
    ].copy()
    pool["week2_retention"] = rel.loc[pool.index, 2]
    pool = pool.dropna(subset=["week2_retention"]).sort_values("week2_retention")
    return pool.index[0], pool.index[-1], pool


def fig_contrast(aligned: pd.DataFrame, films: pd.DataFrame, rel: pd.DataFrame, overall: pd.DataFrame) -> dict:
    front_path, leggy_path, pool = pick_contrast(films, rel)
    fig, ax = plt.subplots(figsize=(11.5, 6.5))
    _plot_median(ax, overall, GREY, f"Median film still in release (all {int(overall.loc[1, 'n'])} films)",
                 lw=2, marker="", dashed_tail=True)
    summaries = {}
    for path, color, role in ((front_path, RED, "front_loaded"), (leggy_path, GREEN, "long_legged")):
        film = films.loc[path]
        curve = rel.loc[path]
        run = aligned[aligned["movie_path"] == path].set_index("week")
        week2 = curve[2] - 1
        ax.plot(curve.index, curve.where(curve > 0), color=color, marker="o", lw=2.5,
                label=(f"{film['display_title']}: week 1 ${film['week1_gross'] / 1e6:.0f}M, "
                       f"week 2 {week2:+.0%}, total {film['multiple']:.1f}x week 1"))
        summaries[role] = {
            "title": film["display_title"],
            "genre": film["genre_group"],
            "distributor": film["distributor"],
            "week1_gross": film["week1_gross"],
            "week1_theaters": int(film["week1_theaters"]),
            "week2_change": round(float(week2), 4),
            "final_gross": film["final_gross"],
            "multiple": round(float(film["multiple"]), 3),
            "weeks_reported": int(film["last_week"]),
            "retention": {str(w): round(float(curve[w]), 4) for w in (2, 3, 4, 6, 8, 10, 12) if not np.isnan(curve[w])},
            "theaters": {str(w): int(run.loc[w, "theaters"]) for w in (1, 2, 3, 4, 6, 8, 10, 12) if w in run.index},
        }
    _log_percent_axis(ax, low=1e-3, high=1.5)
    _week_axis(ax)
    ax.set_ylabel("Weekly gross as a share of week 1 (log scale)")
    ax.set_title("Front-loaded versus long-legged: two $40M+ openings")
    ax.legend(loc="lower left", fontsize=10)
    _save(fig, "02_case_study_contrast.png")
    summaries["pool_size"] = int(len(pool))
    summaries["pool_rule"] = (
        "week-1 gross of $40M or more, Friday opening, finished run, no holiday in week 2"
    )
    return summaries


def fig_all_curves(aligned: pd.DataFrame, films: pd.DataFrame, overall: pd.DataFrame) -> None:
    fig, (ax, share_ax) = plt.subplots(
        2, 1, figsize=(12, 9), sharex=True, gridspec_kw={"height_ratios": [3.2, 1], "hspace": 0.08}
    )
    week1 = films["week1_gross"]
    segments = []
    for path, run in aligned[aligned["week"] <= MAX_WEEK].groupby("movie_path"):
        run = run.sort_values("week")
        values = (run["gross"] / week1[path]).clip(lower=LOG_FLOOR)
        segments.append(np.column_stack([run["week"], values]))
    ax.add_collection(LineCollection(segments, colors=BLUE, linewidths=0.7, alpha=0.08))

    reliable = reliable_weeks(overall)
    ax.fill_between(overall.index, overall["q25"].where(reliable), overall["q75"].where(reliable),
                    color=ORANGE, alpha=0.25, label="Middle half of films still in release")
    _plot_median(ax, overall, ORANGE, "Median film still in release", lw=3, ms=6, dashed_tail=True)
    for week in (2, 4, 6, 8, 10):
        value = overall.loc[week, "median"]
        if reliable[week]:
            ax.annotate(f"{value:.1%}", (week, value), textcoords="offset points", xytext=(6, 9),
                        fontsize=10, fontweight="bold", color="#9c4221")
    _log_percent_axis(ax, high=3.0)
    ax.set_ylabel("Weekly gross as a share of week 1 (log scale)")
    years = films["open_chart_date"].dt.year
    ax.set_title(f"How {len(films)} wide releases decayed after opening ({years.min()}-{years.max()})")
    ax.plot([], [], color=BLUE, alpha=0.5, lw=1, label="One line per film")
    ax.plot([], [], color=ORANGE, lw=1.8, ls="--", label="Median once fewer than half are still in release")
    ax.legend(loc="upper right")

    share = overall["share_reporting"]
    share_ax.bar(share.index, share, color=[GREY if r else "#cbd5e0" for r in reliable], width=0.7)
    for week, value in share.items():
        share_ax.text(week, value + 0.03, f"{value:.0%}", ha="center", fontsize=8.5)
    share_ax.set_ylim(0, 1.2)
    share_ax.yaxis.set_major_formatter(PERCENT)
    share_ax.set_ylabel("Films still\nin release")
    _week_axis(share_ax)
    _save(fig, "03_all_decay_curves.png")


def fig_group_curves(summaries: dict[str, pd.DataFrame], sizes: dict[str, int], title: str, name: str,
                     overall: pd.DataFrame) -> None:
    fig, ax = plt.subplots(figsize=(11.5, 6.5))
    _plot_median(ax, overall, GREY, "All films", lw=1.8, marker="", dashed_tail=False)
    ax.lines[-1].set_linestyle("--")
    for color, (group, summary) in zip(GROUP_COLORS, summaries.items()):
        reliable = reliable_weeks(summary)
        if not reliable.any():
            print(f"  skipped {group}: fewer than {MIN_FILMS_REPORTING} films")
            continue
        last_week = int(reliable[reliable].index.max())
        _plot_median(ax, summary, color,
                     f"{group} (n={sizes[group]}): week 2 = {summary.loc[2, 'median']:.0%}, "
                     f"most still playing to week {last_week}")
    _log_percent_axis(ax, low=1e-3, high=1.5)
    _week_axis(ax)
    ax.set_ylabel("Median weekly gross as a share of week 1 (log scale)")
    ax.set_title(title)
    ax.legend(loc="upper left", bbox_to_anchor=(0.0, -0.13), ncol=2, fontsize=9.5)
    ax.text(0.99, 0.98, "Medians over films still in release; a line stops once fewer than half remain",
            transform=ax.transAxes, ha="right", va="top", fontsize=9, color=GREY)
    _save(fig, name)


def fig_week2_hist(week2_change: pd.Series, holiday_in_week2: pd.Series) -> None:
    fig, ax = plt.subplots(figsize=(11, 6))
    clipped = week2_change.clip(-1.0, 1.0) * 100
    holiday = holiday_in_week2.reindex(clipped.index).notna()
    bins = np.arange(-100, 105, 5)
    ax.hist([clipped[~holiday], clipped[holiday]], bins=bins, stacked=True, color=[BLUE, ORANGE],
            alpha=0.88, edgecolor="white",
            label=[f"Week 2 without a holiday ({(~holiday).sum()} films)",
                   f"Week 2 containing a holiday ({holiday.sum()} films)"])
    median = week2_change.median() * 100
    q25, q75 = week2_change.quantile([0.25, 0.75]) * 100
    ax.axvspan(q25, q75, color=GREY, alpha=0.12, label=f"Middle half: {q25:.0f}% to {q75:.0f}%")
    ax.axvline(median, color="#1a202c", lw=2.5, label=f"Median: {median:.0f}%")
    ax.axvline(0, color=GREY, lw=1, ls=":")
    ax.set_xlim(-102, 102)
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:+.0f}%" if v else "0%"))
    ax.set_xlabel("Change in gross from week 1 to week 2 (values above +100% are shown at +100%)")
    ax.set_ylabel("Number of films")
    ax.set_title(f"The week-2 drop across {len(week2_change)} wide releases")
    ax.legend(loc="upper right")
    _save(fig, "06_week2_drop_hist.png")


def fig_gross_vs_theaters(decomposition: pd.DataFrame, cohort_size: int) -> None:
    fig, ax = plt.subplots(figsize=(11, 6.5))
    lines = (
        ("gross", "Weekly gross", BLUE),
        ("theaters", "Theater count", ORANGE),
        ("per_theater", "Gross per theater", GREEN),
    )
    last = decomposition.index[-1]
    for column, label, color in lines:
        ax.plot(decomposition.index, decomposition[column], color=color, lw=2.8, marker="o", label=label)
        ax.annotate(f"{decomposition.loc[last, column]:.0%}", (last, decomposition.loc[last, column]),
                    textcoords="offset points", xytext=(10, -4), color=color, fontweight="bold")
    _log_percent_axis(ax, low=0.005, high=1.3)
    ax.set_xlim(0.6, DECOMPOSITION_WEEKS + 0.8)
    ax.set_xticks(range(1, DECOMPOSITION_WEEKS + 1))
    ax.set_xlabel("Week of release")
    ax.set_ylabel("Median as a share of week 1 (log scale)")
    ax.set_title("Where the decline comes from: fewer theaters or fewer people per theater")
    ax.legend(loc="lower left")
    ax.text(0.99, 0.98, f"The same {cohort_size} films every week: finished runs still in release in week {DECOMPOSITION_WEEKS}",
            transform=ax.transAxes, ha="right", va="top", fontsize=9, color=GREY)
    _save(fig, "07_gross_vs_theaters.png")


def fig_cumulative(cum: pd.DataFrame) -> pd.DataFrame:
    summary = quantiles(cum)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.fill_between(summary.index, summary["q25"], summary["q75"], color=BLUE, alpha=0.18,
                    label="Middle half of films")
    ax.plot(summary.index, summary["median"], color=BLUE, lw=3, marker="o", label="Median film")
    offsets = {1: (14, -4), 2: (14, -10), 4: (6, -22), 8: (0, -24)}
    for week, offset in offsets.items():
        value = summary.loc[week, "median"]
        ax.annotate(f"Week {week}: {value:.0%}", (week, value), textcoords="offset points", xytext=offset,
                    fontsize=10.5, fontweight="bold", color="#1a365d", ha="left" if week < 8 else "center")
    ax.set_ylim(0, 1.04)
    ax.yaxis.set_major_formatter(PERCENT)
    _week_axis(ax)
    ax.set_ylabel("Share of final domestic total earned so far")
    ax.set_title(f"How quickly the money comes in ({len(cum)} finished runs)")
    ax.legend(loc="lower right")
    _save(fig, "08_cumulative_share.png")
    return summary


# ---------------------------------------------------------------- stats


def _records(frame: pd.DataFrame, digits: int = 4) -> dict:
    return {
        str(index): {column: (None if pd.isna(value) else round(float(value), digits)) for column, value in row.items()}
        for index, row in frame.iterrows()
    }


def group_decay(rel: pd.DataFrame, films: pd.DataFrame, column: str, groups: list[str]) -> dict[str, pd.DataFrame]:
    return {group: decay_summary(rel.loc[films.index[films[column] == group]]) for group in groups}


def group_stats(films: pd.DataFrame, summaries: dict[str, pd.DataFrame], column: str) -> dict:
    out = {}
    for group, summary in summaries.items():
        members = films[films[column] == group]
        finished = members[~members["incomplete_run"]]
        reliable = reliable_weeks(summary)
        out[group] = {
            "films": int(len(members)),
            "median_week1_gross": float(members["week1_gross"].median()),
            "median_week1_theaters": float(members["week1_theaters"].median()),
            "median_retention": {str(w): float(summary.loc[w, "median"]) for w in (2, 3, 4, 6, 8, 10)},
            "share_still_in_release": {str(w): float(summary.loc[w, "share_reporting"]) for w in (4, 6, 8, 10, 12)},
            "last_week_with_half_still_in_release": int(reliable[reliable].index.max()) if reliable.any() else None,
            "finished_runs": int(len(finished)),
            "median_multiple_finished": float(finished["multiple"].median()),
            "median_weeks_reported_finished": float(finished["last_week"].median()),
        }
    return out


def wow_by_week(wow: pd.DataFrame) -> dict:
    summary = (
        wow[(wow["week"] >= 2) & (wow["week"] <= MAX_WEEK)]
        .groupby("week")["wow_change"]
        .agg(n="count", median="median", q25=lambda s: s.quantile(0.25), q75=lambda s: s.quantile(0.75))
    )
    return _records(summary)


def holiday_stats(wow: pd.DataFrame) -> dict:
    later = wow[(wow["week"] >= 2) & wow["wow_change"].notna()]
    plain = later[later["holiday"].isna()]
    out = {"no_holiday": {"film_weeks": int(len(plain)), "median_change": float(plain["wow_change"].median())}}
    for name, group in later.dropna(subset=["holiday"]).groupby("holiday"):
        out[name] = {"film_weeks": int(len(group)), "median_change": float(group["wow_change"].median())}
    after = later[later.groupby("movie_path")["holiday"].shift(1).notna()]
    after = after[after["holiday"].isna()]
    out["week_after_a_holiday"] = {"film_weeks": int(len(after)), "median_change": float(after["wow_change"].median())}
    return out


def _spearman(a: pd.Series, b: pd.Series) -> float:
    return float(a.rank().corr(b.rank()))


def week2_hold_stats(rel: pd.DataFrame, wow: pd.DataFrame, finished: pd.DataFrame) -> dict:
    """How the week-2 hold lines up with the weekly decline in weeks 3-6."""
    later = wow[(wow["week"] >= 3) & (wow["week"] <= 6)].groupby("movie_path")["wow_change"].median()
    hold = pd.DataFrame({"week2_retention": rel[2], "median_change_weeks_3_6": later}).dropna()
    hold["quartile"] = pd.qcut(hold["week2_retention"], 4, labels=["weakest", "second", "third", "strongest"])
    by_quartile = hold.groupby("quartile", observed=True).agg(
        films=("week2_retention", "size"),
        median_week2_retention=("week2_retention", "median"),
        median_change_weeks_3_6=("median_change_weeks_3_6", "median"),
    )
    legs = pd.DataFrame({"week2_retention": rel.loc[finished.index, 2], "multiple": finished["multiple"]}).dropna()
    return {
        "films": int(len(hold)),
        "spearman_week2_vs_weeks_3_6_change": _spearman(hold["week2_retention"], hold["median_change_weeks_3_6"]),
        "by_week2_quartile": _records(by_quartile),
        "spearman_week2_vs_multiple_finished": _spearman(legs["week2_retention"], legs["multiple"]),
    }


def main() -> None:
    movies, weekly, weekend, meta = load()
    latest = pd.Timestamp(meta["latest_chart_date"])
    print("Checking data")
    checks = check_data(movies, weekly)

    reasons = exclusion_reasons(movies)
    checks["excluded"] = {
        reason: movies.loc[reasons == reason, "title"].tolist() for reason in sorted(set(reasons) - {""})
    }
    movies = movies[reasons == ""].copy()
    movies["partial_opening"] = movies["open_days_in_release"] < 7
    weekly = weekly[weekly["movie_path"].isin(movies["movie_path"])]
    weekend = weekend[weekend["movie_path"].isin(movies["movie_path"])]
    weekly, estimated_weeks = extend_with_weekends(weekly, weekend)
    checks["weeks_estimated_from_weekend"] = estimated_weeks
    checks["films_with_estimated_week"] = int(weekly.loc[weekly["estimated"], "movie_path"].nunique())
    aligned, rereleased = initial_runs(align_weeks(movies, weekly))
    checks["rereleases_cut_from_runs"] = movies.set_index("movie_path").loc[rereleased, "title"].tolist()
    films = build_films(movies, aligned, latest, rereleased)
    unusable = films["week1_gross"].isna() | (films["weeks_observable"] < 1)
    checks["dropped_week1_not_yet_complete"] = films.loc[unusable, "display_title"].tolist()
    films = films[~unusable]
    aligned = aligned[aligned["movie_path"].isin(films.index)]

    gross = week_matrix(aligned, films, "gross")
    rel = relative_to_week1(gross)
    overall = decay_summary(rel)
    wow = week_over_week(aligned)
    finished = films[~films["incomplete_run"]]

    print("Drawing figures")
    case_study = fig_case_study(aligned, films)
    contrast = fig_contrast(aligned, films, rel, overall)
    fig_all_curves(aligned, films, overall)

    buckets = [name for name, _, _ in OPENING_BUCKETS]
    bucket_summaries = group_decay(rel, films, "opening_bucket", buckets)
    bucket_sizes = {b: int((films["opening_bucket"] == b).sum()) for b in buckets}
    fig_group_curves(bucket_summaries, bucket_sizes, "Decay by size of opening week",
                     "04_decay_by_opening_size.png", overall)

    genres = films["genre_group"].value_counts().head(N_GENRES).index.tolist()
    genre_summaries = group_decay(rel, films, "genre_group", genres)
    genre_sizes = {g: int((films["genre_group"] == g).sum()) for g in genres}
    fig_group_curves(genre_summaries, genre_sizes, f"Decay by genre ({N_GENRES} most common)",
                     "05_decay_by_genre.png", overall)

    week2_change = (rel[2] - 1).dropna()
    fig_week2_hist(week2_change, films["holiday_in_week2"])

    cohort = finished.index[finished["last_week"] >= DECOMPOSITION_WEEKS]
    decomposition = pd.DataFrame(
        {
            column: relative_to_week1(week_matrix(aligned, films.loc[cohort], column, DECOMPOSITION_WEEKS)).median()
            for column in ("gross", "theaters", "per_theater")
        }
    )
    fig_gross_vs_theaters(decomposition, len(cohort))

    cum = cumulative_share(aligned, finished)
    cum_summary = fig_cumulative(cum)
    weeks_to = {}
    for level in (0.5, 0.75, 0.9):
        reached = cum.ge(level)
        first_week = reached.idxmax(axis=1).where(reached.any(axis=1))
        weeks_to[f"median_weeks_to_{int(level * 100)}pct"] = float(first_week.median())
        weeks_to[f"share_not_reaching_{int(level * 100)}pct_by_week_{MAX_WEEK}"] = float(first_week.isna().mean())
    weeks_to["share_earning_half_in_week1"] = float((cum[1] >= 0.5).mean())
    weeks_to["share_at_90pct_by_week4"] = float((cum[4] >= 0.9).mean())

    holiday_week2 = films.loc[week2_change.index, "holiday_in_week2"].notna()
    stats = {
        "scrape": meta,
        "data_checks": checks,
        "sample": {
            "films": int(len(films)),
            "films_by_opening_year": {
                str(k): int(v) for k, v in films["open_chart_date"].dt.year.value_counts().sort_index().items()
            },
            "partial_week_openers": int(films["partial_opening"].sum()),
            "partial_week_opening_days": {
                str(k): int(v)
                for k, v in films.loc[films["partial_opening"], "open_days_in_release"].value_counts().sort_index().items()
            },
            "finished_runs": int(len(finished)),
            "incomplete_runs": int(films["incomplete_run"].sum()),
            "incomplete_run_cutoff": (latest - pd.Timedelta(weeks=INCOMPLETE_RUN_WEEKS)).date().isoformat(),
            "median_week1_gross": float(films["week1_gross"].median()),
            "median_week1_theaters": float(films["week1_theaters"].median()),
            "median_final_gross_finished": float(finished["final_gross"].median()),
            "median_multiple_finished": float(finished["multiple"].median()),
            "multiple_q25_q75_finished": [float(v) for v in finished["multiple"].quantile([0.25, 0.75])],
            "share_multiple_below_1_5_finished": float((finished["multiple"] < 1.5).mean()),
            "films_multiple_3_or_more_finished": int((finished["multiple"] >= 3).sum()),
            "median_weeks_reported_finished": float(finished["last_week"].median()),
            "weeks_reported_q25_q75_finished": [float(v) for v in finished["last_week"].quantile([0.25, 0.75])],
            "genre_groups": {str(k): int(v) for k, v in films["genre_group"].value_counts().items()},
            "opening_buckets": {str(k): int(v) for k, v in films["opening_bucket"].value_counts().sort_index().items()},
            "biggest_week1": films.nlargest(5, "week1_gross")[["display_title", "week1_gross"]].to_dict("records"),
            "longest_runs_finished": finished.nlargest(5, "last_week")[["display_title", "last_week"]].to_dict("records"),
        },
        "decay_relative_to_week1": _records(overall),
        "week_over_week_change": wow_by_week(wow),
        "week2_change": {
            "films": int(len(week2_change)),
            "median": float(week2_change.median()),
            "mean": float(week2_change.mean()),
            "q25": float(week2_change.quantile(0.25)),
            "q75": float(week2_change.quantile(0.75)),
            "median_without_holiday": float(week2_change[~holiday_week2].median()),
            "median_with_holiday": float(week2_change[holiday_week2].median()),
            "films_with_holiday_in_week2": int(holiday_week2.sum()),
            "share_dropping_more_than_50pct": float((week2_change < -0.5).mean()),
            "share_dropping_more_than_60pct": float((week2_change < -0.6).mean()),
            "share_dropping_more_than_70pct": float((week2_change < -0.7).mean()),
            "share_dropping_less_than_30pct": float((week2_change > -0.3).mean()),
            "share_increasing": float((week2_change > 0).mean()),
            "films_with_no_week2": int((week2_change == -1).sum()),
            "steepest": [
                {"title": films.loc[p, "display_title"], "change": round(float(v), 4)}
                for p, v in week2_change.nsmallest(8).items()
            ],
            "gentlest": [
                {"title": films.loc[p, "display_title"], "change": round(float(v), 4),
                 "holiday_in_week2": films.loc[p, "holiday_in_week2"]}
                for p, v in week2_change.nlargest(8).items()
            ],
        },
        "week2_hold_vs_later_decay": week2_hold_stats(rel, wow, finished),
        "by_opening_size": group_stats(films, bucket_summaries, "opening_bucket"),
        "by_genre": group_stats(films, genre_summaries, "genre_group"),
        "holiday_weeks": holiday_stats(wow),
        "gross_vs_theaters": {"cohort_films": int(len(cohort)), "weeks": _records(decomposition)},
        "cumulative_share_finished": {"weeks": _records(cum_summary), **weeks_to},
        "case_study": case_study,
        "contrast": contrast,
    }
    (DATA_DIR / "summary_stats.json").write_text(json.dumps(stats, indent=2, default=str))
    print(f"Wrote data/summary_stats.json ({len(films)} films, {len(finished)} finished runs)")


if __name__ == "__main__":
    main()

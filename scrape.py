"""Collect weekly domestic box office runs for wide releases from the-numbers.com.

Stage 1 ("charts") reads every weekly chart from START_DATE through the latest
published week and keeps films that opened wide. Stage 2 ("movies") visits each
of those films' pages once for the week-by-week and weekend-by-weekend tables
and the film's details.

    python scrape.py                  # both stages
    python scrape.py --stage charts   # only weekly charts -> charts.csv, openings.csv
    python scrape.py --stage movies   # only movie pages -> movies.csv, weekly.csv, weekend.csv
    python scrape.py --limit 5        # try stage 2 on the first five films
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from tn_client import NotFound, clean_text, fetch, parse_int, parse_money, parse_percent

DATA_DIR = Path(__file__).resolve().parent / "data"
START_DATE = dt.date(2023, 1, 6)
MIN_OPENING_THEATERS = 1000
MAX_OPENING_DAYS = 7
# Charts and movie pages for the last few weeks still get revised.
RECENT_WEEKS = 4
RECENT_MAX_AGE_S = 24 * 3600

CHART_LANDING_PATH = "/weekly-box-office-chart"
CHART_DATE_PATH = re.compile(r"/box-office-chart/(?:weekly|weekend)/(\d{4})/(\d{2})/(\d{2})")


def chart_path(chart_date: dt.date) -> str:
    return f"/box-office-chart/weekly/{chart_date:%Y/%m/%d}"


def latest_chart_date() -> dt.date:
    html = fetch(CHART_LANDING_PATH, max_age_s=6 * 3600)
    heading = clean_text(BeautifulSoup(html, "lxml").find("h1").get_text())
    date_text = heading.replace("Week Domestic Box Office", "").strip()
    return dt.datetime.strptime(date_text, "%B %d, %Y").date()


def parse_chart(html: str, chart_date: dt.date) -> list[dict]:
    table = BeautifulSoup(html, "lxml").select_one("table.chart-desktop")
    if table is None:
        return []
    rows = []
    for tr in table.select("tbody tr"):
        cells = tr.find_all("td")
        if len(cells) < 9:
            continue
        link = cells[2].find("a")
        rows.append(
            {
                "chart_date": chart_date.isoformat(),
                "rank": parse_int(cells[0].get_text()),
                "prev": clean_text(cells[1].get_text()),
                "title": clean_text(cells[2].get_text()),
                "movie_path": link["href"] if link and link.has_attr("href") else None,
                "gross": parse_money(cells[3].get_text()),
                "weekly_change": parse_percent(cells[4].get_text()),
                "theaters": parse_int(cells[5].get_text()),
                "theater_avg": parse_money(cells[6].get_text()),
                "total_gross": parse_money(cells[7].get_text()),
                "days_in_release": parse_int(cells[8].get_text()),
            }
        )
    return rows


def scrape_charts() -> None:
    latest = latest_chart_date()
    recent_cutoff = latest - dt.timedelta(weeks=RECENT_WEEKS)
    chart_dates = []
    day = START_DATE
    while day <= latest:
        chart_dates.append(day)
        day += dt.timedelta(weeks=1)
    print(f"Stage 1: {len(chart_dates)} weekly charts, {START_DATE} to {latest}")

    all_rows = []
    for i, chart_date in enumerate(chart_dates, start=1):
        max_age = RECENT_MAX_AGE_S if chart_date >= recent_cutoff else None
        try:
            html = fetch(chart_path(chart_date), max_age_s=max_age)
        except NotFound:
            print(f"  no chart for {chart_date}")
            continue
        rows = parse_chart(html, chart_date)
        if not rows:
            print(f"  chart for {chart_date} has no rows")
        all_rows.extend(rows)
        if i % 25 == 0 or i == len(chart_dates):
            print(f"  {i}/{len(chart_dates)} charts read ({len(all_rows)} rows so far)")

    charts = pd.DataFrame(all_rows)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    charts.to_csv(DATA_DIR / "charts.csv", index=False)

    linked = charts.dropna(subset=["movie_path"])
    first_seen = linked.sort_values("chart_date").drop_duplicates("movie_path", keep="first")
    is_wide_opening = (
        (first_seen["prev"] == "(new)")
        & (first_seen["theaters"] >= MIN_OPENING_THEATERS)
        & (first_seen["days_in_release"] <= MAX_OPENING_DAYS)
    )
    openings = first_seen[is_wide_opening].rename(
        columns={
            "chart_date": "open_chart_date",
            "rank": "open_rank",
            "theaters": "open_theaters",
            "gross": "open_week_gross",
            "days_in_release": "open_days_in_release",
        }
    )[
        [
            "movie_path",
            "title",
            "open_chart_date",
            "open_rank",
            "open_theaters",
            "open_week_gross",
            "open_days_in_release",
        ]
    ]
    openings.to_csv(DATA_DIR / "openings.csv", index=False)

    new_entries = first_seen[first_seen["prev"] == "(new)"]
    meta = {
        "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
        "first_chart_date": START_DATE.isoformat(),
        "latest_chart_date": latest.isoformat(),
        "charts_read": int(charts["chart_date"].nunique()),
        "chart_rows": len(charts),
        "distinct_titles_on_charts": int(linked["movie_path"].nunique()),
        "new_entries": len(new_entries),
        "excluded_under_min_theaters": int((new_entries["theaters"] < MIN_OPENING_THEATERS).sum()),
        "excluded_over_max_days": int(
            (
                (new_entries["theaters"] >= MIN_OPENING_THEATERS)
                & (new_entries["days_in_release"] > MAX_OPENING_DAYS)
            ).sum()
        ),
        "wide_openings": len(openings),
        "min_opening_theaters": MIN_OPENING_THEATERS,
        "max_opening_days": MAX_OPENING_DAYS,
    }
    (DATA_DIR / "scrape_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"Stage 1 done: {meta['wide_openings']} wide openings out of {meta['new_entries']} new entries")


def _strip_tags(fragment: str) -> str:
    return clean_text(re.sub(r"<[^>]+>", " ", fragment))


def _detail(html: str, *labels: str) -> str | None:
    """Value cell that follows a bold label cell, e.g. <b>Genre:</b></td><td>Action."""
    for label in labels:
        pattern = (
            r"<b>\s*" + re.escape(label) + r"\s*</b>\s*</td>\s*<td[^>]*>(.*?)(?=</td>|<tr|</table>)"
        )
        match = re.search(pattern, html, re.S | re.I)
        if match:
            return match.group(1)
    return None


def _first_float(text: str | None) -> float | None:
    match = re.search(r"\d+(?:\.\d+)?", clean_text(text))
    return float(match.group(0)) if match else None


def parse_performance_table(soup: BeautifulSoup, heading_text: str, movie_path: str) -> list[dict]:
    """Rows of the 'Weekly' or 'Weekend Box Office Performance' table on a movie page."""
    heading = soup.find(lambda tag: tag.name == "h2" and clean_text(tag.get_text()) == heading_text)
    table = heading.find_next("table") if heading else None
    rows = []
    for tr in table.find_all("tr") if table else []:
        cells = tr.find_all("td")
        if len(cells) < 8:
            continue
        link = cells[0].find("a")
        date_match = CHART_DATE_PATH.search(link["href"]) if link and link.has_attr("href") else None
        if date_match:
            week_date = dt.date(*map(int, date_match.groups()))
        else:
            week_date = dt.datetime.strptime(clean_text(cells[0].get_text()), "%b %d, %Y").date()
        rows.append(
            {
                "movie_path": movie_path,
                "week": parse_int(cells[7].get_text()),
                "week_date": week_date.isoformat(),
                "rank": parse_int(cells[1].get_text()),
                "gross": parse_money(cells[2].get_text()),
                "pct_change": parse_percent(cells[3].get_text()),
                "theaters": parse_int(cells[4].get_text()),
                "per_theater": parse_money(cells[5].get_text()),
                "total_gross": parse_money(cells[6].get_text()),
            }
        )
    return rows


def parse_movie(html: str, movie_path: str) -> tuple[dict, list[dict], list[dict]]:
    normalized = html.replace("&nbsp;", " ").replace("\xa0", " ")
    soup = BeautifulSoup(html, "lxml")
    heading = soup.find("h1")

    release_html = _detail(normalized, "Domestic Releases:") or ""
    release_lines = [
        _strip_tags(line) for line in re.split(r"<br\s*/?>", release_html, flags=re.I) if _strip_tags(line)
    ]
    releases = [
        match
        for match in (
            re.match(r"(?P<date>[A-Za-z]+ \d+\w*, \d{4})\s*(?:\((?P<kind>[^)]*)\))?\s*(?:by\s+(?P<dist>.+))?", line)
            for line in release_lines
        )
        if match
    ]
    # The wide release, not an earlier IMAX/fan preview, limited or canceled date.
    release = next((r for r in releases if r.group("kind") == "Wide"), releases[0] if releases else None)
    rating_text = _strip_tags(_detail(normalized, "MPA Rating:", "MPAA Rating:") or "")
    rating = re.match(r"(NC-17|PG-13|PG|G|R|Not Rated|Unrated)\b", rating_text)
    theater_counts = _strip_tags(_detail(normalized, "Theater counts:") or "")
    counts = re.match(
        r"([\d,]+) opening theaters/([\d,]+) max\. theaters, ([\d.]+) weeks", theater_counts
    )

    def text(*labels: str) -> str | None:
        value = _detail(normalized, *labels)
        return _strip_tags(value) if value is not None else None

    movie = {
        "movie_path": movie_path,
        "page_title": clean_text(heading.get_text()) if heading else None,
        "release_date_text": release.group("date") if release else None,
        "release_type": release.group("kind") if release else None,
        "release_lines": " | ".join(release_lines),
        "distributor": (
            re.split(r",\s*released as\b", clean_text(release.group("dist")))[0]
            if release and release.group("dist")
            else None
        ),
        "genre": text("Genre:"),
        "subgenre": text("Subgenre:"),
        "source": text("Source:"),
        "creative_type": text("Creative Type:"),
        "production_method": text("Production Method:"),
        "mpa_rating": rating.group(1) if rating else None,
        "runtime_min": parse_int(text("Running Time:")),
        "budget": parse_money(text("Production Budget:")),
        "opening_weekend": parse_money(text("Opening Weekend:")),
        "legs": _first_float(text("Legs:")),
        "domestic_total": parse_money(text("Domestic Box Office")),
        "international_total": parse_money(text("International Box Office")),
        "worldwide_total": parse_money(text("Worldwide Box Office")),
        "page_opening_theaters": parse_int(counts.group(1)) if counts else None,
        "max_theaters": parse_int(counts.group(2)) if counts else None,
        "avg_weeks_per_theater": float(counts.group(3)) if counts else None,
    }

    weeks = parse_performance_table(soup, "Weekly Box Office Performance", movie_path)
    weekends = parse_performance_table(soup, "Weekend Box Office Performance", movie_path)
    movie["n_weeks"] = len(weeks)
    movie["n_weekends"] = len(weekends)
    return movie, weeks, weekends


def scrape_movies(limit: int | None = None) -> None:
    openings = pd.read_csv(DATA_DIR / "openings.csv")
    meta = json.loads((DATA_DIR / "scrape_meta.json").read_text())
    latest = dt.date.fromisoformat(meta["latest_chart_date"])
    if limit:
        openings = openings.head(limit)
    print(f"Stage 2: {len(openings)} movie pages")

    movies, weekly, weekend, missing = [], [], [], []
    for i, opening in enumerate(openings.itertuples(index=False), start=1):
        opened = dt.date.fromisoformat(opening.open_chart_date)
        still_changing = opened >= latest - dt.timedelta(weeks=26)
        try:
            html = fetch(opening.movie_path, max_age_s=RECENT_MAX_AGE_S if still_changing else None)
        except NotFound:
            missing.append(opening.movie_path)
            print(f"  movie page missing: {opening.movie_path}")
            continue
        movie, weeks, weekends = parse_movie(html, opening.movie_path)
        if not weeks:
            print(f"  no weekly table: {opening.movie_path}")
        movies.append({**opening._asdict(), **movie})
        weekly.extend(weeks)
        weekend.extend(weekends)
        if i % 25 == 0 or i == len(openings):
            print(f"  {i}/{len(openings)} movie pages read")

    pd.DataFrame(movies).to_csv(DATA_DIR / "movies.csv", index=False)
    pd.DataFrame(weekly).to_csv(DATA_DIR / "weekly.csv", index=False)
    pd.DataFrame(weekend).to_csv(DATA_DIR / "weekend.csv", index=False)
    meta.update({"movie_pages_read": len(movies), "movie_pages_missing": missing})
    (DATA_DIR / "scrape_meta.json").write_text(json.dumps(meta, indent=2))
    print(f"Stage 2 done: {len(movies)} movies, {len(weekly)} movie-weeks")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stage", choices=["charts", "movies", "all"], default="all")
    parser.add_argument("--limit", type=int, default=None, help="only scrape the first N movie pages")
    args = parser.parse_args()
    if args.stage in ("charts", "all"):
        scrape_charts()
    if args.stage in ("movies", "all"):
        scrape_movies(limit=args.limit)


if __name__ == "__main__":
    main()

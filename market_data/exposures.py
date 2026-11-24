"""Country and sector weights of each instrument into exposures.

    python -m market_data.exposures            download the latest weights, then rebuild exposures
    python -m market_data.exposures --offline  rebuild exposures from the cached files only

Fetches sector weights for ETFs and the sector and country of stocks from Yahoo
Finance (via yfinance), caches them as one JSON per ticker in data/raw/exposures/,
then applies the hand-written data/reference/exposures.csv on top.
"""

import argparse
import csv
import json
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import yfinance as yf

from init_db import DATABASE_FILE

project_root = Path(__file__).parent.parent
cache_folder = project_root / "data" / "raw" / "exposures"
reference_file = project_root / "data" / "reference" / "exposures.csv"
companies_file = project_root / "data" / "reference" / "companies.csv"


def fetch_exposure(ticker, asset_type, folder=cache_folder):
    if asset_type == "etf":
        funds = yf.Ticker(ticker).funds_data
        exposure = {
            "sectors": funds.sector_weightings,
            "holdings": [{"symbol": symbol, "name": row["Name"], "weight": row["Holding Percent"]}
                        for symbol, row in funds.top_holdings.iterrows()],
        }
    else:
        info = yf.Ticker(ticker).info
        exposure = {"sector": info.get("sector"), "country": info.get("country")}

    if not any(exposure.values()):
        print(f"WARNING: no exposure data for {ticker} ({asset_type}), skipped")
        return

    exposure["as_of"] = date.today().isoformat()
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"{ticker}.json", "w", encoding="utf-8") as f:
        json.dump(exposure, f, indent=2)


UNCLASSIFIED = "Unclassified"


def sector_key(sector):
    """'Real Estate' -> 'realestate', 'Financial Services' -> 'financial_services', as in ETF weights."""
    key = sector.lower().replace(" ", "_")
    return "realestate" if key == "real_estate" else key


def clean_name(kind, name):
    """Sectors as ETF keys; 'Other' is weight the source doesn't list, so it's Unclassified, not a country."""
    if name.strip().lower() in ("other", UNCLASSIFIED.lower()):
        return UNCLASSIFIED
    return sector_key(name) if kind == "sector" else name

def clean_company(name):
    """Clean company names for consistency."""
    # "& Co" and "and Co" go whole: stripping " Co" alone leaves "Merck &" and "Eli Lilly and".
    SUFFIXES = (" Class A", " Class B", " Class C", " Class H", " Ordinary Shares", " Inc", " Corp", " Ltd",
                " PLC", " & Co", " and Co", " Co")
    stripped = True
    while stripped:
        stripped = False
        for suffix in SUFFIXES:
            if name.endswith(suffix):
                name = name[: -len(suffix)]
                stripped = True
    return name

def company_names(paths, override_path):
    """Symbol -> company name: cleaned yfinance names, then the override file on top."""
    names = {}
    for path in paths:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for holding in data.get("holdings") or []:
            names[holding["symbol"]] = clean_company(holding["name"])

    if override_path.exists():
        with open(override_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                names[row["symbol"]] = row["company"]
    return names


def parse_exposure(path, instrument_id, asset_type):
    """Turn a cached JSON into (instrument_id, kind, name, weight, source, as_of) rows."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if asset_type == "etf":
        as_of = date.fromisoformat(data["as_of"]) if data.get("as_of") else None
        companies = [(instrument_id, "company", h["symbol"], Decimal(str(h["weight"])), "yfinance", as_of)
                    for h in (data.get("holdings") or [])]
        sectors = [(instrument_id, "sector", clean_name("sector", name), Decimal(str(weight)), "yfinance", None)
            for name, weight in (data.get("sectors") or {}).items()]
        return sectors + companies

    rows = []
    if data.get("sector"):
        rows.append((instrument_id, "sector", clean_name("sector", data["sector"]), Decimal(1), "yfinance", None))
    if data.get("country"):
        rows.append((instrument_id, "country", clean_name("country", data["country"]), Decimal(1), "yfinance", None))
    return rows


def load_overrides(path):
    """(isin, kind, name, weight_pct, as_of) rows from the hand-written file, or none if it doesn't exist."""
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return [
            (r["isin"], r["kind"], r["name"], Decimal(r["weight_pct"]),
             date.fromisoformat(r["as_of"]) if r.get("as_of") else None)
            for r in csv.DictReader(f)
        ]


def apply_overrides(rows, overrides, isin_to_id):
    """Each (instrument, kind) in the overrides replaces all of yfinance's rows for it."""
    replaced = set()
    added = []
    for isin, kind, name, weight, as_of in overrides:
        if isin not in isin_to_id:
            raise ValueError(f"ISIN {isin} in the exposures file but not in the database.")
        replaced.add((isin_to_id[isin], kind))
        added.append((isin_to_id[isin], kind, clean_name(kind, name), weight, "reference", as_of))
    return [row for row in rows if (row[0], row[1]) not in replaced] + added


def normalise(rows):
    """Scale each (instrument, kind) so its weights add up to 1."""
    totals = defaultdict(Decimal)
    for instrument_id, kind, _, weight, _, _ in rows:
        totals[(instrument_id, kind)] += weight
    return [
        (instrument_id, kind, name,
            weight if kind == "company" else (weight / totals[(instrument_id, kind)]).quantize(Decimal("0.000001")),
            source, as_of)
        for instrument_id, kind, name, weight, source, as_of in rows
    ]


def save_exposures_to_db(connection, rows, names=None):
    """Replace every row in exposures and companies, all or nothing."""
    connection.begin()
    try:
        connection.execute("DELETE FROM exposures")
        if rows:
            connection.executemany(
                "INSERT INTO exposures (instrument_id, kind, name, weight, source, as_of) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        connection.execute("DELETE FROM companies")
        if names:
            connection.executemany("INSERT INTO companies (symbol, company) VALUES (?, ?)", list(names.items()))
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def run(connection, fetch=True):
    """Rebuild exposures and return how many rows were saved.

    With fetch=False, nothing is downloaded. A failed download keeps the last cached file.
    """
    instruments = connection.execute(
        "SELECT instrument_id, isin, price_ticker, asset_type FROM instruments WHERE price_ticker IS NOT NULL ORDER BY instrument_id"
    ).fetchall()

    rows = []
    paths = []
    for instrument_id, _, ticker, asset_type in instruments:
        if fetch:
            try:
                fetch_exposure(ticker, asset_type, cache_folder)
            except Exception as error:
                print(f"WARNING: exposure download failed for {ticker}, using the cache: {error!r}")
        path = cache_folder / f"{ticker}.json"
        if path.exists():
            rows += parse_exposure(path, instrument_id, asset_type)
            paths.append(path)

    isin_to_id = {isin: instrument_id for instrument_id, isin, _, _ in instruments}
    rows = normalise(apply_overrides(rows, load_overrides(reference_file), isin_to_id))
    save_exposures_to_db(connection, rows, company_names(paths, companies_file))
    return len(rows)


def print_summary(connection):
    for ticker, kind, count, source, as_of in connection.execute(
        """
        SELECT i.price_ticker, e.kind, COUNT(*), STRING_AGG(DISTINCT e.source, ', '), MAX(e.as_of)
        FROM exposures e
        JOIN instruments i ON i.instrument_id = e.instrument_id
        GROUP BY i.price_ticker, e.kind
        ORDER BY i.price_ticker, e.kind
        """
    ).fetchall():
        print(f"  {ticker:10} {kind:8} {count:3} rows from {source}{f', as of {as_of}' if as_of else ''}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Rebuild exposures from Yahoo Finance and the reference file.")
    parser.add_argument("--offline", action="store_true", help="don't download; rebuild from the cached files")
    args = parser.parse_args()

    with duckdb.connect(str(DATABASE_FILE)) as connection:
        count = run(connection, fetch=not args.offline)
        print(f"Saved {count} exposure rows\n")
        print_summary(connection)

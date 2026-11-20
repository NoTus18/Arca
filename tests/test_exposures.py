import json
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from init_db import apply_sql_files
from market_data import exposures


def write_json(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_parse_etf_sectors_and_stock_sector_and_country(tmp_path):
    etf = write_json(tmp_path / "ETF.json", {"sectors": {"technology": 0.383, "realestate": 0.018}})
    stock = write_json(tmp_path / "STK.json", {"sector": "Real Estate", "country": "United States"})

    assert exposures.parse_exposure(etf, 1, "etf") == [
        (1, "sector", "technology", Decimal("0.383"), "yfinance", None),
        (1, "sector", "realestate", Decimal("0.018"), "yfinance", None),
    ]
    assert exposures.parse_exposure(stock, 2, "reit") == [
        (2, "sector", "realestate", Decimal(1), "yfinance", None),
        (2, "country", "United States", Decimal(1), "yfinance", None),
    ]
    assert exposures.sector_key("Financial Services") == "financial_services"


def test_overrides_replace_only_their_own_kind():
    rows = [(2, "sector", "technology", Decimal(1), "yfinance", None),
            (2, "country", "Cayman Islands", Decimal(1), "yfinance", None)]
    overrides = [("KY1", "country", "United States", Decimal(100), None)]

    assert exposures.apply_overrides(rows, overrides, {"KY1": 2}) == [
        (2, "sector", "technology", Decimal(1), "yfinance", None),
        (2, "country", "United States", Decimal(100), "reference", None),
    ]
    # Factsheet sector names match the stock ones: 'Real Estate' -> 'realestate'.
    [sector] = exposures.apply_overrides([], [("KY1", "sector", "Real Estate", Decimal(100), None)], {"KY1": 2})
    assert sector == (2, "sector", "realestate", Decimal(100), "reference", None)
    with pytest.raises(ValueError, match="NOPE"):
        exposures.apply_overrides(rows, [("NOPE", "country", "X", Decimal(1), None)], {"KY1": 2})


def test_other_is_stored_as_unclassified_for_both_kinds():
    overrides = [("IE1", "country", "Other", Decimal(5), None), ("IE1", "sector", "other", Decimal(5), None)]
    names = [row[2] for row in exposures.apply_overrides([], overrides, {"IE1": 1})]
    assert names == ["Unclassified", "Unclassified"]


def test_normalise_scales_each_group_to_one():
    rows = exposures.normalise([
        (1, "country", "United States", Decimal("60"), "reference", None),
        (1, "country", "Unclassified", Decimal("39.8"), "reference", None),
        (1, "sector", "technology", Decimal("0.5"), "yfinance", None),
    ])
    weights = {(r[1], r[2]): r[3] for r in rows}
    assert weights[("sector", "technology")] == 1
    assert abs(weights[("country", "United States")] + weights[("country", "Unclassified")] - 1) <= Decimal("0.000001")


def test_run_offline_rebuilds_from_cache_and_reference(tmp_path, monkeypatch):
    connection = duckdb.connect(database=":memory:")
    apply_sql_files(connection)
    connection.execute(
        """
        INSERT INTO instruments VALUES
            (1, 'IE0000000001', 'Test ETF', 'etf', 'EUR', 'ETF1'),
            (2, 'KY0000000002', 'Test Stock', 'stock', 'USD', 'STK1'),
            (3, 'US0000000003', 'No Data', 'stock', 'USD', 'NODATA')
        """
    )
    write_json(tmp_path / "ETF1.json", {"sectors": {"technology": 0.6, "energy": 0.4}})
    write_json(tmp_path / "STK1.json", {"sector": "Technology", "country": "Cayman Islands"})
    reference = tmp_path / "exposures.csv"
    reference.write_text(
        "isin,kind,name,weight_pct,as_of\n"
        "IE0000000001,country,United States,70,2026-10-31\nIE0000000001,country,Other,30,2026-10-31\n"
        "KY0000000002,country,United States,100,\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(exposures, "cache_folder", tmp_path)
    monkeypatch.setattr(exposures, "reference_file", reference)

    assert exposures.run(connection, fetch=False) == 6

    saved = connection.execute("SELECT instrument_id, kind, name, weight, source, as_of FROM exposures ORDER BY ALL").fetchall()
    assert saved == [
        (1, "country", "Unclassified", Decimal("0.3"), "reference", date(2026, 10, 31)),
        (1, "country", "United States", Decimal("0.7"), "reference", date(2026, 10, 31)),
        (1, "sector", "energy", Decimal("0.4"), "yfinance", None),
        (1, "sector", "technology", Decimal("0.6"), "yfinance", None),
        (2, "country", "United States", Decimal("1"), "reference", None),
        (2, "sector", "technology", Decimal("1"), "yfinance", None),
    ]
    connection.close()


def test_example_reference_file_loads():
    rows = exposures.load_overrides(exposures.project_root / "reference" / "exposures.example.csv")
    assert rows and all(len(row) == 5 for row in rows)

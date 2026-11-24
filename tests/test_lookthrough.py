import json
from datetime import date
from decimal import Decimal

import duckdb
import pytest

from analysis.portfolio import lookthrough_companies
from init_db import apply_sql_files
from market_data import exposures


# Step 1: company rows from the cached yfinance top holdings

def test_parse_etf_top_holdings_into_company_rows(tmp_path):
    path = tmp_path / "ETF1.json"
    path.write_text(json.dumps({
        "sectors": {"technology": 1.0},
        "holdings": [{"symbol": "MSFT", "name": "Microsoft Corp", "weight": 0.059},
                     {"symbol": "GOOGL", "name": "Alphabet Inc Class A", "weight": 0.025}],
        "as_of": "2026-11-20",
    }), encoding="utf-8")

    companies = [r for r in exposures.parse_exposure(path, 1, "etf") if r[1] == "company"]

    assert companies == [
        (1, "company", "MSFT", Decimal("0.059"), "yfinance", date(2026, 11, 20)),
        (1, "company", "GOOGL", Decimal("0.025"), "yfinance", date(2026, 11, 20)),
    ]


def test_normalise_leaves_company_weights_alone():
    # A top 10 adds up to well under 100%; the rest is the fund's unlisted part, not to be spread.
    rows = [(1, "company", "MSFT", Decimal("0.059"), "yfinance", None),
            (1, "company", "AAPL", Decimal("0.077"), "yfinance", None)]
    assert exposures.normalise(rows) == rows


@pytest.mark.parametrize("raw, clean", [
    ("Alphabet Inc Class A", "Alphabet"),
    ("Alphabet Inc Class C", "Alphabet"),
    ("Microsoft Corp", "Microsoft"),
    ("Meta Platforms Inc Class A", "Meta Platforms"),
    ("Alibaba Group Holding Ltd Ordinary Shares", "Alibaba Group Holding"),
    # Real yfinance names from the funds' top 10s.
    ("Merck & Co Inc", "Merck"),
    ("Eli Lilly and Co", "Eli Lilly"),
    ("JPMorgan Chase & Co", "JPMorgan Chase"),
    ("China Construction Bank Corp Class H", "China Construction Bank"),
    ("HSBC Holdings PLC", "HSBC Holdings"),
    ("Taiwan Semiconductor Manufacturing Co Ltd", "Taiwan Semiconductor Manufacturing"),
])
def test_company_names_drop_share_classes_and_legal_forms(raw, clean):
    assert exposures.clean_company(raw) == clean


def test_run_builds_companies_from_the_cache_and_the_override_file(tmp_path, monkeypatch):
    connection = duckdb.connect(database=":memory:")
    apply_sql_files(connection)
    connection.execute("INSERT INTO instruments VALUES (1, 'IE0000000001', 'Test ETF', 'etf', 'EUR', 'ETF1')")
    (tmp_path / "ETF1.json").write_text(json.dumps({
        "sectors": {"technology": 1.0},
        "holdings": [{"symbol": "GOOGL", "name": "Alphabet Inc Class A", "weight": 0.03},
                     {"symbol": "GOOG", "name": "Alphabet Inc Class C", "weight": 0.02},
                     {"symbol": "2330.TW", "name": "Taiwan Semiconductor Manufacturing Co Ltd", "weight": 0.01}],
    }), encoding="utf-8")
    overrides = tmp_path / "companies.csv"
    overrides.write_text("symbol,company\n2330.TW,TSMC\n", encoding="utf-8")
    monkeypatch.setattr(exposures, "cache_folder", tmp_path)
    monkeypatch.setattr(exposures, "reference_file", tmp_path / "none.csv")
    monkeypatch.setattr(exposures, "companies_file", overrides)

    exposures.run(connection, fetch=False)

    assert connection.execute("SELECT symbol, company FROM companies ORDER BY symbol").fetchall() == [
        ("2330.TW", "TSMC"), ("GOOG", "Alphabet"), ("GOOGL", "Alphabet"),
    ]
    weights = connection.execute("SELECT name, weight FROM exposures WHERE kind = 'company' ORDER BY name").fetchall()
    assert weights == [("2330.TW", Decimal("0.01")), ("GOOG", Decimal("0.02")), ("GOOGL", Decimal("0.03"))]
    connection.close()


# Step 2: lookthrough_companies

@pytest.fixture
def connection():
    """Account 1 on 5 Jan: ETF1 worth 100, ETF2 worth 50, ETF3 worth 20 (no company data), MSFT held directly worth 30."""
    connection = duckdb.connect(database=":memory:")
    apply_sql_files(connection)
    connection.execute("INSERT INTO accounts VALUES (1, 'Broker', 'EUR')")
    connection.execute(
        """
        INSERT INTO instruments VALUES
            (1, 'IE0000000001', 'Fund One', 'etf', 'EUR', 'ETF1'),
            (2, 'IE0000000002', 'Fund Two', 'etf', 'EUR', 'ETF2'),
            (3, 'US5949181045', 'Microsoft', 'stock', 'EUR', 'MSFT'),
            (4, 'IE0000000004', 'Fund Three', 'etf', 'EUR', 'ETF3')
        """
    )
    connection.execute("INSERT INTO transactions (transaction_id, account_id, type, event_date) VALUES (1, 1, 'buy', '2026-01-05')")
    connection.execute(
        """
        INSERT INTO transaction_legs (transaction_id, leg_number, role, amount, instrument_id, source_file, source_id)
        VALUES (1, 1, 'shares', 10, 1, 't.csv', 'a'), (1, 2, 'shares', 5, 2, 't.csv', 'b'),
               (1, 3, 'shares', 1, 3, 't.csv', 'c'), (1, 4, 'shares', 2, 4, 't.csv', 'd')
        """
    )
    connection.execute(
        "INSERT INTO prices VALUES (1, '2026-01-05', 10, 10, 'EUR'), (2, '2026-01-05', 10, 10, 'EUR'), "
        "(3, '2026-01-05', 30, 30, 'EUR'), (4, '2026-01-05', 10, 10, 'EUR')"
    )
    connection.execute(
        """
        INSERT INTO exposures (instrument_id, kind, name, weight, source) VALUES
            (1, 'company', 'MSFT', 0.10, 'yfinance'), (1, 'company', 'GOOGL', 0.05, 'yfinance'),
            (1, 'company', 'GOOG', 0.05, 'yfinance'), (2, 'company', 'MSFT', 0.20, 'yfinance')
        """
    )
    # MSFT has no row here on purpose: it falls back to the direct holding's name.
    connection.execute("INSERT INTO companies VALUES ('GOOGL', 'Alphabet'), ('GOOG', 'Alphabet')")
    yield connection
    connection.close()


def rows(connection, **filters):
    return {r["company"]: r for r in lookthrough_companies(connection, [1], **filters)}


def test_direct_and_etf_exposure_to_one_company_add_up(connection):
    msft = rows(connection)["Microsoft"]
    # Direct 30; via ETF1 100 x 10% = 10 and via ETF2 50 x 20% = 10.
    assert (msft["direct_eur"], msft["via_etfs_eur"], msft["total_eur"]) == (30, 20, 50)
    assert sorted(msft["sources"]) == [("ETF1", 10), ("ETF2", 10), ("MSFT", 30)]
    assert msft["is_rest"] is False


def test_share_classes_merge_into_one_company(connection):
    alphabet = rows(connection)["Alphabet"]
    assert alphabet["total_eur"] == 10 and alphabet["direct_eur"] == 0  # 100 x (5% + 5%)
    assert alphabet["sources"] == [("ETF1", 10)]  # one source per fund, not one per share class


def test_unlisted_part_of_each_fund_is_its_own_row_last(connection):
    result = lookthrough_companies(connection, [1])
    assert [r["company"] for r in result] == ["Microsoft", "Alphabet", "Rest of ETF1", "Rest of ETF2", "Rest of ETF3"]
    by_name = {r["company"]: r for r in result}
    assert by_name["Rest of ETF1"]["total_eur"] == 80 and by_name["Rest of ETF1"]["is_rest"] is True
    assert by_name["Rest of ETF3"]["total_eur"] == 20  # no company data: all of it is the rest
    # Everything adds back up to the 200 held, so the weights add up to 100%.
    assert sum(r["total_eur"] for r in result) == 200
    assert sum(r["weight_pct"] for r in result) == 100


def test_a_filter_to_one_fund_still_works(connection):
    result = rows(connection, tickers=["ETF2"])
    assert set(result) == {"Microsoft", "Rest of ETF2"}
    assert result["Microsoft"]["direct_eur"] == 0 and result["Microsoft"]["via_etfs_eur"] == 10
    assert result["Microsoft"]["weight_pct"] == 20

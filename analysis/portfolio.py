import csv
import functools
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
import statistics


def check_known(connection, column, given):
    """Raise if any value in `given` isn't in instruments.<column>. None means no filter."""
    if given is None:
        return
    known = {row[0] for row in connection.execute(f"SELECT DISTINCT {column} FROM instruments").fetchall()}
    unknown = set(given) - known
    if unknown:
        raise ValueError(f"Unknown {column}: {sorted(unknown)}")


# Join and filter shared by every query on a selection. Cash rows have no instrument,
# so they pass only on include_cash; holding rows must pass every filter that is set.
SELECTION = """
    FROM daily_valuation v
    LEFT JOIN instruments i ON i.instrument_id = v.instrument_id
    WHERE list_contains($account_ids, v.account_id)
      AND (
        (v.instrument_id IS NULL AND $include_cash)
        OR (
          v.instrument_id IS NOT NULL
          AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
          AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
        )
      )
"""


def portfolio_value(connection, account_ids, asset_types=None, tickers=None, include_cash=True):
    """Total value in EUR per day of the selection, as (date, total_value) rows.

    asset_types and tickers of None mean no filter; all filters combine with AND.
    """
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    params = {
        "account_ids": account_ids,
        "tickers": tickers,
        "asset_types": asset_types,
        "include_cash": include_cash,
    }

    # SUM skips NULLs, so a missing exchange rate would silently lower the total.
    missing = connection.execute(
        f"""
        SELECT v.date, v.account_id, v.currency
        {SELECTION}
          AND v.value_eur IS NULL
        ORDER BY v.date
        LIMIT 5
        """,
        params,
    ).fetchall()
    if missing:
        raise ValueError(f"No exchange rate for (date, account_id, currency): {missing}")

    return connection.execute(
        f"""
        SELECT v.date, SUM(v.value_eur) AS total_value
        {SELECTION}
        GROUP BY v.date
        ORDER BY v.date
        """,
        params,
    ).fetchall()


def average_cost_positions(connection, account_ids, asset_types=None, tickers=None):
    rows = connection.execute(
        """
        SELECT t.account_id, i.price_ticker, t.quantity, t.cash_eur FROM trades t
        JOIN instruments i ON i.instrument_id = t.instrument_id
        WHERE list_contains($account_ids, t.account_id)
          AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
          AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
        ORDER BY t.event_date, t.transaction_id
        """
        ,
        {
            "account_ids": account_ids,
            "tickers": tickers,
            "asset_types": asset_types,
        }
    ).fetchall()

    positions = {}
    for account_id, ticker, quantity, cash_eur in rows:
        position = positions.setdefault(
            (account_id, ticker), {"qty": Decimal(0), "cost": Decimal(0), "realised": Decimal(0)}
        )
        if quantity > 0:
            position["qty"] += quantity
            position["cost"] += -(cash_eur)
        else:
            # Average cost: the sold shares take their share of the cost, at the qty before the sale.
            sold = -quantity
            sold_cost = position["cost"] * sold / position["qty"]
            position["realised"] += cash_eur - sold_cost
            position["cost"] -= sold_cost
            position["qty"] -= sold
    return positions


def holdings_snapshot(connection, account_ids, asset_types=None, tickers=None):
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    positions = average_cost_positions(connection, account_ids, asset_types, tickers)

    value_rows = connection.execute(
        f"""
        SELECT v.account_id, i.price_ticker, v.value_eur
        {SELECTION}
        AND v.date = (SELECT MAX(date) FROM daily_valuation)
        """,
        {
            "account_ids": account_ids,
            "tickers": tickers,
            "asset_types": asset_types,
            "include_cash": False,
        }
    ).fetchall()
    values = {(account_id, ticker): value_eur for account_id, ticker, value_eur in value_rows}
    by_ticker = {}
    for (account_id, ticker), position in positions.items():
        if position["qty"] > 0:
            total = by_ticker.setdefault(ticker, {"qty": Decimal(0), "cost": Decimal(0), "value": Decimal(0)})
            total["qty"] += position["qty"]
            total["cost"] += position["cost"]
            total["value"] += values[(account_id, ticker)]
    total_value = sum(total["value"] for total in by_ticker.values())
    result = []
    for ticker, sums in by_ticker.items():
        result.append({
            "ticker": ticker,
            "qty": sums["qty"],
            "avg_cost_eur": sums["cost"] / sums["qty"],
            "cost": sums["cost"],
            "value_eur": sums["value"],
            "weight": sums["value"] / total_value,
            "unrealised_pnl_eur": sums["value"] - sums["cost"],
            "unrealised_pnl_percent": (sums["value"] - sums["cost"]) / sums["cost"] * 100,
            "weight_pct": (sums["value"] / total_value) * 100,
        })
    result.sort(key=lambda row: row["value_eur"], reverse=True)
    return result


def realised_gains(connection, account_ids, asset_types=None, tickers=None):
    """Realised gain in EUR per ticker from sells at average cost, fully sold tickers included."""
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    by_ticker = {}
    for (_, ticker), position in average_cost_positions(connection, account_ids, asset_types, tickers).items():
        by_ticker[ticker] = by_ticker.get(ticker, Decimal(0)) + position["realised"]
    return [
        {"ticker": ticker, "realised_eur": realised}
        for ticker, realised in sorted(by_ticker.items())
        if realised != 0
    ]


COUNTRIES_FILE = Path(__file__).parent.parent / "reference" / "countries.csv"
UNCLASSIFIED = "Unclassified"


@functools.cache
def load_countries(path=COUNTRIES_FILE):
    """Country -> (region, market_type). Regions are geographic; market type is MSCI's."""
    with open(path, newline="", encoding="utf-8") as f:
        return {r["country"]: (r["region"], r["market_type"]) for r in csv.DictReader(f)}


# Groups that spread each holding over its weights in exposures; region and market roll countries up.
EXPOSURE_KINDS = {"country": "country", "region": "country", "market": "country", "sector": "sector"}
ROLL_UPS = {"region": 0, "market": 1}


def allocation(connection, account_ids, by="asset_type", asset_types=None, tickers=None, include_cash=True):
    """Latest value per group. Country, region and sector leave out cash and holdings without weights."""
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    GROUPS = {
        "asset_type": "COALESCE(i.asset_type, 'cash')",
        "currency": "v.currency",
        "account": "v.account_id",
        "ticker": "COALESCE(i.price_ticker, 'cash')",
    }
    if by not in GROUPS and by not in EXPOSURE_KINDS:
        raise ValueError(f"by must be one of {sorted([*GROUPS, *EXPOSURE_KINDS])}")

    params = {
        "account_ids": account_ids,
        "tickers": tickers,
        "asset_types": asset_types,
        "include_cash": include_cash,
    }
    if by in GROUPS:
        rows = connection.execute(
            f"""
            SELECT {GROUPS[by]} AS grp, SUM(v.value_eur) AS value_eur
            {SELECTION}
            AND v.date = (SELECT MAX(date) FROM daily_valuation)
            GROUP BY grp
            ORDER BY value_eur DESC
            """,
            params,
        ).fetchall()
        rows = [(grp, value, None) for grp, value in rows]
    else:
        parts = connection.execute(
            f"""
            SELECT e.name, s.price_ticker, SUM(s.value_eur * e.weight)
            FROM (
                SELECT v.instrument_id, i.price_ticker, v.value_eur
                {SELECTION}
                AND v.date = (SELECT MAX(date) FROM daily_valuation)
            ) s
            JOIN exposures e ON e.instrument_id = s.instrument_id AND e.kind = $kind
            GROUP BY e.name, s.price_ticker
            """,
            {**params, "include_cash": False, "kind": EXPOSURE_KINDS[by]},
        ).fetchall()

        if by in ROLL_UPS:
            countries = load_countries()
            unknown = {name for name, _, _ in parts} - countries.keys() - {UNCLASSIFIED}
            if unknown:
                raise ValueError(f"Countries missing from reference/countries.csv: {sorted(unknown)}")
            column = ROLL_UPS[by]
            parts = [(name if name == UNCLASSIFIED else countries[name][column], ticker, value)
                     for name, ticker, value in parts]

        groups = defaultdict(lambda: defaultdict(Decimal))
        for name, ticker, value in parts:
            groups[name][ticker] += value
        rows = [(name, sum(by_ticker.values()), sorted(by_ticker.items(), key=lambda p: p[1], reverse=True))
                for name, by_ticker in groups.items()]
        # Unclassified is a gap in the data, not a group, so it goes last whatever its size.
        rows.sort(key=lambda row: (row[0] == UNCLASSIFIED, -row[1], row[0]))

    total = sum(value_eur for _, value_eur, _ in rows)
    result = []
    for grp, value, by_ticker in rows:
        row = {
            "group": grp,
            "value_eur": value,
            "weight_pct": (value / total) * 100 if total else Decimal(0),
        }
        if by_ticker is not None:
            row["by_ticker"] = by_ticker
        result.append(row)
    return result

def performance(connection, account_ids, asset_types=None, tickers=None):
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    if asset_types is None and tickers is None:
        rows = connection.execute(
            """
            SELECT t.event_date, SUM(CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6))) AS amount_eur
            FROM transaction_legs tl
            JOIN transactions t ON t.transaction_id = tl.transaction_id
            LEFT JOIN daily_fx f ON f.quote_currency = tl.currency AND f.date = t.event_date
            WHERE list_contains($account_ids, t.account_id)
            AND tl.role IN ('deposit', 'withdrawal', 'reward')
            GROUP BY t.event_date
            """,
            {"account_ids": account_ids},
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT t.event_date, -SUM(t.cash_eur) AS amount_eur
            FROM trades t
            JOIN instruments i ON i.instrument_id = t.instrument_id
            WHERE list_contains($account_ids, t.account_id)
            AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
            AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
            GROUP BY t.event_date
            """,
            {
                "account_ids": account_ids,
                "tickers": tickers,
                "asset_types": asset_types,
            }
        ).fetchall()

    flows = dict(rows)

    include_cash = asset_types is None and tickers is None
    if not include_cash:
        # Holdings view: dividends are money coming back, like sell proceeds. The account
        # view needs nothing, since they already sit in the cash it counts.
        for row in dividends(connection, account_ids, by="date", asset_types=asset_types, tickers=tickers):
            flows[row["group"]] = flows.get(row["group"], Decimal(0)) - row["net_eur"]
    value_rows = portfolio_value(connection, account_ids, asset_types=asset_types, tickers=tickers, include_cash=include_cash)
    result = []
    invested = Decimal(0)
    for day, value in value_rows:
        invested += flows.get(day, Decimal(0))
        result.append({
            "date": day,
            "value_eur": value,
            "invested_eur": invested,
            "unrealised_pnl_eur": value - invested,
            "unrealised_pnl_percent": (value - invested) / invested * 100 if invested > 0 else None,
        })
    return result


def dividends(connection, account_ids, by="month", asset_types=None, tickers=None):
    """Dividends in EUR per group, as gross, withholding tax (negative) and net."""
    if not account_ids:
        raise ValueError("account_ids cannot be empty")
    check_known(connection, "price_ticker", tickers)
    check_known(connection, "asset_type", asset_types)

    GROUPS = {
        "date": "t.event_date",
        "month": "strftime(t.event_date, '%Y-%m')",
        "year": "year(t.event_date)",
        "ticker": "i.price_ticker",
    }
    if by not in GROUPS:
        raise ValueError(f"by must be one of {sorted(GROUPS)}")

    rows = connection.execute(
        f"""
        WITH legs AS (
            SELECT {GROUPS[by]} AS grp, tl.role,
                   CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6)) AS amount_eur
            FROM transaction_legs tl
            JOIN transactions t ON t.transaction_id = tl.transaction_id
            JOIN instruments i ON i.instrument_id = t.instrument_id
            LEFT JOIN daily_fx f ON f.quote_currency = tl.currency AND f.date = t.event_date
            WHERE t.type = 'dividend'
              AND tl.role IN ('income', 'tax')
              AND list_contains($account_ids, t.account_id)
              AND ($tickers IS NULL OR list_contains($tickers, i.price_ticker))
              AND ($asset_types IS NULL OR list_contains($asset_types, i.asset_type))
        )
        SELECT grp,
               COALESCE(SUM(amount_eur) FILTER (WHERE role = 'income'), 0) AS gross_eur,
               COALESCE(SUM(amount_eur) FILTER (WHERE role = 'tax'), 0) AS tax_eur
        FROM legs
        GROUP BY grp
        ORDER BY grp
        """,
        {
            "account_ids": account_ids,
            "tickers": tickers,
            "asset_types": asset_types,
        },
    ).fetchall()

    return [
        {"group": grp, "gross_eur": gross, "tax_eur": tax, "net_eur": gross + tax}
        for grp, gross, tax in rows
    ]


def fees(connection, account_ids, by="year"):
    """Fees paid in EUR per group, as negative amounts. Includes currency conversion fees.

    No holdings filter: conversion fees belong to no instrument.
    """
    if not account_ids:
        raise ValueError("account_ids cannot be empty")

    GROUPS = {
        "month": "strftime(t.event_date, '%Y-%m')",
        "year": "year(t.event_date)",
        "account": "t.account_id",
        "type": "t.type",
    }
    if by not in GROUPS:
        raise ValueError(f"by must be one of {sorted(GROUPS)}")

    rows = connection.execute(
        f"""
        SELECT {GROUPS[by]} AS grp,
               SUM(CAST(tl.amount / CASE WHEN tl.currency = 'EUR' THEN 1 ELSE f.rate END AS DECIMAL(18, 6))) AS fees_eur
        FROM transaction_legs tl
        JOIN transactions t ON t.transaction_id = tl.transaction_id
        LEFT JOIN daily_fx f ON f.quote_currency = tl.currency AND f.date = t.event_date
        WHERE tl.role = 'fee'
          AND list_contains($account_ids, t.account_id)
        GROUP BY grp
        ORDER BY grp
        """,
        {"account_ids": account_ids},
    ).fetchall()

    return [{"group": grp, "fees_eur": amount} for grp, amount in rows]

def time_weighted(history):
    growth = Decimal(1)
    prev_value = Decimal(0)
    prev_invested = Decimal(0)
    result = []
    for row in history:
        flow = row["invested_eur"] - prev_invested
        base = prev_value + flow
        if base > 0:
            growth *= (row["value_eur"]) / base
        result.append((
            row["date"],
            growth))
        prev_value = row["value_eur"]
        prev_invested = row["invested_eur"]
    return result

def xirr(cashflows, period=365):
    """Rate per `period` days that makes the cash flows add up to zero (money in negative), or None."""
    def npv(rate):
        first = cashflows[0][0]
        return sum(amount / (1 + rate) ** (Decimal((day - first).days) / period) for day, amount in cashflows)

    low, high = Decimal('-0.99'), Decimal(10)
    if (npv(low) > 0) == (npv(high) > 0):
        return None

    for _ in range(100):
        mid = (low + high) / 2
        if (npv(mid) > 0) == (npv(low) > 0):
            low = mid
        else:
            high = mid

    return mid


def returns(history, start=None):
    """Time- and money-weighted return in % from start (None: all). Per year only over a year or more."""
    if not history:
        return None

    if start is None:
        index = 0
    else:
        index = next((i for i, row in enumerate(history) if row["date"] >= start), len(history) - 1)

    if index > 0:
        base = history[index - 1]
    else:
        base = None

    shown = history[index:]
    last = history[-1]

    if base is None:
        first_date = history[0]["date"]
    else:
        first_date = base["date"]
    days = (last["date"] - first_date).days

    growth = time_weighted(history)
    growth_end = growth[-1][1]
    growth_base = growth[index - 1][1] if index > 0 else Decimal(1)
    twr = (growth_end / growth_base) - 1
    cashflows = []
    if base is not None:
        cashflows.append((base["date"], -base["value_eur"]))

    prev = base["invested_eur"] if base is not None else Decimal(0)
    for row in shown:
        flow = row["invested_eur"] - prev
        if flow != 0:
            cashflows.append((row["date"], -flow))
        prev = row["invested_eur"]

    cashflows.append((last["date"], last["value_eur"]))

    annualised = days >= 365
    # Short ranges solve for the return over the range itself: per year, a few days blow up past any bound.
    rate = xirr(cashflows, period=365 if annualised else days) if days > 0 else None
    if annualised:
        twr = (1 + twr) ** (Decimal(365) / days) - 1

    return {
        "time_weighted": twr * 100,
        "money_weighted": rate * 100 if rate is not None else None,
        "days": days,
        "annualised": annualised,
    }

def lookthrough_companies(connection, account_ids, asset_types=None, tickers=None):
    """Money by company, ETFs opened up into their listed holdings; each fund's unlisted part is its own row."""
    values = {r["group"]: r["value_eur"]
              for r in allocation(connection, account_ids, by="ticker", asset_types=asset_types, tickers=tickers,
                                  include_cash=False)
              if r["value_eur"] != 0}

    instruments = {ticker: (instrument_id, asset_type, name) for instrument_id, ticker, asset_type, name in
                   connection.execute("SELECT instrument_id, price_ticker, asset_type, name FROM instruments").fetchall()}
    names = dict(connection.execute("SELECT symbol, company FROM companies").fetchall())
    weights = defaultdict(list)
    for instrument_id, symbol, weight in connection.execute(
            "SELECT instrument_id, name, weight FROM exposures WHERE kind = 'company'").fetchall():
        weights[instrument_id].append((symbol, weight))

    parts = defaultdict(list)
    rest_names = set()

    def company_of(symbol):
        if symbol in names:
            return names[symbol]
        if symbol in instruments:
            return instruments[symbol][2]
        return symbol

    for ticker, value in values.items():
        instrument_id, asset_type, _ = instruments[ticker]
        if asset_type != "etf":
            parts[company_of(ticker)].append((ticker, value, True))
        else:
            for symbol, weight in weights[instrument_id]:
                parts[company_of(symbol)].append((ticker, value * weight, False))
            rest = f"Rest of {ticker}"
            parts[rest].append((ticker, value * (1 - sum(w for _, w in weights[instrument_id])), False))
            rest_names.add(rest)

    result = []
    for company, pieces in parts.items():
        direct = sum((eur for _, eur, is_direct in pieces if is_direct), Decimal(0))
        via = sum((eur for _, eur, is_direct in pieces if not is_direct), Decimal(0))
        by_source = defaultdict(Decimal)
        for t, eur, _ in pieces:
            by_source[t] += eur
        sources = sorted(by_source.items(), key=lambda s: s[1], reverse=True)
        result.append({"company": company, "direct_eur": direct, "via_etfs_eur": via, "total_eur": direct + via,
                        "sources": sources, "is_rest": company in rest_names})

    total = sum((r["total_eur"] for r in result), Decimal(0))
    for r in result:
        r["weight_pct"] = r["total_eur"] / total * 100 if total else Decimal(0)

    result.sort(key=lambda r: (r["is_rest"], -r["total_eur"]))
    return result

def effective_number(values, exclude=(), diffuse=()):
    kept = {k: Decimal(str(v)) for k, v in values.items() if k not in exclude}

    total = sum(kept.values())
    if total == 0:
        return None

    sum_sq = sum((v / total) ** 2 for k, v in kept.items() if k not in diffuse)
    if sum_sq == 0:
        return None
    else:
        return 1 / sum_sq

def daily_returns(history):
    """Return per trading day, deposits taken out: (date, fraction). Weekends and days before any money are skipped."""
    growth = time_weighted(history)
    result = []
    for i in range(1, len(history)):
        day = history[i]["date"]
        if day.weekday() >= 5:
            continue
        if history[i - 1]["value_eur"] <= 0:
            continue
        result.append((day, growth[i][1] / growth[i - 1][1] - 1))
    return result

def drawdowns(history):
    """How far below its highest point so far, per day: 0 at a new high, −0.25 = 25% below."""
    growth = time_weighted(history)
    peak = Decimal(0)
    result = []
    for day, g in growth:
        if g > peak:
            peak = g
        result.append((day, g / peak - 1))
    return result


def risk_summary(history):
    """Volatility, drawdowns and the worst and best day and month, from returns with deposits taken out (in %)."""
    returns = daily_returns(history)
    if not returns:
        return None
    downs = drawdowns(history)

    start = next(r["date"] for r in history if r["value_eur"] > 0)
    end = history[-1]["date"]
    observations = len(returns)
    values = [r for _, r in returns]
    volatility = statistics.stdev(values) * Decimal(252).sqrt() * 100 if observations >= 2 else None

    # The deepest point, the last high before it, and the first day back at that high (None if not yet).
    bottom_day, depth = min(downs, key=lambda d: d[1])
    peak = recovery = None
    if depth < 0:
        peak = max(day for day, d in downs if day < bottom_day and d == 0)
        recovery = next((day for day, d in downs if day > bottom_day and d == 0), None)
    else:
        bottom_day = None

    worst_date, worst = min(returns, key=lambda x: x[1])
    best_date, best = max(returns, key=lambda x: x[1])

    months = defaultdict(lambda: Decimal(1))
    for day, r in returns:
        months[day.strftime("%Y-%m")] *= 1 + r
    worst_month, month_growth = min(months.items(), key=lambda m: m[1])

    return {
        "volatility": volatility,
        "max_drawdown": {"depth": depth * 100, "peak": peak, "bottom": bottom_day, "recovery": recovery},
        "current_drawdown": downs[-1][1] * 100,
        "worst_day": (worst_date, worst * 100),
        "best_day": (best_date, best * 100),
        "worst_month": (worst_month, (month_growth - 1) * 100),
        "start": start,
        "end": end,
        "observations": observations,
        "enough": observations >= 60,
    }


def correlations(connection, account_ids, asset_types=None, tickers=None):
    held = sorted(r["group"] for r in allocation(connection, account_ids, by="ticker", asset_types=asset_types,
                    tickers=tickers, include_cash=False) if r["value_eur"] != 0)

    eur_returns = connection.execute(
        """
        SELECT i.price_ticker, p.date,
        p.adj_close / CASE WHEN p.currency = 'EUR' THEN 1 ELSE f.rate END AS eur_price
        FROM prices p
        JOIN instruments i ON i.instrument_id = p.instrument_id
        LEFT JOIN daily_fx f ON f.quote_currency = p.currency AND f.date = p.date
        WHERE list_contains($tickers, i.price_ticker)
        ORDER BY i.price_ticker, p.date
        """,
        {"tickers": held},
    ).fetchall()

    series = defaultdict(dict)
    last_price = {}
    for ticker, day, price in eur_returns:
        if price is None:
            continue
        if ticker in last_price:
            series[ticker][day] = price / last_price[ticker] - 1
        last_price[ticker] = price

    def correlation(a, b):
        days = sorted(a.keys() & b.keys())
        if len(days) < 60:
            return None
        try:
            return statistics.correlation([float(a[d]) for d in days], [float(b[d]) for d in days])
        except statistics.StatisticsError:
            return None

    portfolio = dict(daily_returns(performance(connection, account_ids, asset_types=asset_types, tickers=tickers)))
    return {
        "tickers": held,
        "matrix": [[1 if a == b else correlation(series[a], series[b]) for b in held] for a in held],
        "to_portfolio": {t: correlation(series[t], portfolio) for t in held}
    }
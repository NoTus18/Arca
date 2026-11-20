from datetime import date
from decimal import Decimal

import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/income", name="Dividends & fees", order=4)

DIVIDEND_GROUPS = {"month": "Month", "year": "Year", "ticker": "Holding"}
FEE_GROUPS = {"year": "Year", "month": "Month", "type": "Type", "account": "Account"}
# Fee colours keep clear of blue and orange, which mean received and withheld in the dividends chart.
FEE_TYPES = {"buy": ("Buy", ui.SERIES[2]), "sell": ("Sell", ui.SERIES[4]),
             "conversion": ("Currency conversion", ui.SERIES[6])}

layout = html.Div(
    [
        ui.page_header("Dividends & fees", "Money paid out to you, and money paid to brokers. In euros on the day."),
        dmc.Stack(
            [
                ui.panel(html.Div(id="dividends-content"), title="Dividends",
                         right=dmc.SegmentedControl(id="dividends-by", value="month",
                                                    data=[{"value": k, "label": v} for k, v in DIVIDEND_GROUPS.items()],
                                                    persistence=True, persistence_type="local")),
                ui.panel(html.Div(id="fees-content"), title="Fees",
                         right=dmc.SegmentedControl(id="fees-by", value="year",
                                                    data=[{"value": k, "label": v} for k, v in FEE_GROUPS.items()],
                                                    persistence=True, persistence_type="local")),
            ],
            gap="lg",
        ),
    ]
)


def every_month(rows):
    """Months with no dividend still get a bar of zero, so gaps in income are visible."""
    if not rows:
        return rows
    found = {r["group"]: r for r in rows}
    year, month = map(int, rows[0]["group"].split("-"))
    last = rows[-1]["group"]
    filled = []
    while True:
        key = f"{year:04d}-{month:02d}"
        filled.append(found.get(key, {"group": key, "gross_eur": Decimal(0), "tax_eur": Decimal(0), "net_eur": Decimal(0)}))
        if key == last:
            return filled
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def bar_chart(rows, series, horizontal=False, **props):
    return dmc.BarChart(
        h=max(220, 36 * len(rows)) if horizontal else 260,
        data=rows, dataKey="label", series=series,
        orientation="vertical" if horizontal else "horizontal",
        gridAxis="x" if horizontal else "y", tickLine="none", maxBarWidth=28, barProps={"radius": 3},
        valueFormatter={"function": "eurCents"},
        yAxisProps={"width": 140} if horizontal else {"tickFormatter": {"function": "eurCompact"}, "width": 64},
        xAxisProps={"tickFormatter": {"function": "eurCompact"}} if horizontal else {},
        **props,
    )


@callback(Output("dividends-content", "children"), *ui.FILTERS, Input("dividends-by", "value"))
@ui.guarded
def render_dividends(accounts, asset_types, tickers, by):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()

    with data.connect() as connection:
        rows = portfolio.dividends(connection, account_ids, by=by, asset_types=asset_types, tickers=tickers)
        names = data.instruments(connection)

    if not rows:
        return ui.empty("No dividends in this selection")

    gross = sum((r["gross_eur"] for r in rows), Decimal(0))
    tax = sum((r["tax_eur"] for r in rows), Decimal(0))
    net = gross + tax

    if by == "month":
        rows = every_month(rows)
        label = lambda g: f"{date(int(g[:4]), int(g[5:]), 1):%b %y}"
    elif by == "ticker":
        rows = sorted(rows, key=lambda r: r["net_eur"], reverse=True)
        label = lambda g: names[g][0] if g in names else g
    else:
        label = str

    chart = bar_chart(
        [{"label": label(r["group"]), "net": data.num(r["net_eur"]), "withheld": data.num(-r["tax_eur"])} for r in rows],
        [{"name": "net", "label": "Received", "color": ui.SERIES[0]},
         {"name": "withheld", "label": "Withheld tax", "color": ui.SERIES[1]}],
        horizontal=by == "ticker", type="stacked",
    )

    return dmc.Stack(
        [
            dmc.SimpleGrid(
                [ui.stat("Received", ui.eur(net)),
                 ui.stat("Before tax", ui.eur(gross)),
                 ui.stat("Withheld tax", ui.eur(-tax)),
                 ui.stat("Withholding rate", ui.pct(-tax / gross * 100 if gross else None))],
                cols={"base": 2, "md": 4}, spacing="lg",
            ),
            ui.legend([("Received", ui.SERIES[0]), ("Withheld tax", ui.SERIES[1])]),
            chart,
        ],
        gap="md",
    )


@callback(Output("fees-content", "children"), *ui.FILTERS, Input("fees-by", "value"))
@ui.guarded
def render_fees(accounts, asset_types, tickers, by):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()

    with data.connect() as connection:
        rows = data.fees_by_type(connection, account_ids, by)
        names = data.accounts(connection)

    if not rows:
        return ui.empty("No fees recorded", "Lightyear deposit fees aren't in its exports.")

    kind = lambda t: FEE_TYPES.get(t, (str(t).title(), ui.SERIES[3]))
    label = {"account": lambda g: names.get(g, str(g)), "type": lambda g: kind(g)[0]}.get(by, str)
    order = list(FEE_TYPES)
    types = sorted({t for _, t, _ in rows}, key=lambda t: order.index(t) if t in order else len(order))
    # One row per group with every type, so the stacks line up.
    bars = {}
    for group, fee_type, amount in rows:
        bars.setdefault(group, {"label": label(group), **dict.fromkeys(types, 0)})[fee_type] = data.num(-amount)
    total = -sum((amount for _, _, amount in rows), Decimal(0))
    note = "Paid on trades and currency conversions. Tax withheld on dividends is in the chart above."
    if data.is_holdings_view(asset_types, tickers):
        note += " Fees cover the whole accounts: conversion fees belong to no holding."

    return dmc.Stack(
        [
            dmc.Group([ui.stat("Paid in fees", ui.eur(total)), dmc.Text(note, size="xs", c="dimmed", maw=420)],
                      justify="space-between", align="flex-end"),
            ui.legend([kind(t) for t in types]),
            bar_chart(list(bars.values()), [{"name": t, "label": kind(t)[0], "color": kind(t)[1]} for t in types],
                      horizontal=by in ("type", "account"), type="stacked"),
        ],
        gap="md",
    )

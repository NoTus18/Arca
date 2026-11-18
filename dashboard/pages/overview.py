from decimal import Decimal

import dash
import dash_mantine_components as dmc
from dash import Output, callback, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/", name="Overview", order=0)

layout = html.Div(id="overview-content")


def bridge(parts, total):
    """Rows that add up to `total`: each source of gain as a bar left or right of zero."""
    largest = max([abs(v) for _, v, _ in parts] + [Decimal("0.01")])

    def row(label, value, note, class_name="bridge-row"):
        width = f"{float(abs(value) / largest * 100):.1f}%"
        return html.Div(
            [
                dmc.Stack([dmc.Text(label, size="sm"), note and dmc.Text(note, size="xs", c="dimmed")], gap=0),
                html.Div([html.Div(html.Div(className="bridge-bar down", style={"width": width}) if value < 0 else None),
                          html.Div(html.Div(className="bridge-bar up", style={"width": width}) if value > 0 else None)],
                         className="bridge-track", role="presentation"),
                dmc.Text(ui.signed_text(ui.eur(value, signed=True), value), ta="right", size="sm"),
            ],
            className=class_name,
        )

    rows = [row(label, value, note) for label, value, note in parts]
    rows.append(html.Div(
        [dmc.Text("Total gain", size="sm", fw=650), html.Div(),
         dmc.Text(ui.signed_text(ui.eur(total, signed=True), total, fw=650), ta="right", size="sm")],
        className="bridge-row bridge-total",
    ))
    return html.Div(rows)


@callback(Output("overview-content", "children"), *ui.FILTERS)
@ui.guarded
def render(accounts, asset_types, tickers):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()
    holdings_view = data.is_holdings_view(asset_types, tickers)

    with data.connect() as connection:
        history = portfolio.performance(connection, account_ids, asset_types, tickers)
        snapshot = portfolio.holdings_snapshot(connection, account_ids, asset_types, tickers)
        realised = portfolio.realised_gains(connection, account_ids, asset_types, tickers)
        dividends = portfolio.dividends(connection, account_ids, by="year", asset_types=asset_types, tickers=tickers)
        fees = [] if holdings_view else portfolio.fees(connection, account_ids, by="type")
        by_type = portfolio.allocation(connection, account_ids, by="asset_type", asset_types=asset_types,
                                       tickers=tickers, include_cash=not holdings_view)
        names = data.instruments(connection)

    if not history:
        return ui.empty("Nothing to show yet", "Run python update_all.py to import your exports.")

    latest = history[-1]
    value, invested, gain = latest["value_eur"], latest["invested_eur"], latest["unrealised_pnl_eur"]

    # Where the gain came from. Trade fees are already in the average cost, so only other fees
    # (currency conversions) get their own row; what is left is mostly currency moves on cash.
    unrealised = sum((h["unrealised_pnl_eur"] for h in snapshot), Decimal(0))
    realised_total = sum((r["realised_eur"] for r in realised), Decimal(0))
    dividends_net = sum((d["net_eur"] for d in dividends), Decimal(0))
    other_fees = sum((f["fees_eur"] for f in fees if f["group"] not in ("buy", "sell")), Decimal(0))
    parts = [
        ("Price gains on holdings", unrealised, "Value now against average cost"),
        ("Realised on sales", realised_total, None),
        ("Dividends", dividends_net, "After withholding tax"),
    ]
    if not holdings_view:
        parts.append(("Conversion and other fees", other_fees, None))
    rest = gain - sum(v for _, v, _ in parts)
    if abs(rest) >= Decimal("0.01"):
        parts.append(("Currency moves and other", rest, "Cash in other currencies, rounding"))

    invested_label = "Net invested" if holdings_view else "Deposited"
    hero = dmc.Group(
        [
            dmc.Stack(
                [
                    dmc.Text("Value of selected holdings" if holdings_view else "Portfolio value", c="dimmed", size="sm"),
                    dmc.Text(ui.eur(value), className="hero-value"),
                    ui.signed_text(
                        f"{ui.eur(gain, signed=True)} ({ui.pct(latest['unrealised_pnl_percent'], signed=True)}) all time",
                        gain, fw=600,
                    ),
                ],
                gap=8,
            ),
            dmc.Group(
                [ui.stat(invested_label, ui.eur(invested, decimals=0)),
                 ui.stat("Open positions", str(len(snapshot))),
                 ui.stat("Since", f"{history[0]['date']:%b %Y}")],
                gap="xl", align="flex-end",
            ),
        ],
        justify="space-between", align="flex-end", className="hero", wrap="wrap", gap="lg",
    )

    chart = ui.time_chart(
        [{"date": r["date"].isoformat(), "value": data.num(r["value_eur"]), "invested": data.num(r["invested_eur"])}
         for r in history],
        [{"name": "value", "label": "Value", "color": ui.SERIES[0], "type": "area"},
         {"name": "invested", "label": invested_label, "color": ui.SERIES[1], "type": "line",
          "strokeDasharray": "5 4"}],
        kind=dmc.CompositeChart,
        h=300,
    )

    top = snapshot[:6]
    holdings_table = ui.table(
        ["Holding", "Value", "Gain", "Weight"],
        [[dmc.Stack([dmc.Text(names[h["ticker"]][0], size="sm", fw=500, truncate=True, maw=220),
                     dmc.Text(h["ticker"], size="xs", c="dimmed")], gap=0),
          ui.eur(h["value_eur"]),
          ui.signed_text(ui.pct(h["unrealised_pnl_percent"], signed=True), h["unrealised_pnl_eur"], size="sm"),
          ui.weight_bar(h["weight_pct"])] for h in top],
        numeric=(1, 2),
    ) if top else ui.empty("No open positions in this selection")

    type_colors = {t: ui.SERIES[i] for i, t in enumerate(["etf", "stock", "reit", "cash"])}
    donut_data = [{"name": ui.asset_type_label(a["group"]), "value": data.num(a["value_eur"]),
                   "color": type_colors.get(a["group"], ui.SERIES[4])} for a in by_type if a["value_eur"] > 0]
    allocation = dmc.Group(
        [
            dmc.DonutChart(data=donut_data, size=168, thickness=22, paddingAngle=2, strokeWidth=0,
                           withTooltip=False),
            dmc.Stack(
                [dmc.Group([html.Span(className="swatch", style={"background": type_colors.get(a["group"], ui.SERIES[4])}),
                            dmc.Text(ui.asset_type_label(a["group"]), size="sm", w=64),
                            dmc.Text(ui.pct(a["weight_pct"]), size="sm", className="num", fw=600)],
                           gap=8, wrap="nowrap") for a in by_type],
                gap=8,
            ),
        ],
        gap="xl", justify="center",
    )

    return dmc.Stack(
        [
            dmc.Stack([hero, ui.legend([("Value", ui.SERIES[0]), (invested_label, ui.SERIES[1])]), chart], gap="md"),
            ui.panel(bridge(parts, gain), title="Where the gain came from",
                     description="Each line adds to the total gain since the first deposit."
                     if not holdings_view else "Each line adds to the gain of the selected holdings."),
            dmc.SimpleGrid(
                [ui.panel(holdings_table, title="Largest holdings",
                          right=dmc.Anchor("All holdings", href="/holdings", size="sm")),
                 ui.panel(allocation, title="By asset type",
                          right=dmc.Anchor("Allocation", href="/allocation", size="sm"))],
                cols={"base": 1, "lg": 2}, spacing="lg",
            ),
        ],
        gap="xl",
    )

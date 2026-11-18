import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/allocation", name="Allocation", order=3)

GROUPS = {"asset_type": "Asset type", "currency": "Currency", "account": "Account", "ticker": "Holding"}

layout = html.Div(
    [
        ui.page_header(
            "Allocation",
            "How the latest value splits up. Currency is the currency each holding is priced in.",
            right=dmc.Group(
                [dmc.Switch(id="allocation-cash", label="Include cash", checked=True, size="sm",
                            persistence=True, persistence_type="local"),
                 dmc.SegmentedControl(id="allocation-by", value="asset_type",
                                      data=[{"value": k, "label": v} for k, v in GROUPS.items()],
                                      persistence=True, persistence_type="local")],
                gap="lg",
            ),
        ),
        html.Div(id="allocation-content"),
    ]
)


def labels_and_colors(connection, by):
    """Each group keeps its colour whatever the filter, so a slice never changes colour."""
    if by == "account":
        names = data.accounts(connection)
        return ({str(k): v for k, v in names.items()},
                {str(k): ui.SERIES[(k - 1) % len(ui.SERIES)] for k in names})
    if by == "currency":
        currencies = [r[0] for r in connection.execute(
            "SELECT DISTINCT currency FROM daily_valuation WHERE currency IS NOT NULL ORDER BY currency").fetchall()]
        return {c: c for c in currencies}, {c: ui.SERIES[i % len(ui.SERIES)] for i, c in enumerate(currencies)}
    types = sorted({t for _, t, _ in data.instruments(connection).values()}) + ["cash"]
    return {t: ui.asset_type_label(t) for t in types}, {t: ui.SERIES[i % len(ui.SERIES)] for i, t in enumerate(types)}


@callback(Output("allocation-content", "children"), *ui.FILTERS,
          Input("allocation-by", "value"), Input("allocation-cash", "checked"))
@ui.guarded
def render(accounts, asset_types, tickers, by, include_cash):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()

    with data.connect() as connection:
        rows = portfolio.allocation(connection, account_ids, by=by, asset_types=asset_types, tickers=tickers,
                                    include_cash=include_cash)
        names = data.instruments(connection)
        if by != "ticker":
            labels, colors = labels_and_colors(connection, by)

    rows = [r for r in rows if r["value_eur"] != 0]
    if not rows:
        return ui.empty("Nothing to allocate in this selection")

    if by == "ticker":
        # Too many holdings for distinct colours: ranked bars in one colour instead of a donut.
        largest = rows[0]["value_eur"]
        return ui.panel(
            dmc.Stack(
                [dmc.Group(
                    [dmc.Stack([dmc.Text(names[r["group"]][0] if r["group"] in names else "Cash", size="sm", truncate=True),
                                dmc.Text(r["group"] if r["group"] in names else "All currencies", size="xs", c="dimmed")],
                               gap=0, w={"base": 140, "sm": 260}),
                     dmc.Progress(value=float(r["value_eur"] / largest * 100), color=ui.SERIES[0], size="lg",
                                  radius="sm", style={"flex": 1}),
                     dmc.Text(ui.eur(r["value_eur"]), size="sm", className="num", w=96, ta="right"),
                     dmc.Text(ui.pct(r["weight_pct"]), size="sm", className="num", w=56, ta="right", fw=600)],
                    gap="md", wrap="nowrap")
                 for r in rows],
                gap="sm",
            ),
            title="By holding", description="Bar length is relative to the largest holding.",
        )

    group = lambda r: str(r["group"])
    donut = dmc.DonutChart(
        data=[{"name": labels.get(group(r), group(r)), "value": data.num(r["value_eur"]),
               "color": colors.get(group(r), ui.SERIES[-1])} for r in rows if r["value_eur"] > 0],
        size=220, thickness=28, paddingAngle=2, strokeWidth=0, withTooltip=False,
        chartLabel=ui.eur(sum(r["value_eur"] for r in rows), decimals=0),
    )
    breakdown = ui.table(
        [GROUPS[by], "Value", "Weight"],
        [[dmc.Group([html.Span(className="swatch", style={"background": colors.get(group(r), ui.SERIES[-1])}),
                     dmc.Text(labels.get(group(r), group(r)), size="sm")], gap=8, wrap="nowrap"),
          ui.eur(r["value_eur"]),
          ui.weight_bar(r["weight_pct"], color=colors.get(group(r), ui.SERIES[-1]))] for r in rows],
        numeric=(1,),
    )
    return ui.panel(
        dmc.Grid([dmc.GridCol(dmc.Center(donut), span={"base": 12, "md": 4}),
                  dmc.GridCol(breakdown, span={"base": 12, "md": 8})], gutter="xl", align="center"),
        title=f"By {GROUPS[by].lower()}",
    )

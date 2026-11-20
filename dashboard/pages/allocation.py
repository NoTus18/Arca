import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, ctx, dcc, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data

dash.register_page(__name__, path="/allocation", name="Allocation", order=3)

HELD = {"asset_type": "Asset type", "currency": "Currency", "account": "Account", "ticker": "Holding"}
LOOK_THROUGH = {"region": "Region", "country": "Country", "market": "Market", "sector": "Sector"}
UNLISTED = "Weight the fund factsheets don't break down."
GROUPS = {**HELD, **LOOK_THROUGH}


def segments(id, groups):
    return dmc.SegmentedControl(id=id, data=[{"value": k, "label": v} for k, v in groups.items()], size="sm")


layout = html.Div(
    [
        dcc.Store(id="allocation-by", storage_type="local", data="asset_type"),
        ui.page_header(
            "Allocation",
            "How the latest value splits up. Currency is the currency each holding is priced in. "
            "Region, country, market and sector look through funds to what they hold. "
            "Regions are geographic; market is MSCI's developed or emerging.",
            right=dmc.Stack(
                [segments("allocation-held", HELD),
                 dmc.Group([segments("allocation-look", LOOK_THROUGH),
                            dmc.Switch(id="allocation-cash", label="Include cash", checked=True, size="sm",
                                       persistence=True, persistence_type="local")],
                           gap="lg")],
                gap="xs", align="flex-start",
            ),
        ),
        html.Div(id="allocation-content"),
    ]
)


@callback(Output("allocation-held", "value"), Output("allocation-look", "value"), Output("allocation-by", "data"),
          Output("allocation-cash", "disabled"),
          Input("allocation-held", "value"), Input("allocation-look", "value"), Input("allocation-by", "data"))
def pick_group(held, look, stored):
    """One group across both rows: picking in one clears the other."""
    by = {"allocation-held": held, "allocation-look": look}.get(ctx.triggered_id) or stored
    by = by if by in GROUPS else "asset_type"
    return (by if by in HELD else None, by if by in LOOK_THROUGH else None, by, by in LOOK_THROUGH)


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
    if by in portfolio.ROLL_UPS:
        column = portfolio.ROLL_UPS[by]
        names = list(dict.fromkeys(v[column] for v in portfolio.load_countries().values()))
        return ({n: n for n in names} | {portfolio.UNCLASSIFIED: portfolio.UNCLASSIFIED},
                {n: ui.SERIES[i % len(ui.SERIES)] for i, n in enumerate(names)} | {portfolio.UNCLASSIFIED: ui.UNCLASSIFIED})
    if by == "sector":
        # 11 sectors for 8 colours, so some repeat; the table labels tell them apart.
        return (dict(ui.SECTOR_LABELS) | {portfolio.UNCLASSIFIED: portfolio.UNCLASSIFIED},
                {s: ui.SERIES[i % len(ui.SERIES)] for i, s in enumerate(ui.SECTOR_LABELS)}
                | {portfolio.UNCLASSIFIED: ui.UNCLASSIFIED})
    types = sorted({t for _, t, _ in data.instruments(connection).values()}) + ["cash"]
    return {t: ui.asset_type_label(t) for t in types}, {t: ui.SERIES[i % len(ui.SERIES)] for i, t in enumerate(types)}


def with_sources(row, child):
    """Hover a looked-through group to see which holdings its value comes from."""
    if "by_ticker" not in row:
        return child
    sources = " · ".join(f"{ticker} {ui.eur(value)}" for ticker, value in row["by_ticker"])
    label = f"{UNLISTED} From: {sources}" if row["group"] == portfolio.UNCLASSIFIED else f"From: {sources}"
    # The wrapper fills the row, or the flex Progress bar inside it shrinks to nothing.
    return dmc.Tooltip(dmc.Box(child), label=label, multiline=True, w=320, withArrow=True, openDelay=150,
                       boxWrapperProps={"w": "100%"})


def ranked_bars(rows, label, sublabel):
    """Too many groups for distinct colours: ranked bars in one colour instead of a donut."""
    largest = max(r["value_eur"] for r in rows)
    return dmc.Stack(
        [with_sources(r, dmc.Group(
            [dmc.Stack([dmc.Text(label(r), size="sm", truncate=True), dmc.Text(sublabel(r), size="xs", c="dimmed")],
                       gap=0, w={"base": 140, "sm": 260}),
             dmc.Progress(value=float(r["value_eur"] / largest * 100), size="lg", radius="sm", style={"flex": 1},
                          color=ui.UNCLASSIFIED if r["group"] == portfolio.UNCLASSIFIED else ui.SERIES[0]),
             dmc.Text(ui.eur(r["value_eur"]), size="sm", className="num", w=96, ta="right"),
             dmc.Text(ui.pct(r["weight_pct"]), size="sm", className="num", w=56, ta="right", fw=600)],
            gap="md", wrap="nowrap"))
         for r in rows],
        gap="sm",
    )


def country_sublabel(country):
    if country == portfolio.UNCLASSIFIED:
        return "Not broken down by the factsheets"
    region, market = portfolio.load_countries().get(country, ("", ""))
    return f"{region} · {market}" if region else ""


def left_out(connection, account_ids, asset_types, tickers, kind):
    """A note on the holdings that have no weights of this kind, or None when every holding has them."""
    covered = data.tickers_with_exposures(connection, kind)
    missing = [r for r in portfolio.allocation(connection, account_ids, by="ticker", asset_types=asset_types,
                                               tickers=tickers, include_cash=False)
               if r["group"] not in covered and r["value_eur"] != 0]
    if not missing:
        return None
    return dmc.Text(f"Leaves out {ui.eur(sum(r['value_eur'] for r in missing))} with no {kind} weights: "
                    f"{', '.join(r['group'] for r in missing)}", size="xs", c="dimmed")


@callback(Output("allocation-content", "children"), *ui.FILTERS,
          Input("allocation-by", "data"), Input("allocation-cash", "checked"))
@ui.guarded
def render(accounts, asset_types, tickers, by, include_cash):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()
    by = by if by in GROUPS else "asset_type"
    kind = portfolio.EXPOSURE_KINDS.get(by)

    with data.connect() as connection:
        rows = portfolio.allocation(connection, account_ids, by=by, asset_types=asset_types, tickers=tickers,
                                    include_cash=include_cash)
        names = data.instruments(connection)
        if by not in ("ticker", "country"):
            labels, colors = labels_and_colors(connection, by)
        note = kind and left_out(connection, account_ids, asset_types, tickers, kind)

    rows = [r for r in rows if r["value_eur"] != 0]
    if not rows:
        if kind:
            return ui.empty(f"No {kind} weights for this selection", "Run python update_all.py to fetch them.")
        return ui.empty("Nothing to allocate in this selection")

    title = f"By {GROUPS[by].lower()}"
    description = (f"Spread over each holding's {kind} weights. Cash is left out. "
                   "Hover a row to see which holdings it comes from." if kind else None)

    if by == "ticker":
        return ui.panel(
            ranked_bars(rows, lambda r: names[r["group"]][0] if r["group"] in names else "Cash",
                        lambda r: r["group"] if r["group"] in names else "All currencies"),
            title=title, description="Bar length is relative to the largest holding.",
        )
    if by == "country":
        return ui.panel(dmc.Stack([note, ranked_bars(rows, lambda r: r["group"], lambda r: country_sublabel(r["group"]))],
                                  gap="md"),
                        title=title, description=f"{description} Bar length is relative to the largest country.")

    group = lambda r: str(r["group"])
    name = lambda r: labels.get(group(r), ui.sector_label(group(r)) if by == "sector" else group(r))
    donut = dmc.DonutChart(
        data=[{"name": name(r), "value": data.num(r["value_eur"]),
               "color": colors.get(group(r), ui.SERIES[-1])} for r in rows if r["value_eur"] > 0],
        size=220, thickness=28, paddingAngle=2, strokeWidth=0, withTooltip=False,
        chartLabel=ui.eur(sum(r["value_eur"] for r in rows), decimals=0),
    )
    breakdown = ui.table(
        [GROUPS[by], "Value", "Weight"],
        [[with_sources(r, dmc.Group([html.Span(className="swatch", style={"background": colors.get(group(r), ui.SERIES[-1])}),
                                     dmc.Text(name(r), size="sm")], gap=8, wrap="nowrap")),
          ui.eur(r["value_eur"]),
          ui.weight_bar(r["weight_pct"], color=colors.get(group(r), ui.SERIES[-1]))] for r in rows],
        numeric=(1,),
    )
    return ui.panel(
        dmc.Stack([note, dmc.Grid([dmc.GridCol(dmc.Center(donut), span={"base": 12, "md": 4}),
                                   dmc.GridCol(breakdown, span={"base": 12, "md": 8})], gutter="xl", align="center")],
                  gap="md"),
        title=title, description=description,
    )

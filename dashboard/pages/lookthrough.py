from decimal import Decimal

import dash
import dash_mantine_components as dmc
from dash import Input, Output, callback, html

from analysis import portfolio
from dashboard import components as ui
from dashboard import data
from dashboard.pages.allocation import ranked_bars

dash.register_page(__name__, path="/look-through", name="Look-through", order=5, group="Analysis")

GROUPS = {"region": "Region", "country": "Country", "market": "Market", "sector": "Sector", "company": "Company"}
UNLISTED = "Weight the fund factsheets don't break down."
TOP_COMPANIES = 20
DIRECT, VIA_ETFS = ui.SERIES[0], ui.SERIES[2]

layout = html.Div(
    [
        ui.page_header(
            "Look-through",
            "Where the money really is: funds are opened up into the countries, sectors and companies they hold, "
            "and added to your direct holdings. Regions are geographic; market is MSCI's developed or emerging. Cash is "
            "left out.",
            right=dmc.SegmentedControl(id="lookthrough-by", value="region",
                                       data=[{"value": k, "label": v} for k, v in GROUPS.items()],
                                       persistence=True, persistence_type="local"),
        ),
        html.Div(id="lookthrough-content"),
    ]
)


def labels_and_colors(by):
    """Each group keeps its colour whatever the filter, so a slice never changes colour."""
    if by in portfolio.ROLL_UPS:
        column = portfolio.ROLL_UPS[by]
        names = list(dict.fromkeys(v[column] for v in portfolio.load_countries().values()))
        return ({n: n for n in names} | {portfolio.UNCLASSIFIED: portfolio.UNCLASSIFIED},
                {n: ui.SERIES[i % len(ui.SERIES)] for i, n in enumerate(names)} | {portfolio.UNCLASSIFIED: ui.UNCLASSIFIED})
    # 11 sectors for 8 colours, so some repeat; the table labels tell them apart.
    return (dict(ui.SECTOR_LABELS) | {portfolio.UNCLASSIFIED: portfolio.UNCLASSIFIED},
            {s: ui.SERIES[i % len(ui.SERIES)] for i, s in enumerate(ui.SECTOR_LABELS)}
            | {portfolio.UNCLASSIFIED: ui.UNCLASSIFIED})


def with_sources(row, child):
    """Hover a group to see which holdings its value comes from."""
    sources = " · ".join(f"{ticker} {ui.eur(value)}" for ticker, value in row["by_ticker"])
    label = f"{UNLISTED} From: {sources}" if row["group"] == portfolio.UNCLASSIFIED else f"From: {sources}"
    # The wrapper fills the row, or the flex Progress bar inside it shrinks to nothing.
    return dmc.Tooltip(dmc.Box(child), label=label, multiline=True, w=320, withArrow=True, openDelay=150,
                       boxWrapperProps={"w": "100%"})


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


CONCENTRATION_HELP = {
    "countries": "Unclassified is left out and the rest rescaled, because it could hide any country.",
    "sectors": "Unclassified is left out and the rest rescaled, because it could hide any sector.",
    "companies": "Each fund's unlisted rest counts as many tiny positions: it's in the total but adds almost "
                 "nothing to concentration, so it raises the number, never lowers it.",
}


def concentration(values, noun, exclude=(), diffuse=()):
    """'3.7 effective sectors of 11': how many equal-sized groups the money behaves like."""
    n = portfolio.effective_number(values, exclude=exclude, diffuse=diffuse)
    if n is None:
        return None
    count = sum(1 for k in values if k not in exclude and k not in diffuse)
    total = sum(values.values(), Decimal(0))
    left_out = sum((values[k] for k in exclude if k in values), Decimal(0))
    spread_out = sum((values[k] for k in diffuse if k in values), Decimal(0))
    detail = f"effective {noun} of {count}"
    if left_out and total:
        detail += f", excluding {ui.pct(left_out / total * 100)} unclassified"
    if spread_out and total:
        # The funds' unlisted rest is hundreds of positions, so "of 34 listed" is no upper limit.
        detail = f"effective {noun}: {count} listed, plus {ui.pct(spread_out / total * 100)} spread over funds' unlisted holdings"
    return ui.explained(
        ui.stat("Concentration", f"{n:.1f}", dmc.Text(detail, size="xs", c="dimmed")),
        f"How many equal-sized {noun} your money behaves like: 1 ÷ the sum of each one's squared share. "
        f"{count} equal {noun} would give {count}; one large and many small ones give close to 1. "
        f"{CONCENTRATION_HELP[noun]}",
    )


def company_bar(label, sublabel, direct, via, total, weight_pct, largest, sources, muted=False):
    """One company: a bar split into what you hold directly and what comes through funds."""
    bar = dmc.ProgressRoot(
        [dmc.ProgressSection(value=float(direct / largest * 100), color=DIRECT),
         dmc.ProgressSection(value=float(via / largest * 100), color=ui.UNCLASSIFIED if muted else VIA_ETFS)],
        size="lg", radius="sm", style={"flex": 1},
    )
    row = dmc.Group(
        [dmc.Stack([dmc.Text(label, size="sm", truncate=True), dmc.Text(sublabel, size="xs", c="dimmed")],
                   gap=0, w={"base": 140, "sm": 260}),
         bar,
         dmc.Text(ui.eur(total), size="sm", className="num", w=96, ta="right"),
         dmc.Text(ui.pct(weight_pct), size="sm", className="num", w=56, ta="right", fw=600)],
        gap="md", wrap="nowrap",
    )
    return dmc.Tooltip(dmc.Box(row), label=f"From: {' · '.join(f'{t} {ui.eur(v)}' for t, v in sources)}",
                       multiline=True, w=320, withArrow=True, openDelay=150, boxWrapperProps={"w": "100%"})


def render_companies(connection, account_ids, asset_types, tickers):
    rows = [r for r in portfolio.lookthrough_companies(connection, account_ids, asset_types, tickers)
            if r["total_eur"] != 0]
    if not rows:
        return ui.empty("Nothing to look through in this selection")

    companies = [r for r in rows if not r["is_rest"]]
    top, others = companies[:TOP_COMPANIES], companies[TOP_COMPANIES:]
    rest = [r for r in rows if r["is_rest"]]

    def grouped(label, sublabel, group):
        """Several rows as one grey line, with the largest parts on hover."""
        return {"company": label, "sublabel": sublabel, "direct_eur": Decimal(0),
                "via_etfs_eur": sum((r["total_eur"] for r in group), Decimal(0)),
                "total_eur": sum((r["total_eur"] for r in group), Decimal(0)),
                "weight_pct": sum((r["weight_pct"] for r in group), Decimal(0)),
                "sources": sorted(((r["company"], r["total_eur"]) for r in group), key=lambda s: s[1], reverse=True)[:12],
                "muted": True}

    greys = ([grouped("Other companies", f"{len(others)} more, each smaller", others)] if others else []) + \
            ([grouped("Rest of funds (not listed)", "Beyond each fund's top 10 holdings", rest)] if rest else [])
    # One scale for every bar, grey rows included, so the unlisted rest looks as big as it is.
    largest = max(r["total_eur"] for r in top + greys)

    def split(r):
        return "Direct and via funds" if r["direct_eur"] and r["via_etfs_eur"] else (
            "Held directly" if r["direct_eur"] else "Via funds")

    bars = [company_bar(r["company"], r.get("sublabel") or split(r), r["direct_eur"], r["via_etfs_eur"],
                        r["total_eur"], r["weight_pct"], largest, r["sources"], muted=r.get("muted", False))
            for r in top + greys]

    spread = concentration({r["company"]: r["total_eur"] for r in rows}, "companies",
                           diffuse=tuple(r["company"] for r in rest))
    return ui.panel(
        dmc.Stack([spread, ui.legend([("Held directly", DIRECT), ("Via funds", VIA_ETFS)]), dmc.Stack(bars, gap="sm")],
                  gap="md"),
        title="By company",
        description=f"Your {TOP_COMPANIES} largest companies, with funds opened up into their top 10 holdings. "
                    "Hover a row to see which holdings it comes from.",
    )


@callback(Output("lookthrough-content", "children"), *ui.FILTERS, Input("lookthrough-by", "value"))
@ui.guarded
def render(accounts, asset_types, tickers, by):
    account_ids, asset_types, tickers = data.selection(accounts, asset_types, tickers)
    if not account_ids:
        return ui.no_accounts()
    by = by if by in GROUPS else "region"
    if by == "company":
        with data.connect() as connection:
            return render_companies(connection, account_ids, asset_types, tickers)
    kind = portfolio.EXPOSURE_KINDS[by]

    with data.connect() as connection:
        rows = portfolio.allocation(connection, account_ids, by=by, asset_types=asset_types, tickers=tickers)
        note = left_out(connection, account_ids, asset_types, tickers, kind)

    rows = [r for r in rows if r["value_eur"] != 0]
    if not rows:
        return ui.empty(f"No {kind} weights for this selection", "Run python update_all.py to fetch them.")

    title = f"By {GROUPS[by].lower()}"
    description = f"Spread over each holding's {kind} weights. Hover a row to see which holdings it comes from."
    # Region and market have too few groups for the number to say much.
    spread = None
    if by in ("country", "sector"):
        spread = concentration({r["group"]: r["value_eur"] for r in rows}, "countries" if by == "country" else "sectors",
                               exclude=(portfolio.UNCLASSIFIED,))

    if by == "country":
        bars = ranked_bars(rows, lambda r: r["group"], lambda r: country_sublabel(r["group"]),
                           wrap=with_sources, muted=lambda r: r["group"] == portfolio.UNCLASSIFIED)
        return ui.panel(dmc.Stack([spread, note, bars], gap="md"), title=title,
                        description=f"{description} Bar length is relative to the largest country.")

    labels, colors = labels_and_colors(by)
    group = lambda r: str(r["group"])
    name = lambda r: labels.get(group(r), ui.sector_label(group(r)) if by == "sector" else group(r))
    color = lambda r: colors.get(group(r), ui.SERIES[-1])
    donut = dmc.DonutChart(
        data=[{"name": name(r), "value": data.num(r["value_eur"]), "color": color(r)} for r in rows if r["value_eur"] > 0],
        size=220, thickness=28, paddingAngle=2, strokeWidth=0, withTooltip=False,
        chartLabel=ui.eur(sum(r["value_eur"] for r in rows), decimals=0),
    )
    breakdown = ui.table(
        [GROUPS[by], "Value", "Weight"],
        [[with_sources(r, dmc.Group([html.Span(className="swatch", style={"background": color(r)}),
                                     dmc.Text(name(r), size="sm")], gap=8, wrap="nowrap")),
          ui.eur(r["value_eur"]),
          ui.weight_bar(r["weight_pct"], color=color(r))] for r in rows],
        numeric=(1,),
    )
    return ui.panel(
        dmc.Stack([spread, note, dmc.Grid([dmc.GridCol(dmc.Center(donut), span={"base": 12, "md": 4}),
                                   dmc.GridCol(breakdown, span={"base": 12, "md": 8})], gutter="xl", align="center")],
                  gap="md"),
        title=title, description=description,
    )

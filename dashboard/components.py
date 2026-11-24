"""Building blocks shared by every page."""

import functools
import logging

import dash_mantine_components as dmc
import duckdb
from dash import Input, html

log = logging.getLogger(__name__)

# Categorical slots in fixed order (validated palette, see assets/style.css for the dark steps).
SERIES = [f"var(--arca-series-{n})" for n in range(1, 9)]
GOOD = "var(--arca-good)"
BAD = "var(--arca-bad)"
UNCLASSIFIED = "var(--arca-unclassified)"  # grey: weight no source lists, not a real group

ASSET_TYPE_LABELS = {"etf": "ETFs", "stock": "Stocks", "reit": "REITs", "cash": "Cash"}

# Sector keys as exposures stores them. The order fixes each sector's colour.
SECTOR_LABELS = {
    "technology": "Technology", "financial_services": "Financial services", "healthcare": "Healthcare",
    "consumer_cyclical": "Consumer cyclical", "communication_services": "Communication services",
    "industrials": "Industrials", "consumer_defensive": "Consumer defensive", "energy": "Energy",
    "basic_materials": "Basic materials", "utilities": "Utilities", "realestate": "Real estate",
}


def eur(value, signed=False, decimals=2):
    if value is None:
        return "–"
    sign = "−" if value < 0 else ("+" if signed and value > 0 else "")
    return f"{sign}€{abs(value):,.{decimals}f}"


def pct(value, signed=False, decimals=1):
    if value is None:
        return "–"
    sign = "−" if value < 0 else ("+" if signed and value > 0 else "")
    return f"{sign}{abs(value):,.{decimals}f}%"


def quantity(value):
    # Fractional shares: keep up to 4 decimals, drop trailing zeros.
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def signed_text(text, value, className="", **props):
    """Gain/loss text: the sign carries the meaning, the colour repeats it."""
    tone = "flat" if not value else ("up" if value > 0 else "down")
    return dmc.Text(text, className=f"num tone-{tone} {className}", span=True, **props)


def asset_type_label(asset_type):
    return ASSET_TYPE_LABELS.get(asset_type, str(asset_type).title())


def sector_label(sector):
    return SECTOR_LABELS.get(sector, str(sector).replace("_", " ").capitalize())


def page_header(title, description=None, right=None):
    return dmc.Group(
        [
            dmc.Stack([dmc.Title(title, order=1, className="page-title"),
                       description and dmc.Text(description, c="dimmed", size="sm", maw=640)], gap=4),
            right,
        ],
        justify="space-between",
        align="flex-end",
        mb="lg",
        wrap="wrap",
    )


def panel(children, title=None, description=None, right=None, **props):
    head = None
    if title or right:
        head = dmc.Group(
            [dmc.Stack([title and dmc.Title(title, order=3, className="panel-title"),
                        description and dmc.Text(description, c="dimmed", size="xs")], gap=2),
             right],
            justify="space-between",
            align="flex-start",
            mb="md",
            wrap="wrap",
        )
    return dmc.Paper([head, children], p="lg", radius="md", withBorder=True, className="panel", **props)


def stat(label, value, detail=None):
    return dmc.Stack(
        [dmc.Text(label, size="xs", c="dimmed"),
         value if not isinstance(value, str) else dmc.Text(value, className="num stat-value"),
         detail],
        gap=2,
        className="stat",
    )


def explained(stat_card, meaning):
    """A stat with help on hover or keyboard focus; its label gets a dotted underline. `meaning` is text or components."""
    body = [dmc.Text(meaning, size="sm")] if isinstance(meaning, str) else meaning
    return dmc.HoverCard(
        [dmc.HoverCardTarget(html.Div(stat_card, className="has-breakdown", tabIndex=0)),
         dmc.HoverCardDropdown(dmc.Stack(body, gap=6))],
        width=300, shadow="md", position="bottom-start", withArrow=True, openDelay=100,
    )


def weight_bar(weight_pct, color=SERIES[0]):
    return dmc.Group(
        [dmc.Progress(value=float(weight_pct), color=color, size="sm", w=72, radius="xl"),
         dmc.Text(pct(weight_pct), size="sm", className="num", w=48, ta="right")],
        gap="xs",
        wrap="nowrap",
    )


def table(head, rows, numeric=(), foot=None, min_width=600, **props):
    """A plain Mantine table. `numeric` lists the column indexes to right-align."""
    def cell(tag, index, content):
        return tag(content, className="num" if index in numeric else None,
                   style={"textAlign": "right"} if index in numeric else None)

    parts = [
        dmc.TableThead(dmc.TableTr([cell(dmc.TableTh, i, h) for i, h in enumerate(head)])),
        dmc.TableTbody([dmc.TableTr([cell(dmc.TableTd, i, c) for i, c in enumerate(row)]) for row in rows]),
    ]
    if foot:
        parts.append(dmc.TableTfoot(dmc.TableTr([cell(dmc.TableTh, i, c) for i, c in enumerate(foot)])))
    return dmc.TableScrollContainer(
        dmc.Table(parts, highlightOnHover=True, verticalSpacing="xs", className="arca-table", **props),
        minWidth=min_width,  # below this the table scrolls sideways instead of squeezing cells
    )


def empty(message, hint=None):
    return dmc.Stack(
        [dmc.Text(message, fw=600), hint and dmc.Text(hint, c="dimmed", size="sm")],
        gap=4, align="center", py="xl", className="empty",
    )


def alert(title, message):
    return dmc.Alert(message, title=title, color="red", variant="light", radius="md")


def error_message(error):
    if isinstance(error, duckdb.IOException):
        return alert(
            "The database is busy",
            f"Another program has it open for writing, usually update_all.py. Reload when it has finished. ({error})",
        )
    log.exception("Rendering a page failed", exc_info=error)
    return alert("This page couldn't be calculated", f"{type(error).__name__}: {error}")


def guarded(render):
    """Turn a failed calculation into a message on the page instead of a broken callback."""
    @functools.wraps(render)
    def wrapper(*args):
        try:
            return render(*args)
        except Exception as error:
            return error_message(error)
    return wrapper


def no_accounts():
    return empty("No accounts selected", "Pick at least one account in the sidebar.")


def time_chart(rows, series, kind=dmc.AreaChart, h=280, **overrides):
    """A chart over dates (rows keyed by an ISO "date"), in EUR unless overridden."""
    props = dict(
        h=h, data=rows, dataKey="date", series=series,
        curveType="linear", withDots=False, strokeWidth=2,
        gridAxis="y", tickLine="none",
        valueFormatter={"function": "eurCents"},
        xAxisProps={"tickFormatter": {"function": "monthYear"}, "minTickGap": 56},
        yAxisProps={"tickFormatter": {"function": "eurCompact"}, "width": 64},
        tooltipAnimationDuration=0,
    )
    if kind is dmc.AreaChart:
        props["fillOpacity"] = 0.12
    props.update(overrides)
    return kind(**props)


# The sidebar filters (built in app.py) that every page callback listens to.
FILTERS = [Input("f-accounts", "value"), Input("f-asset-types", "value"), Input("f-tickers", "value")]


def legend(items):
    """Legend for >= 2 series: a colour swatch and a text label, so identity isn't colour alone."""
    return dmc.Group(
        [dmc.Group([html.Span(className="swatch", style={"background": color}), dmc.Text(label, size="xs")],
                   gap=6, wrap="nowrap") for label, color in items],
        gap="md",
    )

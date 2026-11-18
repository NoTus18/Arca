// Formatters for DMC charts, used as {"function": "<name>"} props.
var dmcfuncs = window.dashMantineFunctions = window.dashMantineFunctions || {};

const euro = new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
const euroCents = new Intl.NumberFormat("en-IE", { style: "currency", currency: "EUR", minimumFractionDigits: 2 });
const euroCompact = new Intl.NumberFormat("en-IE", {
    style: "currency", currency: "EUR", notation: "compact", maximumFractionDigits: 1,
});

dmcfuncs.eur = (value) => euro.format(value);
dmcfuncs.eurCents = (value) => euroCents.format(value);
dmcfuncs.eurCompact = (value) => euroCompact.format(value);
dmcfuncs.pct = (value) => `${value.toFixed(1)}%`;

// "2026-03-01" -> "Mar 26". Parsed as UTC so the day never shifts with the time zone.
dmcfuncs.monthYear = (iso) => new Date(iso + "T00:00:00Z")
    .toLocaleDateString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
dmcfuncs.dayMonthYear = (iso) => new Date(iso + "T00:00:00Z")
    .toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });

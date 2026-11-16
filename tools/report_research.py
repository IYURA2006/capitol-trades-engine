"""HTML sections for the research checks (input: out/research.json).

Used by tools/make_report.py. Every number comes from tools/run_research.py;
nothing is computed here except formatting.
"""
from __future__ import annotations

import base64
import html
import io
from itertools import pairwise

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
BLUE, ORANGE = "#2a78d6", "#eb6834"
BENCH_NAMES = {"SPY": "S&P 500", "RSP": "Equal-weight S&P 500", "QQQ": "Nasdaq-100", "sector": "Own sector fund"}


def pct(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value * 100:+.{digits}f}%"


def share(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def interval(row: dict, digits: int = 1) -> str:
    if row.get("ci_low") is None:
        return "n/a"
    return f"{pct(row['ci_low'], digits)} to {pct(row['ci_high'], digits)}"


def clear(row: dict) -> str:
    """Plain words for "does the interval leave out zero"."""
    if row.get("ci_low") is None:
        return "too few to say"
    return "no clear difference" if row["ci_low"] <= 0 <= row["ci_high"] else "interval leaves out zero"


def _cell(value) -> str:
    """Counts get a thousands separator; everything else is shown as it is."""
    return f"{value:,}" if isinstance(value, int) and not isinstance(value, bool) else str(value)


def table(headers: list[str], rows: list[list]) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(_cell(c))}</td>" for c in row) + "</tr>" for row in rows)
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _finish(fig) -> str:
    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", facecolor=SURFACE)
    plt.close(fig)
    return base64.b64encode(buffer.getvalue()).decode()


def _axes(figsize=(7.2, 3.8)):
    fig, ax = plt.subplots(figsize=figsize, dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left", "bottom"):
        ax.spines[side].set_visible(False)
    return fig, ax


def copycat_chart(research: dict) -> str:
    hold = research["settings"]["copycat_hold"]
    spy, rsp = research["copycat"][f"{hold}/SPY"], research["copycat"][f"{hold}/RSP"]
    from datetime import date

    days = [date.fromisoformat(d) for d, _, _ in spy["curve"]]
    fig, ax = _axes()
    lines = [("Copycat portfolio", [p * 100 for _, p, _ in spy["curve"]], BLUE, "-", 2.2),
             ("S&P 500", [b * 100 for _, _, b in spy["curve"]], ORANGE, "-", 1.8),
             ("Equal-weight S&P 500", [b * 100 for _, _, b in rsp["curve"]], MUTED, "--", 1.5)]
    for label, values, color, style, width in lines:
        ax.plot(days[: len(values)], values, color=color, linestyle=style, linewidth=width, label=label)
    # Direct labels at the right end, moved apart when two lines end close together.
    ends = sorted(((values[-1], label) for label, values, *_ in lines))
    placed = []
    for value, label in ends:
        y = value if not placed or value - placed[-1] >= 3.0 else placed[-1] + 3.0
        placed.append(y)
        ax.annotate(f"{label} {value:+.0f}%", (days[-1], value), xytext=(8, (y - value) * 2.2),
                    textcoords="offset points", va="center", fontsize=9, color=INK)
    ax.axhline(0, color=MUTED, linewidth=1)
    ax.set_ylabel("Total return since the start, %", color=MUTED, fontsize=9)
    ax.margins(x=0.0)
    ax.set_xlim(days[0], days[-1] + (days[-1] - days[0]) * 0.36)
    # Month ticks only where there is data; the space on the right is for the labels.
    ticks = [d for i, d in enumerate(days) if (i == 0 or d.month != days[i - 1].month) and d.month in (1, 7)]
    ax.set_xticks(ticks, [d.strftime("%b %Y") for d in ticks])
    ax.legend(frameon=False, fontsize=9, loc="upper left", labelcolor=INK)
    fig.tight_layout()
    return _finish(fig)


def random_chart(research: dict) -> str:
    main = research["settings"]["main_window"]
    result = research["random"][f"{main}/by_size"]
    counts, edges = result["histogram"]["counts"], [e * 100 for e in result["histogram"]["edges"]]
    fig, ax = _axes(figsize=(7.2, 3.4))
    ax.bar(edges[:-1], counts, width=[b - a for a, b in pairwise(edges)], align="edge",
           color=ORANGE, edgecolor=SURFACE, linewidth=1, label="1,000 random portfolios")
    real = result["real"] * 100
    ax.axvline(real, color=BLUE, linewidth=2.2, label="Members' real purchases")
    ax.set_ylim(0, max(counts) * 1.25)  # room above the bars for the label
    ax.annotate(f"real: {real:+.1f}%", (real, max(counts) * 1.17), xytext=(6, 0), textcoords="offset points",
                fontsize=9, color=INK, va="center")
    ax.set_xlabel(f"Mean return after {main} trading days, %", color=MUTED, fontsize=9)
    ax.set_ylabel("Random portfolios", color=MUTED, fontsize=9)
    ax.legend(frameon=False, fontsize=9, loc="upper right", labelcolor=INK)
    fig.tight_layout()
    return _finish(fig)


def _difference_rows(result: dict, yes: str, no: str) -> list[list]:
    return [[yes, result["yes"]["n"], result["yes"]["members"], pct(result["yes"]["excess"]), ""],
            [no, result["no"]["n"], result["no"]["members"], pct(result["no"]["excess"]), ""],
            ["Difference", "", "", pct(result["diff"]), f"{interval(result)} ({clear(result)})"]]


def all_tests(research: dict) -> list[list]:
    """Every comparison in this report in one place, so chance findings can be judged."""
    main, hold = research["settings"]["main_window"], research["settings"]["copycat_hold"]
    o = research["overall"]
    tests = [
        ("Purchases against the S&P 500", o[f"buy/trade/{main}/SPY"], "excess"),
        ("Purchases against the equal-weight S&P 500", o[f"buy/trade/{main}/RSP"], "excess"),
        ("Purchases against their own sector fund", o[f"buy/trade/{main}/sector"], "excess"),
        ("Sold stocks against the S&P 500 (below zero = good sale)", o[f"sell/trade/{main}/SPY"], "excess"),
        ("Purchases outside the AI names against the S&P 500", research["ai"][f"rest/{main}/SPY"], "excess"),
        ("Purchases outside the AI names against the equal-weight S&P 500", research["ai"][f"rest/{main}/RSP"], "excess"),
        ("Committee match: matched minus same industry", research["committee"][f"narrow/buy/{main}/sector"], "diff"),
        ("Late minus on-time purchases", research["late"][f"{main}/SPY"], "diff"),
        ("Home-state minus other purchases", research["home"][f"{main}/sector"], "diff"),
        ("Price move in the 2 days after disclosure of a purchase", research["reaction"]["buy/2/SPY"], "excess"),
    ]
    rows = [[name, pct(r[key]), interval(r), clear(r)] for name, r, key in tests]
    c = research["copycat"][f"{hold}/SPY"]
    rows.append([f"Copycat portfolio (hold {hold} days) against the S&P 500, per year", pct(c["excess_per_year"]),
                 interval(c), clear(c)])
    r = research["random"][f"{main}/by_size"]
    rows.append(["Random picker test: share of random portfolios beaten", share(r["percentile"]), "", ""])
    return rows


def sections(research: dict) -> str:
    s = research["settings"]
    main, hold = s["main_window"], s["copycat_hold"]
    o, counts = research["overall"], research["counts"]
    parts: list[str] = []

    # ---- purchases and sales apart ------------------------------------------
    def by_window(side: str) -> list[list]:
        out = []
        for window in s["windows"]:
            r = o[f"{side}/trade/{window}/SPY"]
            out.append([window, r["n"], pct(r["stock"]), pct(r["bench"]), pct(r["excess"]), interval(r),
                        pct(r["median"]), share(r["beat"])])
        return out

    headers = ["Trading days", "Trades", "Stock", "S&P 500", "Difference", "95% interval", "Median difference",
               "Beat the S&P 500"]
    buy_main = o[f"buy/trade/{main}/SPY"]
    parts.append(f"""
<h2>Purchases and sales, apart</h2>
<p>The first table of this report mixes purchases and sales. Here they are apart, counted from the day after the
trade, and for up to {main} trading days (about one year). {counts['buys']:,} purchases and {counts['sells']:,} sales have prices.
Longer windows hold fewer trades, because late trades do not have a year of prices yet.</p>
<h3>Purchases</h3>{table(headers, by_window("buy"))}
<p>After {main} days the bought stocks were up {pct(buy_main['stock'])} on average and the S&amp;P 500 was up
{pct(buy_main['bench'])} over the same days. Only {share(buy_main['beat'])} of the purchases beat the index:
the average is held up by a small number of large winners.</p>
<h3>Sales</h3>
<p class="note">Here a difference <b>below</b> zero is a good sale: the stock did worse than the market after it was sold.</p>
{table(headers, by_window("sell"))}""")

    # ---- benchmarks -----------------------------------------------------------
    bench_rows = []
    for bench in ("SPY", "RSP", "QQQ", "sector"):
        r = o[f"buy/trade/{main}/{bench}"]
        bench_rows.append([BENCH_NAMES[bench], r["n"], pct(r["bench"]), pct(r["excess"]), interval(r), share(r["beat"])])
    ai = research["ai"]
    ai_rows = []
    for label, name in (("ai", "AI names"), ("rest", "All other purchases")):
        spy, rsp = ai[f"{label}/{main}/SPY"], ai[f"{label}/{main}/RSP"]
        ai_rows.append([name, spy["n"], pct(spy["stock"]), pct(spy["excess"]), interval(spy), pct(rsp["excess"]), interval(rsp)])
    parts.append(f"""
<h2>The answer depends on the benchmark</h2>
<p>In 2024 and 2025 a few very large technology companies lifted the S&amp;P 500. The equal-weight version of the
index gives every company the same weight, so it shows what a typical large company did. Purchases after {main} trading days:</p>
{table(["Compared with", "Purchases", "Benchmark return", "Difference", "95% interval", "Beat it"], bench_rows)}
<h3>AI names and the rest</h3>
<p>{share(ai['share_of_buys'])} of the purchases were in a list of 32 AI-related stocks (the largest technology companies and
chip, cloud and power companies). <b>The list was written in November 2026, after these stocks had risen,</b> so its
row shows where the return came from. It is not evidence of skill: many ordinary investors bought the same stocks.</p>
{table(["Group", "Purchases", "Stock", "vs S&P 500", "95% interval", "vs equal-weight", "95% interval"], ai_rows)}""")

    # ---- sectors --------------------------------------------------------------
    sector_rows = []
    for sector, r in sorted(research["sectors"].items(), key=lambda kv: -kv[1]["SPY"]["n"]):
        sector_rows.append([sector, r["SPY"]["n"], share(r["share_of_dollars"]), pct(r["SPY"]["stock"]),
                            pct(r["SPY"]["excess"]), pct(r["sector"]["excess"]), interval(r["sector"])])
    parts.append(f"""
<h2>By sector</h2>
<p>Each purchase after {main} trading days, against the S&amp;P 500 and against a fund of its own sector. "Share of
dollars" uses the middle of each reported amount band, over all purchases.</p>
{table(["Sector", "Purchases", "Share of dollars", "Stock", "vs S&P 500", "vs own sector fund", "95% interval"], sector_rows)}
<p class="note">Eleven sectors are eleven tests. One or two intervals that leave out zero are expected by chance alone.</p>""")

    # ---- committee match ------------------------------------------------------
    cm = research["committee"]
    cc = cm["narrow/buy/counts"]
    matched_share = cc.get("matched", 0) / max(cc["with_member"], 1)
    committee_rows = _difference_rows(cm[f"narrow/buy/{main}/sector"], "Member sits on the committee",
                                      "Same industries, other members")
    sens = [["Main: purchases, own sector fund, " + str(main) + " days", pct(cm[f"narrow/buy/{main}/sector"]["diff"]),
             interval(cm[f"narrow/buy/{main}/sector"])],
            [f"Against the S&P 500, {main} days", pct(cm[f"narrow/buy/{main}/SPY"]["diff"]), interval(cm[f"narrow/buy/{main}/SPY"])],
            ["60 days, own sector fund", pct(cm["narrow/buy/60/sector"]["diff"]), interval(cm["narrow/buy/60/sector"])],
            [f"With the two broad committees added, {main} days", pct(cm[f"broad/buy/{main}/sector"]["diff"]),
             interval(cm[f"broad/buy/{main}/sector"])],
            [f"Sales instead of purchases, {main} days (below zero = good sale)", pct(cm[f"narrow/sell/{main}/sector"]["diff"]),
             interval(cm[f"narrow/sell/{main}/sector"])]]
    parts.append(f"""
<h2>Committee match</h2>
<p>Do members do better in the industries that their own committee oversees? A 2022 study of senators found that
prices rose more after such purchases. Here it is tried on the House. Six committees were linked to industries
<b>before any result was computed</b> (Armed Services and defence companies, Financial Services and banks, and so on).
Each filing was linked to a member by seat and date; {counts['linked_to_a_member']:,} of {counts['trades_with_prices']:,} trades could be linked.</p>
<p>{cc.get('matched', 0)} purchases ({share(matched_share)}) by {cc['members_matched']} members were in an industry
of the member's own committee. They are compared with purchases in the same industries by members who do not sit on
that committee, each against its own sector fund, after {main} trading days.</p>
{table(["Group", "Purchases", "Members", "Mean vs own sector fund", "95% interval of the difference"], committee_rows)}
<h3>How much the answer moves</h3>
{table(["Variant", "Difference", "95% interval"], sens)}
<p class="note">Committee lists are one snapshot per Congress (June 2024 and June 2025). A group result says nothing
about why any single trade was made.</p>""")

    # ---- copycat --------------------------------------------------------------
    cp = research["copycat"]
    c_main = cp[f"{hold}/SPY"]
    copy_rows = []
    for h in (hold, *sorted(int(k.split("/")[0]) for k in cp if not k.startswith(f"{hold}/") and k.endswith("/SPY"))):
        for bench in ("SPY", "RSP"):
            r = cp[f"{h}/{bench}"]
            copy_rows.append([f"{h} days" + (" (main)" if h == hold else ""), BENCH_NAMES[bench], pct(r["total"]),
                              pct(r["bench_total"]), pct(r["excess_per_year"]), interval(r), f"{r['beta']:.2f}",
                              pct(r["alpha_per_year"])])
    parts.append(f"""
<h2>Copycat portfolio</h2>
<p>What if you simply copied them? Every purchase is bought at the close of the first trading day after it became
public, held for {hold} trading days, and all positions have the same weight. From {c_main['first']} to
{c_main['last']} the portfolio held {c_main['avg_positions']:.0f} positions on an average day.</p>
<figure>
 <img alt="Total return of the copycat portfolio, the S&P 500 and the equal-weight S&P 500 over the same days. The end values are in the table below." src="data:image/png;base64,{copycat_chart(research)}">
 <figcaption>Copycat portfolio against two indexes. No trading costs or taxes are taken off.</figcaption>
</figure>
{table(["Holding time", "Compared with", "Portfolio", "Index", "Difference per year", "95% interval", "Beta", "Alpha per year"], copy_rows)}
<p class="note">Beta says how strongly the portfolio moves with the index (1 = the same). Alpha is the return left
after that movement is taken out. The interval is for the simple difference per year and draws blocks of 20 days.</p>""")

    # ---- random picker --------------------------------------------------------
    rp = research["random"]
    random_rows = []
    for window in (60, main):
        for weighting, name in (("by_size", "Large companies picked more often"), ("equal", "Every company equally likely")):
            r = rp[f"{window}/{weighting}"]
            random_rows.append([window, name, r["n"], pct(r["real"]), pct(r["random_mean"]),
                                f"{pct(r['random_low'])} to {pct(r['random_high'])}", share(r["percentile"])])
    m = rp["members"]
    r_main = rp[f"{main}/by_size"]
    n_members = len(m["percentiles"])
    parts.append(f"""
<h2>Skill or luck: the random picker test</h2>
<p>This follows a 2022 paper on Congress trades. Every purchase is replaced by a random stock from the same sector,
bought on the same day and held for the same time. That is done {s['draws']:,} times. If members had skill, their real
result would be better than almost all random portfolios.</p>
<figure>
 <img alt="Histogram of the mean return of 1,000 random portfolios, with a line at the members' real mean return. The numbers are in the table below." src="data:image/png;base64,{random_chart(research)}">
 <figcaption>{main} trading days, random stocks drawn in proportion to company size. The real purchases beat
 {share(r_main['percentile'])} of the random portfolios.</figcaption>
</figure>
{table(["Trading days", "How the random stock is drawn", "Purchases", "Real mean return", "Random mean", "Middle 95% of random", "Random portfolios beaten"], random_rows)}
<p>The same test for each of the {n_members} members with at least {m['min_buys']} purchases (60 days, drawn by size):
{m['above_95']} of them beat 95% of their random portfolios and {m['below_05']} were below the 5% mark. Pure luck
would give about {n_members * 0.05:.0f} in each group.</p>
<p class="note">Two weak points. Members often buy the same stock many times, so their picks are not independent
and the per-member numbers spread out more than luck alone would give, in both directions. And the random stocks come
only from the {rp['universe']:,} stocks that members traded at some time: a stock that rose and was bought later is
in the pool for earlier dates too, which makes the random portfolios a little too good. Company size is today's size
scaled back by the price change since the purchase.</p>""")

    # ---- disclosure reaction ---------------------------------------------------
    re_rows = []
    for side, name in (("buy", "Purchases"), ("sell", "Sales")):
        for window in (1, 2, 5):
            r = research["reaction"][f"{side}/{window}/SPY"]
            r2 = research["reaction"][f"{side}/{window}/sector"]
            re_rows.append([name, window, r["n"], pct(r["excess"], 2), interval(r, 2), pct(r2["excess"], 2), interval(r2, 2)])
    parts.append(f"""
<h2>Does the market react when a trade becomes public?</h2>
<p>The return from the last close on or before the disclosure date to 1, 2 and 5 trading days later, against the
S&amp;P 500 and the stock's sector fund. If investors copied members, purchases would jump here.</p>
{table(["Trades", "Days after", "Count", "vs S&P 500", "95% interval", "vs own sector fund", "95% interval"], re_rows)}""")

    # ---- late filers and home state ---------------------------------------------
    late, home = research["late"], research["home"]
    parts.append(f"""
<h2>Late filers</h2>
<p>The law gives 45 days to report a trade. {share(late['share_late'])} of the purchases were reported later than
that; the median was {late['median_days']:.0f} days. Are the late ones the better ones? Purchases against the
S&amp;P 500 after {main} trading days from the trade date:</p>
{table(["Group", "Purchases", "Members", "Mean vs S&P 500", "95% interval of the difference"],
       _difference_rows(late[f"{main}/SPY"], "Reported late", "Reported in time"))}

<h2>Home-state companies</h2>
<p>A 2013 study found that members did well only with companies from their own district. Here a purchase is "home"
when the company's head office is in the member's state ({share(home['share_home'])} of the {home['buys_with_known_state']:,}
purchases of U.S. companies). Against the own sector fund after {main} trading days:</p>
{table(["Group", "Purchases", "Members", "Mean vs own sector fund", "95% interval of the difference"],
       _difference_rows(home[f"{main}/sector"], "Head office in the member's state", "Head office elsewhere"))}""")

    # ---- all tests ------------------------------------------------------------
    tests = all_tests(research)
    parts.append(f"""
<h2>All comparisons in one table</h2>
<p>{len(tests)} main comparisons were made, each with a window that was fixed before the results were seen
({main} trading days from the trade date; {hold} days for the copycat). With this many, one interval that leaves out
zero can appear by chance. All intervals draw whole members, not single trades.</p>
{table(["Comparison", "Result", "95% interval", "Reading"], tests)}""")
    return "\n".join(parts)


EXTRA_LIMITS = """
 <li><b>One market period.</b> 2024 and 2025 were years of a strong, narrow rise. The same checks in another period could look different.</li>
 <li><b>Hindsight in labels.</b> Sector, industry, head office and company size are today's values, and the AI list was made after the fact.</li>
 <li><b>Committee data is coarse.</b> One list per Congress, six committees, industries chosen by hand.</li>
 <li><b>The copycat ignores costs.</b> No trading costs, taxes or price impact.</li>
"""

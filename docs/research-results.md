# Research results

Numbers from `make research` on the full run (966 filings, 8,682 trades with prices: 4,288 purchases and 4,394 sales). The full report with charts is `out/report.html`. Method and rules: [decisions/007](decisions/007-research-outside-the-engine.md).

All intervals are 95% and draw whole members, not single trades. Main window: 250 trading days from the day after the trade.

## Purchases from the trade date, against the S&P 500

| Trading days | Purchases | Stock | S&P 500 | Difference | Interval | Median difference | Beat the index |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20 | 4,288 | +1.8% | +1.7% | +0.1% | -0.3% to +0.6% | -0.3% | 48% |
| 60 | 4,288 | +5.5% | +5.3% | +0.3% | -0.6% to +1.3% | -1.2% | 46% |
| 120 | 3,796 | +10.4% | +10.8% | -0.4% | -1.8% to +1.2% | -3.1% | 43% |
| 250 | 2,259 | +17.9% | +19.3% | -1.4% | -4.5% to +2.1% | -7.4% | 40% |

## The same purchases against four benchmarks (250 days)

| Compared with | Benchmark return | Difference | Interval |
| --- | --- | --- | --- |
| S&P 500 | +19.3% | -1.4% | -4.5% to +2.1% |
| Equal-weight S&P 500 | +13.1% | +4.8% | +1.5% to +8.3% |
| Nasdaq-100 | +22.8% | -4.8% | -8.2% to -1.6% |
| Own sector fund | +16.4% | +1.6% | -1.0% to +4.4% |

## AI names and the rest (250 days)

19% of purchases were in a list of 32 AI-related stocks. The list was made after these stocks had risen, so it describes the sample and proves nothing about skill.

| Group | Purchases | Stock | vs S&P 500 | Interval | vs equal-weight | Interval |
| --- | --- | --- | --- | --- | --- | --- |
| AI names | 433 | +36.6% | +17.9% | +9.3% to +25.3% | +24.1% | +14.9% to +32.1% |
| All other purchases | 1,826 | +13.5% | -6.0% | -8.9% to -3.1% | +0.2% | -2.6% to +2.8% |

## Committee match

301 of 4,238 linked purchases (7%), by 22 members, were in an industry that the member's own committee oversees. Compared with purchases in the same industries by other members, each against its own sector fund, after 250 days:

| Group | Purchases | Members | Mean vs own sector fund |
| --- | --- | --- | --- |
| Member sits on the committee | 147 | 17 | +1.0% |
| Same industries, other members | 900 | 42 | +2.5% |

Difference: **-1.5%** (interval -6.0% to +3.7%). No clear difference. At 60 days: -0.2% (-3.4% to +3.6%). With two broad committees added: +0.8% (-3.8% to +8.3%).

## Copycat portfolio

Every purchase bought at the close of the first trading day after disclosure, equal weights, no costs.

| Holding time | Compared with | Portfolio | Index | Difference per year | Interval | Beta | Alpha per year |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 60 days (main) | S&P 500 | +33.3% | +42.8% | -3.1% | -8.6% to +4.4% | 0.95 | -2.3% |
| 60 days (main) | Equal-weight S&P 500 | +33.3% | +27.0% | +2.5% | -4.6% to +10.1% | 1.05 | +1.9% |
| 20 days | S&P 500 | +35.4% | +46.3% | -3.8% | -11.9% to +5.0% | 0.96 | -3.0% |
| 20 days | Equal-weight S&P 500 | +35.4% | +28.7% | +2.8% | -6.8% to +12.4% | 1.04 | +2.3% |
| 250 days | S&P 500 | +29.8% | +42.7% | -4.4% | -9.7% to +2.0% | 0.90 | -2.6% |
| 250 days | Equal-weight S&P 500 | +29.8% | +27.6% | +0.9% | -5.4% to +7.1% | 1.00 | +0.9% |

Main portfolio: 2024-01-09 to 2026-03-18, 469 positions on an average day.

## Random picker test

Each purchase replaced by a random stock of the same sector on the same day, 1,000 times.

| Trading days | Random stock drawn | Real mean | Random mean | Middle 95% of random | Random portfolios beaten |
| --- | --- | --- | --- | --- | --- |
| 60 | by company size | +5.6% | +5.6% | +5.1% to +6.0% | 51% |
| 60 | equally | +5.6% | +6.6% | +5.4% to +8.5% | 6% |
| 250 | by company size | +17.9% | +19.2% | +17.7% to +20.9% | 6% |
| 250 | equally | +17.9% | +21.0% | +17.5% to +24.8% | 4% |

Per member (at least 20 purchases, 60 days, by size): 28 members; 3 above the 95% mark and 5 below the 5% mark. The random pool is only the 1,118 stocks members traded, which makes the random side a little too good.

## Reaction to disclosure

From the last close on or before the disclosure date, against the S&P 500.

| Trades | Days after | Difference | Interval |
| --- | --- | --- | --- |
| Purchases | 1 | +0.04% | -0.17% to +0.26% |
| Purchases | 2 | +0.15% | -0.10% to +0.46% |
| Purchases | 5 | +0.30% | +0.02% to +0.78% |
| Sales | 1 | -0.14% | -0.29% to +0.01% |
| Sales | 2 | +0.02% | -0.19% to +0.21% |
| Sales | 5 | -0.13% | -0.38% to +0.10% |

## Late filers

22% of purchases were disclosed more than 45 days after the trade (median 28 days). After 250 days against the S&P 500: late -3.9% (595 purchases), in time -0.5% (1,664). Difference -3.4% (interval -10.2% to +5.2%). At 60 days: -2.3% (-4.1% to -0.8%).

## Home-state companies

6% of the 3,696 purchases of U.S. companies were in a company with its head office in the member's state. After 250 days against the own sector fund: home -0.2% (100 purchases, 25 members), elsewhere +1.8% (1,849). Difference -2.1% (interval -14.6% to +11.3%).

## Reading

- Against the S&P 500 the purchases show no edge, and most single purchases lost to it.
- The benchmark decides the story. The S&P 500 of these years was carried by a few very large companies. Purchases outside the AI names lost about 6% a year to it but matched the equal-weight index.
- Committee seats, late reporting and home-state companies show no clear difference. The groups are small and the intervals are wide, so a real effect of a few percent could hide in them.
- Copying the purchases after disclosure did not beat the S&P 500.
- None of this says anything about a single person or about intent.

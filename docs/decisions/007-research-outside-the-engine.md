# 007: Research checks run outside the engine

**Decision.** The deeper finance checks (benchmarks, sectors, committee match, copycat portfolio, random picker test, disclosure reaction, late filers, home state) are plain functions in `pipeline/research/`. `tools/run_research.py` runs them on the records of a finished run plus frozen reference data in `snapshots/research/`. They are not engine tasks.

**Alternative.** Add them as tasks to the pipeline, so their results become records.

**Why.** The engine's claim is tested on a fixed workload: 100 chaos runs that all end with results hash `f6b51d23ff2f`. New tasks would change the records and the hash, and the 100 runs (about 6 hours) would have to be repeated to say the same thing about the engine. The research needs no retries, no fan-out and no rate limit: it reads files and takes a few seconds. So it gains nothing from the engine, and the engine gains nothing from it.

**Rules fixed before any result was computed.**

- Main window: 250 trading days from the trade date. The copycat portfolio holds for 60 trading days from the disclosure date. Other windows are shown only to see how much the answer moves.
- The committee-to-industry mapping (`pipeline/data/committee_industries.json`) was committed before the first committee result. The git history shows the order.
- Every interval draws whole members, not single trades, because 5 members made 45% of the purchases.
- All main comparisons are listed in one table in the report.
- Group results only. No member is named in any output and nobody is ranked.

**Reference data.** Committee lists and House terms come from the public `unitedstates/congress-legislators` repository, pinned to one commit per Congress. A filing is linked to a member by seat and date, not by name; the name is used only to check the link. Sector, industry, head office and company size come from Yahoo Finance as of the day they were fetched.

**Known weak points.**

- Sector, industry and size labels are today's, not those on the trade date. Company size for the random picker is scaled back by the price change, which removes most of that problem but not share issues or buybacks.
- The random stocks are drawn only from stocks that members traded at some point. A stock that rose and was bought later is in that pool for earlier dates too, so the random portfolios are a little too good.
- The list of "AI names" was written in November 2026, after those stocks had risen. It describes the sample and tests nothing.
- Committee lists are one snapshot per Congress.

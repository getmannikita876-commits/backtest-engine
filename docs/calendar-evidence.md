# Exchange-Calendar Evidence Register

This register is the durable audit trail for every schedule fact encoded in a
shipped calendar definition. Its purpose is that, years from now, an auditor
can answer *"why does QRT believe this date/time had this exchange schedule?"*
from the repository alone — without any conversation history, and without a
live network.

The machine-readable citations live beside the facts themselves, in each
definition file (`src/quant_research_terminal/calendars/definitions/*.toml`,
`[[evidence]]` records keyed by id). This document carries the narrative:
retrieval context, evidence strength, what was deliberately *not* encoded, and
why the supported range ends where it does.

Rules this register operates under:

- A fact is encoded only when official CME Group publications state it.
  Vendor documentation may corroborate; it never overrides CME.
- An unretrievable fact is an **unsupported range**, never an approximation.
  No verification status exists that legitimizes a guessed schedule.
- No copyrighted source document is stored in the repository; citations and
  concise factual claims only.
- Runtime code never fetches or infers a schedule fact. Evidence flows one
  way: source → this register + the definition data → deterministic
  materialization.

## Retrieval context (2026-08-10)

`www.cmegroup.com` blocks automated retrieval from the authoring environment
(HTTP 403 bot protection; direct fetches reset). Every source below is an
official CME Group publication retrieved through Internet Archive snapshots of
its official `cmegroup.com` URL. The archived page or PDF is CME's own
content; each citation records both the original URL and the snapshot
identity, so any of them can be re-verified from either the live site or the
archive.

## Calendar: `CME_EQUITY_INDEX` v1

Schedule class: CME Globex **EQUITIES** product group — the schedule CME's own
holiday tables apply to E-mini S&P 500 (ES) and E-mini Nasdaq-100 (NQ)
futures. Supported trading dates: **2023-05-22 through 2023-12-29**.
Authored timezone: `America/Chicago` (CME states its hours in "U.S. Central
Time"; `America/Chicago` is the canonical IANA zone for U.S. Central Time —
this mapping is an interpretation this register makes explicitly).

### E1. Weekly hours and the daily trading halt — `verified-primary`

> "Sunday – Friday 6:00 p.m. – 5:00 p.m. ET with a trading halt from
> 4:15 p.m. – 4:30 p.m. ET"

i.e. 17:00 → 16:00 CT with a 15:15–15:30 CT halt.

- **Source:** CME Group, *Micro E-mini Equity Index futures FAQ*, Q8 "What are
  the trading hours on CME Globex?" (Micro E-minis follow the E-mini equity
  index schedule; the same FAQ defines their settlement from the E-mini's
  Globex prints).
- Original URL:
  `https://www.cmegroup.com/articles/faqs/micro-e-mini-equity-index-futures-frequently-asked-questions.html`
- Snapshots verified: **2023-12-04** (inside the supported range), 2024-05-23,
  2025-04-19, 2025-06-22 — identical wording in all.
- Bracketing for the range start: the identical statement was already
  published at the FAQ's earlier URL
  (`https://www.cmegroup.com/education/frequently-asked-questions-micro-e-mini-equity-index-futures.html`)
  in the **2021-01-21** snapshot. The rule was in force in January 2021 and in
  December 2023; every 2023 holiday schedule PDF (E2–E7) shows the same
  16:00 close / 16:45 pre-open / 17:00 open pattern in between. No CME notice
  changing equity-index Globex hours during 2023 was found in the holiday
  calendar's own enumeration (E8).
- Definition ids: `cme-micro-emini-faq-2023-12`, `cme-micro-emini-faq-2021-01`.

### E2–E7. 2023 holiday schedules — `verified-primary`

CME publishes a per-holiday *Holiday Schedule* PDF of Globex trading hours by
product group, with explicit **TRADE DATE** labels per window and a state
legend: `PREOPEN (HALT)` = order entry allowed, no matching; `OPEN` =
continuous trading; `CLOSED` = final close of the trade date. All times
"U.S. Central Time". The EQUITIES rows for 2023 (each from
`https://www.cmegroup.com/trading-hours/files/<name>.pdf`):

| # | File / snapshot | EQUITIES facts encoded |
| --- | --- | --- |
| E2 | `memorial-day-2023.pdf` (snap 2023-04-20) | Sun 28 May 16:00 PREOPEN, 17:00 OPEN and Mon 29 May 12:00 PREOPEN-HALT, 17:00 OPEN — all TRADE DATE **TUES 30 MAY**; Tue 16:00 CLOSED; Wed TD pre-opens 16:45. Mon 29 May is not a trade date. |
| E3 | `juneteenth-2023.pdf` (snap 2023-06-13) | Identical shape: Sun 18 Jun / Mon 19 Jun → TRADE DATE TUES 20 JUNE. |
| E4 | `4th-of-july-2023.pdf` (snap 2023-06-27) | Mon 3 Jul: TRADE DATE MON 3 JULY **12:15 CLOSED** (early close); Mon 16:45 PREOPEN / 17:00 OPEN and Tue 4 Jul 12:00 PREOPEN-HALT / 17:00 OPEN → TRADE DATE **WED 5 JULY**; Wed 16:00 CLOSED. Tue 4 Jul is not a trade date. |
| E5 | `labor-day-2023.pdf` (snap 2023-08-02) | Identical shape to E2: Sun 3 Sep / Mon 4 Sep → TRADE DATE TUES 5 SEP. |
| E6 | `thanksgiving-day-2023.pdf` (snap 2023-12-03) | Wed 22 Nov 16:00 CLOSED (normal trade date); Wed 16:45 PREOPEN / 17:00 OPEN and Thu 23 Nov 12:00 PREOPEN-HALT / 17:00 OPEN → TRADE DATE **FRI 24 NOV**; Fri 24 Nov **12:15 CLOSED** (early close). Thu 23 Nov is not a trade date. |
| E7 | `christmas-day-2023.pdf` (snap 2026-07-19 of the 2023 file) | Mon 25 Dec 16:00 PREOPEN / 17:00 OPEN → TRADE DATE **TUES 26 DEC**; no session earlier that weekend (no Sunday 24 Dec open); Tue 26 Dec 16:00 = "Regular CME Group Globex close for all products"; notes state day orders entered after Monday's pre-open are for trade date Tuesday 26 December. Mon 25 Dec is not a trade date. |

These tables are the primary evidence that a trading date is **assigned, not
derived**: the Sunday-evening open before a Monday holiday belongs to Tuesday
(E2/E3/E5), one trade date spans multiple disjoint trading windows split by a
holiday halt, and Thanksgiving's Friday trade date opens on Wednesday evening.

Definition ids: `cme-memorial-day-2023-schedule`, `cme-juneteenth-2023-schedule`,
`cme-independence-day-2023-schedule`, `cme-labor-day-2023-schedule`,
`cme-thanksgiving-2023-schedule`, `cme-christmas-2023-schedule`.

### E8. Completeness of the 2023 exception set — `corroborated`

- **Source:** CME Group *Holiday Calendar* page
  (`https://www.cmegroup.com/tools-information/holiday-calendar.html`),
  snapshots **2023-05-28** and **2023-12-03** (the latter enumerates the whole
  year retrospectively).
- The page's Globex trading-hours holiday views for 2023 cover exactly the
  trade dates 01-16, 02-20, 04-07, 05-29, 06-19, 07-04, 09-04, 11-23, 12-25
  (plus 2024-01-01). Within the supported range that is precisely the
  exception set encoded (E2–E7).
- **Columbus Day (Mon 2023-10-09) and Veterans Day (Fri 2023-11-10)** appear
  only as clearing / OTC advisories, not as Globex trading-schedule
  deviations. The Columbus Day clearing advisory
  (`.../tools-information/holiday-calendar/files/2023-columbus-day-advisory.pdf`)
  defines clearing cycles only — trades clear top-day, settlement files at
  their normal window — and for trading hours simply points at the regular
  trading-hours pages. Both days are therefore encoded as **regular weekly
  days**. Status `corroborated`, not `verified-primary`: their normality
  follows from CME's own enumeration of deviations rather than from a
  document that states the day's equity hours directly.
- Definition id: `cme-holiday-calendar-2023-enumeration`.

### E9. Sunday pre-open time — `corroborated`

Every week-opening pre-open in E2–E7 is **16:00 CT** (Sundays 28 May, 18 Jun,
3 Sep; Monday 25 Dec after the full weekend closure), against 16:45 on
ordinary weekdays. All evidenced instances are holiday-week openings; the
16:00 time is applied to *every* week-opening pre-open in the weekly rule, and
that generalization is why the weekly Sunday pre-open rule carries
`corroborated` rather than `verified-primary`. The tradable session
(17:00 open) is unaffected: pre-open windows are HALT (no matching) either
way.

## Facts deliberately not encoded

- **Anything before 2023-05-22 or after 2023-12-29.** Outside the supported
  range the calendar refuses to answer (`UnsupportedTimestampError` /
  `CalendarRangeError`). In particular: 2024 New Year's Eve/Day behavior,
  Good Friday 2023 (2023-04-07, before the range), and every earlier year.
- **The 2023 spring DST transition** (2023-03-12) predates the range; DST
  *mechanics* (gap and fold rejection) are enforced by the materializer and
  tested synthetically, and the in-range 2023-11-05 fall-back is covered by
  materialized windows and tests.
- **Settlement-price publication semantics, auction/pre-open order mechanics,
  price-limit states.** Out of Phase 2.1 scope; the calendar models temporal
  tradability states only.
- **Any maintenance interval for this schedule class.** CME's equity tables
  label the daily platform breaks `CLOSED` (16:00–16:45) and `PREOPEN (HALT)`
  (16:45–17:00); no CME source retrieved labels an equity-schedule interval
  "maintenance", so the `MAINTENANCE` state — which the engine supports —
  is deliberately unused by `CME_EQUITY_INDEX` v1.
- **Other product groups** (interest rates, energy, metals, …): their rows in
  E2–E7 differ from equities and were not encoded.

## Correction policy

A released definition version is immutable. If any claim above is found
wrong, the correction is a **new version** of the definition (new
`CalendarVersion`, new materialized content hash) with the corrected fact and
its evidence appended here; v1 keeps reproducing its released answers.

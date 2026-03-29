# Every API and data source for UK train delay tracking

**The UK rail data ecosystem offers a surprisingly rich set of free APIs that can power a personal commute delay tracker — and the Historical Service Performance (HSP) API is the single most important resource.** HSP provides up to a year of per-service actual arrival/departure times via a simple REST/JSON interface, completely free. Combined with the Realtime Trains API for daily capture and Network Rail's bulk delay attribution downloads for detailed reason codes, a developer can build a comprehensive delay-tracking tool without spending a penny. The ecosystem is fragmented across multiple platforms — Network Rail, National Rail Enquiries, Rail Data Marketplace, and independent projects — but the combination is powerful. This report documents every available source, what each provides, and the optimal architecture for a Claude Code project.

---

## The HSP API is your single most valuable resource

The **Historical Service Performance API**, operated by National Rail Enquiries, is the only free API offering queryable historical performance data at the individual train level. It stores Darwin-derived records and exposes them through two REST endpoints at `https://hsp-prod.rockshore.net/api/v1/`.

**`serviceMetrics`** accepts POST requests with origin/destination CRS codes, date ranges, time windows, and day type (WEEKDAY/SATURDAY/SUNDAY). It returns every matching service with a unique RID (Request ID), plus punctuality statistics at configurable tolerance thresholds (e.g., percentage of trains within 2, 5, or 10 minutes). **`serviceDetails`** takes a single RID and returns per-location data: scheduled public timetable arrival/departure (`gbtt_pta`/`gbtt_ptd`), **actual arrival/departure** (`actual_ta`/`actual_td`), and a `late_canc_reason` numeric code. Authentication is HTTP Basic using your National Rail Data Portal credentials. The API is free with a practical rate limit of **~1,000 requests per hour**.

Historical depth is officially "up to one year," though community members report data stretching back to 2016 in some cases. The key limitation is that delay minutes are not returned directly — you must calculate them from scheduled vs. actual times. Cancellations are identified by the absence of actual times at a calling point. The `late_canc_reason` is a numeric code (not the two-character DAPR code), and reason detail is less granular than formal TRUST delay attribution. Registration is at `opendata.nationalrail.co.uk`, where you must enable the HSP subscription checkbox in your profile. **Note that the National Rail Data Portal is planned for retirement in early 2026**, with migration to the Rail Data Marketplace (`raildata.org.uk`) — so new users should register at raildata.org.uk.

For your use case — tracking a specific daily commute over time — HSP is ideal. Query `serviceMetrics` for your route and commute time window to get all matching services, then drill into `serviceDetails` for each RID to get actual times at every calling point. Store results in a local database and you have a growing historical record with zero infrastructure overhead.

---

## Real-time data capture: Realtime Trains vs Darwin vs Network Rail

Three tiers of real-time data exist, each with different trade-offs for a personal tool.

**Realtime Trains API** (`api.rtt.io`) is the easiest entry point. It provides REST/JSON responses with HTTP Basic authentication, covering both real-time and historical service data derived from Network Rail TRUST feeds. Query by station CRS code and date to get departure/arrival boards, or by service UID and date for full journey detail. Fields include **both working timetable (WTT) and public timetable (GBTT) times**, actual arrival/departure with a flag distinguishing real actuals from estimates, platform, cancellation codes with reason text (in detailed mode), and operator information. Historical data is queryable by specifying past dates. It is **free for personal, academic, and educational use** with no published rate limit beyond "reasonable." A Python library (`pip install rttapi`) wraps the API cleanly. The significant caveat: registrations at `api.rtt.io` have been periodically suspended due to abuse, so access is not guaranteed.

**Darwin OpenLDBWS and OpenLDBSVWS** are the official NRE SOAP APIs powering station departure boards. The public version returns scheduled/estimated/actual times, delay and cancellation reason text, platform, and operator for any station. The **Staff Version (OpenLDBSVWS)** adds train reporting numbers (headcodes), schedule UIDs, **fully-qualified timestamps** (critical for programmatic use vs. the public version's "HH:MM" strings), passing points, and a reference data endpoint for reason code lookups. Both are free up to **5 million requests per 4-week period** (~5,000/hour). Authentication is via a GUID token obtained at registration. The main pain point is SOAP/XML — which is where **Huxley2** comes in. This open-source .NET proxy (GitHub: `jpsingleton/Huxley2`) wraps OpenLDBWS in CORS-enabled REST/JSON, adds a purpose-built `/delays/{CRS}/to/{filterCRS}` endpoint, and can be self-hosted via Docker. It is feature-complete on .NET 8.0.

**Network Rail Open Data (NROD)** provides the rawest, most granular data via STOMP streaming from ActiveMQ. The **Train Movements feed** delivers JSON messages for every train arrival, departure, and passing event recorded by TRUST, including `actual_timestamp`, `gbtt_timestamp`, `planned_timestamp`, `timetable_variation` (delay in whole minutes), `variation_status`, STANOX location codes, TOC code, and platform. Cancellation messages include the formal **two-character DAPR reason code**. The feed covers all trains — passenger, freight, and engineering. Registration is free at `publicdatafeeds.networkrail.co.uk` but **limited to 1,000 accounts** with historically long waiting lists. Connection requires a STOMP client (Python: `stomp.py`), and you must build your own database since there is no historical query API. STANOX codes must be mapped to station names via the CORPUS reference dataset. TRUST data rounds to **whole minutes** with 30-second reporting intervals — a train recorded as 1 minute late may only be 31 seconds late.

---

## Delay attribution: how reason codes actually work

The UK rail industry's delay attribution system is governed by the **Delay Attribution Principles and Rules (DAPR)**, maintained by the independent Delay Attribution Board and incorporated into the Network Code. When TRUST detects a lateness change of **≥3 minutes** between two reporting points, a delay alert triggers investigation by a Level 1 Train Delay Attributor (TDA).

Each incident receives a **two-character cause code** (first letter = category, second = specific cause) and a **responsible manager code** identifying who caused it. The categories are: **I/J** = infrastructure (Network Rail), **T** = TOC passenger operating, **M/N** = mechanical/fleet, **O** = NR operating, **R** = station operating, **X** = external events (NR responsibility), **V** = external events (TOC responsibility), **Y** = reactionary delays, **P** = planned/excluded, and **Z** = unexplained. Common codes include **IA** (signal failure), **IB** (points failure), **TG** (driver shortage), **XC** (trespass), and **MA** (rolling stock failure).

Attribution is **not fully real-time**. Initial coding happens within hours, but the full process of investigation, acceptance, and dispute resolution can take days or weeks. Approximately **40% of attributions are disputed**. This means different APIs provide different levels of reason detail at different timepoints:

- **NROD TRUST Movements**: Cancellation messages include DAPR codes in real-time; delay attribution codes are NOT in the live feed
- **Darwin/OpenLDBWS**: Provides passenger-facing reason text (e.g., "a signal failure") — simplified, not formal DAPR codes
- **HSP API**: Returns a numeric `late_canc_reason` code — less granular than full DAPR
- **Network Rail Historic Attribution Data** (bulk CSV download): Contains the complete, final DAPR codes with responsible manager, incident number, PfPI minutes, and performance event codes — this is the **only source for fully attributed delay reasons** but is only available as periodic bulk file downloads from `networkrail.co.uk/who-we-are/transparency-and-ethics/transparency/`

Sub-threshold delays (under 3 minutes) account for roughly **35% of all delay minutes** but are generally never formally attributed.

---

## ORR statistics and aggregate performance data

The **Office of Rail and Road** publishes comprehensive aggregate performance statistics at `dataportal.orr.gov.uk`. Data is available as ODS/Excel/CSV downloads (no API) covering punctuality percentages (PPM, On Time, Time to 3, Time to 15), cancellations by operator and cause, delay minutes per 1,000 train-miles by Network Rail route, and CaSL (Cancellations and Significantly Late) metrics. Granularity is by TOC, by NR route, by sector (London & South East, Long Distance, Regional), and by cause category — but **never at individual train level**. Historical depth reaches back to approximately 2002 for some series. Data is updated quarterly and periodically (every 4 weeks, with ~15 working days lag). Licensed under Open Government Licence v3.0.

ORR data is useful for contextualizing your commute against system-wide performance — for example, comparing your route's punctuality against operator or national averages — but cannot tell you about individual services. Files sit at stable URLs, so scheduled downloads via `curl` or `requests` are straightforward even without a formal API.

The latest data (October–December 2025) shows **Time to 3 punctuality at 81.5%** and cancellations at 4.0% nationally.

---

## Other sources, community projects, and GTFS

**Transport API** (`transportapi.com`) aggregates multiple UK transport data sources into managed REST/JSON APIs, including a Rail Performance product developed with Northern Trains that offers rich historical data with scheduled, actual, and expected times plus delay reasons. However, the free tier is now limited to just **30 requests per day**, and the platform has shifted firmly toward commercial B2B customers. Not recommended for a personal project unless you're willing to pay.

**OpenTrainTimes** (`opentraintimes.com`) is a visualization tool displaying real-time signalling diagrams from Train Describer data across 126 hand-drawn maps. It hosts the invaluable **Open Rail Data Wiki** (`wiki.openraildata.com`) — the de facto community documentation for all Network Rail feeds — but does not offer a public API for delay data.

**GTFS feeds do not exist officially** for UK National Rail. The industry uses legacy CIF format for timetables. Community tools like **UK2GTFS** (R package by ITS Leeds) and **ATOCCIF2GTFS** (C#) can convert CIF to GTFS, and a pre-converted feed exists on Datahub (`old.datahub.io/en/dataset/gb-rail-gtfs-feed`), updated weekly. No GTFS-RT feed exists.

**Notable GitHub projects** for developers entering this space:

- **`philwieland/openrail`** — Complete C suite for ingesting NROD data (timetable, TRUST movements, signal diagrams) into MySQL with web display. The most comprehensive self-hosted solution, but requires significant setup.
- **`solomon-wheeler/train_delay`** — Python tool using the HSP API to query and visualize delay data with Plotly. Closest existing project to the user's goal.
- **`jpsingleton/Huxley2`** — .NET REST proxy for Darwin SOAP APIs (60+ GitHub stars).
- **`peter-mount/nre-feeds`** — Darwin Push Port backend for departure boards.
- **`rttapi` Python package** — Clean wrapper for Realtime Trains API.
- **`nre-darwin-py`** — Python abstraction for NRE Darwin SOAP (now somewhat superseded by JSON APIs on RDM).

**TOC-specific and Delay Repay data**: No TOC publishes individual train-level performance data. ORR publishes aggregate delay compensation claim statistics by TOC (volumes, approval rates, processing times). FOI requests to Network Rail (`foi@networkrail.co.uk`) have yielded detailed attribution data in the past.

---

## Comparison of all data sources

| Source | Type | Historical? | Delay reasons | Format | Cost | Rate limit | Ease |
|---|---|---|---|---|---|---|---|
| **HSP API** | REST query | ✅ Up to 1yr | Numeric code | JSON POST | Free | ~1,000/hr | Easy |
| **Realtime Trains** | REST query | ✅ Past dates | Text (detailed mode) | JSON GET | Free (personal) | Reasonable | Very easy |
| **NROD TRUST Movements** | Streaming | ❌ (capture yourself) | Cancellation DAPR codes only | JSON/STOMP | Free | N/A (stream) | Hard |
| **Darwin OpenLDBWS** | SOAP polling | ❌ | Text reason strings | XML SOAP | Free <5M/4wk | ~5,000/hr | Medium |
| **Darwin OpenLDBSVWS** | SOAP polling | ❌ | Text + UIDs/headcodes | XML SOAP | Free <5M/4wk | ~5,000/hr | Medium |
| **Huxley2** | REST proxy | ❌ | Text reason strings | JSON GET | Free (self-host) | Underlying API | Very easy |
| **Darwin Push Port** | Streaming | ❌ (capture yourself) | XML reason elements | XML/STOMP | Free | Unlimited | Hard |
| **NR Historic Attribution** | Bulk download | ✅ Years | Full DAPR codes + manager | CSV/Excel | Free | N/A | Medium |
| **ORR Statistics** | File download | ✅ Back to ~2002 | By cause category | ODS/CSV | Free | N/A | Easy |
| **Transport API** | REST query | ✅ Rich | Darwin reasons | JSON GET | 30 req/day free | Very limited | Easy but costly |
| **Rail Data Marketplace** | Portal | Varies | Varies | Various | Mostly free | Per-product | Medium |

---

## How to identify "the same train" every day

UK rail uses overlapping identification systems. **TRAIN_UID** (one letter + five digits, e.g., `W34893`) is the most useful for a commute tracker — it is stable within a timetable period (roughly six months between the May and December timetable changes) and uniquely identifies a scheduled service. The **headcode** (four characters, e.g., `1T55`) is the most human-memorable and generally repeats daily for the same service, but is not guaranteed unique nationally. The **TRUST train_id** (10 characters) changes daily as it encodes the day of month. The **Darwin RID** encodes the date and UID into a single identifier unique per service per day.

For practical implementation: find your commute service on `realtimetrains.co.uk`, note the **service UID** from the URL, and use this as your stable identifier. When timetables change in May or December, you will need to look up the new UID for what is effectively the same service. The RTT API endpoint `/json/service/{serviceUid}/{year}/{month}/{day}` returns complete journey detail for any date.

Performance thresholds to be aware of: **PPM** measures arrival at final destination within 5 minutes (London & South East/Regional) or 10 minutes (Long Distance). The newer **"On Time" statistic** (from April 2019) measures arrival within **1 minute at every station stop** — a much stricter standard showing ~65% nationally versus ~90% for PPM.

---

## Recommended architecture for a Claude Code project

The optimal approach combines three data sources in a lightweight Python pipeline:

**Realtime Trains API** for daily commute capture. A cron job running every 3–5 minutes during your commute window polls RTT for your service UID, recording scheduled times, actual times, delay, platform, and cancellation status into a local SQLite database. The `rttapi` Python package makes this trivial. **HSP API** for historical backfill and trend validation. A weekly job queries `serviceMetrics` for your route and time window, then drills into `serviceDetails` for each RID to populate your database with up to a year of historical records. **Network Rail Historic Attribution Data** for detailed delay reasons. A monthly download of the bulk CSV files adds formal DAPR codes, responsible managers, and incident details that neither RTT nor HSP provide.

```
train-delay-tracker/
├── config.py              # API credentials, route/station config, service UIDs
├── capture_realtime.py    # Polls RTT API during commute windows
├── pull_hsp_history.py    # Weekly HSP backfill
├── download_attribution.py # Monthly NR bulk attribution CSV
├── analysis.py            # Pandas-based trend analysis and aggregations
├── dashboard.py           # Flask/FastAPI web app with Chart.js
├── models.py              # SQLite schema and query helpers
├── delay_codes.json       # DAPR code reference lookup
├── data/                  # SQLite DB, downloaded CSVs
└── README.md
```

The core database schema needs four tables: `journey_observations` (one row per commute journey with scheduled/actual times, delay minutes, reason, cancellation status, and data source), `delay_attribution` (from NR bulk data with incident numbers, DAPR codes, responsible managers, PfPI minutes), `delay_codes` (reference table mapping two-character codes to descriptions and categories), and `schedule_reference` (service UIDs with validity dates, days of operation, and times for detecting timetable changes).

Schedule cron jobs for: real-time capture every 3 minutes during commute windows on weekdays, a daily summary job after evening commute, a weekly HSP historical pull, and a monthly attribution data download. The entire project fits comfortably in Python 3 with `requests`/`httpx`, `pandas`, SQLite, and a simple Flask dashboard — well within Claude Code's capabilities.

---

## Terms of service and licensing considerations

All three recommended sources permit personal, non-commercial use. **Network Rail Open Data** uses a custom licence permitting personal and commercial use but prohibiting use of NR/National Rail branding or calling your application "official." **Darwin/HSP** is licensed under an NRE variant of Open Government Licence v2.0 — personal use is allowed with attribution to National Rail Enquiries required. **Realtime Trains** is explicitly free for personal, academic, and educational use; commercial use requires a separate arrangement. Storing and caching data locally is permitted (and necessary for NROD streaming feeds). Redistribution of derived products is generally allowed with attribution. There are **no GDPR concerns** — these feeds contain only operational train data, not personal information. If you publish a dashboard publicly, just include attribution lines for your data sources and avoid NR/NRE logos.

## Conclusion

The UK rail open data ecosystem is more capable than most developers expect, but its fragmentation across platforms and formats creates a learning curve. The key insight is that **no single API provides everything** — real-time capture, historical querying, and detailed delay attribution each come from different sources. The HSP API is the strategic foundation for any historical analysis tool, RTT is the path of least resistance for real-time capture, and Network Rail's bulk attribution data is the only route to formal delay reason codes. A Claude Code project combining these three can deliver all four of the user's requirements — per-service delay tracking, daily/average views, route-level analysis, and full depth on punctuality, delay minutes, reasons, cancellations, and historical trends — with a modest investment of development time and zero ongoing cost.
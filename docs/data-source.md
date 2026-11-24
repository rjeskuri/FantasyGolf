# Data source: the pgatour.com API

Everything the pipeline ingests comes from the GraphQL API behind www.pgatour.com. It was
probed on 2026-11-24; this page records what was found. `src/fantasygolf/sources/pgatour/endpoints.py`
holds the registry (`ENDPOINTS`, `OFFICIAL_MONEY_STAT_ID`, `ParseError`) and is the
authoritative copy of every GraphQL document.

**Terms of use.** This is an undocumented, internal API, and scraping it is against the site's
terms of use. The client must be polite: sequential requests, a fixed interval, backoff on
errors, an on-disk cache so nothing completed is fetched twice, and only the stats the model
needs.

## Transport

- One endpoint serves every family: `POST https://orchestrator.pgatour.com/graphql`
  (AWS AppSync behind CloudFront).
- Body: JSON `{"operationName": ..., "query": ..., "variables": {...}}`.
- No family paginates: each response holds every row (the OWGR stat returned 998 rows).
- GraphQL errors arrive as HTTP 200 with a top-level `errors` array; always check for it.
- Introspection (`__schema`, `__type`) is enabled, which helps when the site changes a field.

### Headers

The site's JavaScript (the graphql-request client in the `_app-*.js` chunk) sends:

| header | value |
|---|---|
| `x-api-key` | the AppSync API key, read from `PGATOUR_API_KEY` |
| `x-pgat-platform` | `web` |
| `Content-Type` | `application/json` |

Signed-in users also send `Authorization: Bearer <login token>`; nothing here needs it.
`Origin: https://www.pgatour.com` and `Referer: https://www.pgatour.com/` are harmless (the API
answers with `access-control-allow-origin: https://www.pgatour.com`). Send a generic User-Agent.

Without `x-api-key` the API returns a CloudFront `503` HTML page ("The request could not be
satisfied"), not JSON. A client should treat a non-JSON 403/5xx as "key missing or rotated",
not as a parse error of the data. (An invalid key still got a 200 for a cached `scheduleYears`
query once; do not rely on that.)

### Refreshing the key when it rotates

The key is an AppSync API key: it starts with `da2-` and is 30 characters long. It is public
in the site's JavaScript and changes when the site rotates it. To find the current one:

1. Fetch `https://www.pgatour.com/` and search the HTML for `/_next/static/chunks/pages/_app-`.
   That is the app chunk `https://www.pgatour.com/_next/static/chunks/pages/_app-<hash>.js`;
   the hash changes with every deploy.
2. Fetch that chunk and find the production config object, the one whose `"comment"` is
   `"production env, used by live site, prod staging site"`. It holds `"apiKey":"da2-..."`
   followed by `"queryEndpoint":"https://orchestrator.pgatour.com/graphql"`:

   ```sh
   grep -o '"production env, used by live site, prod staging site","apiKey":"[^"]*"' _app.js
   ```

   The same chunk has other config objects (uat, dev, IGF) with other keys: do not use them.
   The homepage also embeds Gigya `api-key` values starting `3_`: those belong to the login
   widget, not this API.
3. Put the value in `.env` as `PGATOUR_API_KEY=...` (see `.env.example`). Never commit it, log
   it or write it into a fixture.

## Rate limits and the recommended interval

- Observed: about 99 requests in total (30 to the GraphQL API, the rest to the site's HTML and
  JavaScript), each at least 2 s after the previous one. No 429s, no error status with a valid
  key, and no rate-limit headers at all. Response headers were only content-type,
  content-length, date, x-cache, via, x-amz-cf-pop, x-amz-cf-id and access-control-*. Responses
  took 0.5 to 4 s.
- Recommended: one request at a time, at least **2 s** between requests, exponential backoff on
  429 and 5xx (2, 4, 8 ... up to 60 s, at most about 5 tries), and every raw response cached on
  disk so a re-run does not refetch completed seasons.
- Cost of a full scrape: roughly seasons x (1 schedule + ~50 events + one request per stat).
  The catalog has ~476 stats per season, so scrape the stat ids the model uses, not all of them.

## Seasons

- **Wraparound vs calendar labelling.** Seasons up to 2023 wrap around: season 2019 is
  2018-19 and starts with the fall events of October 2018 (the Safeway Open, `R2019464`).
  From 2024 the season is the calendar year. The labels differ by query:

  | where | 2019 season | 2024 season |
  |---|---|---|
  | `schedule.seasonYear` | `"2018 - 2019"` (spaces) | `"2024"` |
  | `statDetails.displaySeason`, `yearPills` | `"2018-2019"` | `"2024"` |
  | `scheduleYears.displayValue` | `"2018-2019"` | `"2024"` |

- The 2023 schedule (61 events) also contains the Sept to Dec 2023 FedExCup Fall events, as
  `R20235xx` ids (`R2023543` Fortinet ... `R2023549` RSM), while the 2024 schedule starts with
  The Sentry in January 2024. A calendar-year feature must bucket events by `startDate`, not by
  schedule season.
- **The year argument differs per query:** `schedule(year:)` takes a String (`"2019"`);
  `statDetails` and `statOverview` take an Int season (`2019`); `tournamentPastResults(year:)`
  takes an Int encoded as **season x 10 + n** (`19950`, `20190`, `20240`). The digit `n`
  separates two editions in one season: the 2020-21 season has `20210` and `20211` (the
  November 2020 and April 2021 Masters). A plain `1995` silently returns the latest edition.
- Tournament ids are `R` + season + a three-digit permanent number (`R2024014` is the 2024
  Masters).
- Earliest seasons available, per family:

  | family | earliest | notes |
  |---|---|---|
  | schedule | 2012 | 2011 and earlier return an empty schedule, no error; `scheduleYears` lists 2012..2027 |
  | event_results | per tournament | `availableSeasons` lists every edition; the Masters goes back to 1934 (`19340`) |
  | money_list (109) | 1987 | |
  | owgr (186) | 1995 | |
  | fedex_bonus (02396) | 2007 | |
  | season_stat | per stat | `yearPills` lists every season; strokes gained (SG: Total, 02675) starts in **2004** |
  | stat_catalog | per season | categories can differ by season; query each season you need |
  | player_list, player_bio | current only | no season argument |

## Endpoint families

All families use `POST https://orchestrator.pgatour.com/graphql`. The GraphQL documents are in
the appendix and in `endpoints.py`; the table shows the variables, as recorded in the fixtures.

| family | operation | variables |
|---|---|---|
| `schedule` | `Schedule` | `{"tourCode": "R", "year": "2019"}` |
| `event_results` | `TournamentPastResults` | `{"tournamentPastResultsId": "R2024014", "year": 20240}` |
| `stat_catalog` | `StatOverview` | `{"tourCode": "R", "year": 2024}` |
| `season_stat` | `StatDetails` | `{"tourCode": "R", "statId": "02675", "year": 2024, "eventQuery": null}` |
| `money_list` | `StatDetails` | `{"tourCode": "R", "statId": "109", "year": 2024, "eventQuery": null}` |
| `fedex_bonus` | `StatDetails` | `{"tourCode": "R", "statId": "02396", "year": 2024, "eventQuery": null}` |
| `owgr` | `StatDetails` | `{"tourCode": "R", "statId": "186", "year": 2024, "eventQuery": null}` |
| `player_list` | `PlayerDirectory` | `{"tourCode": "R", "active": null}` |
| `player_bio` | `Player` | `{"playerId": "46046"}` |

None of them paginates.

### schedule

`completed[]` and `upcoming[]` are months, each with `tournaments[]`: `id`, `tournamentName`,
`startDate` (epoch ms), `purse` (`"$6,400,000"`; majors and the TOUR Championship are sometimes
`"$0"`), `champion` (team events: `"Rory McIlroy, Shane Lowry"`), `championEarnings` (per
player, may be null), `tourStandingValue` (`"500 pts"`, null for unofficial events such as the
Hero), and `tournamentCategoryInfo.type` (`SIGNATURE` or `PLAYOFF`, otherwise null; the 2023
"designated" events are not tagged). The documents deliberately leave out the tournament's
ticketing `*ApiKey` fields.

### event_results

Individual events: `players[]` with `position` (`"1"`, `"T5"`, `"CUT"`, `"W/D"` ...),
`rounds[]`, `total`, `parRelativeScore`, and `additionalData` aligned to
`additionalDataHeaders` = `["FedExCup Pts", "Official Money"]`, e.g.
`["500.000", "$1,152,000.00"]` (points before 2007 are `"0.000"`); `teams` is null. Team events
(the Zurich Classic): `players` is `[]`, `winner` is null, and `teams[]` holds `teamId`, two
`players` and per-player `additionalData` (each partner earns the listed amount). The `id`
argument is any id of the tournament; the response `id` is the edition id (`R1995014`).

### stat_catalog

`categories[].subCategories[].stats[]` gives `{statId, statTitle}`: in 2024, 9 categories, 592
entries and 476 unique stat ids (a stat appears under several subcategories, so dedupe by
`statId`). `stats[]` holds 8 headline stats with their top-3 leaders.

### season_stat

`rows[]` is a union: `StatDetailsPlayer` (`playerId`, `playerName`, `rank`,
`stats[] {statName, statValue}` aligned to `statHeaders`) or `StatDetailTourAvg`; guard on
`__typename`. Values are strings (`"2.496"`, `"$29,228,357"`, `"15.6000"`). `eventQuery:
{queryType: THROUGH_EVENT | EVENT_ONLY, tournamentId}` restricts a stat to one event or through
an event; the pipeline sends `null` for the season figure.

### money_list

The official money list is the stat **`OFFICIAL_MONEY_STAT_ID = "109"`** ("Official Money"),
served by `StatDetails`. `statHeaders` is `["Money", "YTD Victories"]`; 1987 onward. Confirmed
from the stat catalog (category `MONEY_FINISHES`, subcategory Money), from
`/stats/detail/109` being the site's money-list page, and from the values matching published
money lists (2024 Scheffler $29,228,357; 2019 Koepka $9,684,006; 1995 Norman $1,654,959).
Related money stats: 194 Total Money (Official and Unofficial), 139 Non-member Earnings, 154
Money per Event, 02444 Money Leaders - Fall Series, 02671 FedExCup Standings (points).

### fedex_bonus

Served through the stats query, not a dedicated one: `StatDetails` with stat id **02396**
(FedExCup Bonus Money), 2007 onward. Values have no `$` (`"25,000,000"`) and ties share a rank.
`tourCupSplit` (the /fedexcup page) has standings and points, not bonus money.

### owgr

Served through the stats query: `StatDetails` with stat id **186** (Official World Golf
Ranking, season end), 1995 onward. `statHeaders` is
`["Avg Points", "Total Points", "Points lost", "Points gained"]`. The current ranking is also
`player(id).owgr`. There is no dedicated OWGR query.

### player_list

Current members only: 219 players, all `isActive: true` (`active: false` returns the same
219), sorted by `alphaSort`. Historical players are reachable only through result and stat
rows (`playerId`).

### player_bio

`player(id)`: names, country, current `owgr`, `rank {rank, statName}`, and `playerBio` (born,
age, height, turnedPro, school, birthplace, residence, careerEarnings, exemptions ...).

## Fixtures

Recorded responses live in `tests/fixtures/pgatour/`, each listed in `manifest.json` with the
request that produced it (method, URL, body; never headers) and hand-checked `expect` values.
`tests/fakes.py` (`FixtureClient`) serves them by request, so ingest tests run offline. Bodies
are pretty-printed; `statDetails.rows` and `playerDirectory.players` are trimmed to the first 25
rows (`expect.row_count` 25, `expect.original_row_count` the live count). Schedules, event
results, the catalog and the bio are whole.

| file | family | covers |
|---|---|---|
| `schedule/schedule_2019.json` | schedule | 2018-19 wraparound season, 51 events, starts with the Safeway Open in Oct 2018; Zurich `R2019018` |
| `schedule/schedule_2023.json` | schedule | 2022-23, 61 events including the 2023 fall events; no SIGNATURE tags |
| `schedule/schedule_2024.json` | schedule | first calendar season, 52 events, 8 SIGNATURE, 3 PLAYOFF |
| `event_results/1995_masters_tournament.json` | event_results | the 1990s: Crenshaw, $396,000.00, 86 rows, 0.000 points |
| `event_results/2019_safeway_open.json` | event_results | a 2019 fall event: Tway, $1,152,000.00, 500 pts, 144 rows |
| `event_results/2023_genesis_invitational.json` | event_results | a 2023 designated event ($20M): Rahm, $3,600,000.00, 550 pts |
| `event_results/2024_masters_tournament.json` | event_results | a 2024 major: Scheffler, $3,600,000.00, 750 pts |
| `event_results/2024_zurich_classic.json` | event_results | a team event: 80 teams, Lowry/McIlroy, $1,286,050.00 each |
| `stat_catalog/stat_overview_2024.json` | stat_catalog | the 2024 catalog |
| `season_stat/sg_total_02675_2024.json` | season_stat | SG: Total 2024 (184 rows live) |
| `money_list/official_money_109_1995.json` | money_list | Official Money 1995 (359 rows live) |
| `money_list/official_money_109_2019.json` | money_list | Official Money 2018-19 (265 rows live) |
| `money_list/official_money_109_2024.json` | money_list | Official Money 2024 (245 rows live) |
| `fedex_bonus/fedexcup_bonus_money_02396_2024.json` | fedex_bonus | FedExCup Bonus Money 2024 (239 rows live) |
| `owgr/owgr_186_2024.json` | owgr | OWGR at the end of 2024 (998 rows live) |
| `player_list/player_directory_R.json` | player_list | the PGA TOUR directory (219 players live) |
| `player_bio/player_46046_scottie_scheffler.json` | player_bio | one player's bio |

## Appendix: GraphQL documents

Verbatim, as recorded. `endpoints.py` holds the same strings; if they ever differ, the code and
the fixtures win.

### Schedule (schedule)

```graphql
query Schedule($tourCode: String!, $year: String) { schedule(tourCode: $tourCode, year: $year) { tour seasonYear completed { month year monthSort tournaments { id tournamentName date startDate sortDate sequenceNumber city state country courseName champion championId champions { displayName playerId } championEarnings purse tournamentStatus tourStandingHeading tourStandingValue tournamentCategoryInfo { type label } } } upcoming { month year monthSort tournaments { id tournamentName date startDate sortDate sequenceNumber city state country courseName champion championId champions { displayName playerId } championEarnings purse tournamentStatus tourStandingHeading tourStandingValue tournamentCategoryInfo { type label } } } } }
```

### TournamentPastResults (event_results)

```graphql
query TournamentPastResults($tournamentPastResultsId: ID!, $year: Int) { tournamentPastResults(id: $tournamentPastResultsId, year: $year) { id players { id position player { id firstName lastName shortName displayName amateur country countryFlag } rounds { score parRelativeScore } additionalData total parRelativeScore } teams { teamId position players { id firstName lastName shortName displayName amateur country countryFlag } additionalData total parRelativeScore rounds { score parRelativeScore } } rounds additionalDataHeaders availableSeasons { year displaySeason } winner { id firstName lastName totalStrokes totalScore countryFlag countryName purse displayPoints displayPurse points seed pointsLabel } winningTeam { id firstName lastName totalStrokes totalScore countryFlag countryName purse displayPoints displayPurse points seed pointsLabel } } }
```

### StatOverview (stat_catalog)

```graphql
query StatOverview($tourCode: TourCode!, $year: Int) { statOverview(tourCode: $tourCode, year: $year) { tourCode year categories { category displayName subCategories { displayName stats { statId statTitle } } categoryType } stats { statName tourAvg statId players { statId playerId statTitle statValue playerName rank country countryFlag } } } }
```

### StatDetails (season_stat, money_list, fedex_bonus, owgr)

```graphql
query StatDetails($tourCode: TourCode!, $statId: String!, $year: Int, $eventQuery: StatDetailEventQuery) { statDetails(tourCode: $tourCode, statId: $statId, year: $year, eventQuery: $eventQuery) { __typename tourCode year displaySeason statId statType tournamentPills { tournamentId displayName } yearPills { year displaySeason } statTitle statDescription tourAvg lastProcessed statHeaders rows { ... on StatDetailsPlayer { __typename playerId playerName country countryFlag rank rankDiff rankChangeTendency stats { statName statValue color supportingData supportingDataColor } } ... on StatDetailTourAvg { __typename displayName value } } } }
```

### PlayerDirectory (player_list)

```graphql
query PlayerDirectory($tourCode: TourCode!, $active: Boolean) { playerDirectory(tourCode: $tourCode, active: $active) { tourCode players { id isActive firstName lastName shortName displayName alphaSort country countryFlag playerBio { id education turnedPro age } } } }
```

### Player (player_bio)

```graphql
query Player($playerId: ID!) { player(id: $playerId) { id firstName lastName displayName country countryFlag owgr rank { rank statName } playerBio { heightImperial heightMeters weightImperial weightKilograms age born birthplace { city state stateCode country countryCode } turnedPro school degree graduationYear residence { city state stateCode country countryCode } playsFrom { city state stateCode country countryCode } family personal careerEarnings pronunciation exemptions { tour description expirationDate } deceased deceasedDate primaryTour } } }
```

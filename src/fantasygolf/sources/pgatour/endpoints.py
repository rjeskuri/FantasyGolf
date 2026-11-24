"""The pgatour.com API endpoints the ingest calls, one per data family.

Every family is a POST of `{"operationName", "query", "variables"}` to the one GraphQL endpoint.
The documents below are byte-for-byte the ones the fixtures in `tests/fixtures/pgatour/` were
recorded with, so a request built from `ENDPOINTS[family].query` matches a recorded one. Change
a document only together with re-recorded fixtures. docs/data-source.md has the variables,
season encodings, the API key header and the rate-limit policy.
"""

from __future__ import annotations

from dataclasses import dataclass

GRAPHQL_URL = "https://orchestrator.pgatour.com/graphql"

# The stat behind the official money list (/stats/detail/109), 1987 onward.
OFFICIAL_MONEY_STAT_ID = "109"


class ParseError(ValueError):
    """A response body does not have the shape the ingest parser expects."""


@dataclass(frozen=True)
class Endpoint:
    """One data family: how to request it, and what to know about the response."""

    family: str
    method: str
    url: str
    query: str | None
    notes: str


_SCHEDULE_QUERY = (
    "query Schedule($tourCode: String!, $year: String) { schedule(tourCode: $tourCode, "
    "year: $year) { tour seasonYear completed { month year monthSort tournaments { id "
    "tournamentName date startDate sortDate sequenceNumber city state country courseName "
    "champion championId champions { displayName playerId } championEarnings purse "
    "tournamentStatus tourStandingHeading tourStandingValue tournamentCategoryInfo { type "
    "label } } } upcoming { month year monthSort tournaments { id tournamentName date "
    "startDate sortDate sequenceNumber city state country courseName champion championId "
    "champions { displayName playerId } championEarnings purse tournamentStatus "
    "tourStandingHeading tourStandingValue tournamentCategoryInfo { type label } } } } }"
)

_TOURNAMENT_PAST_RESULTS_QUERY = (
    "query TournamentPastResults($tournamentPastResultsId: ID!, $year: Int) { "
    "tournamentPastResults(id: $tournamentPastResultsId, year: $year) { id players { id "
    "position player { id firstName lastName shortName displayName amateur country "
    "countryFlag } rounds { score parRelativeScore } additionalData total parRelativeScore "
    "} teams { teamId position players { id firstName lastName shortName displayName "
    "amateur country countryFlag } additionalData total parRelativeScore rounds { score "
    "parRelativeScore } } rounds additionalDataHeaders availableSeasons { year "
    "displaySeason } winner { id firstName lastName totalStrokes totalScore countryFlag "
    "countryName purse displayPoints displayPurse points seed pointsLabel } winningTeam { "
    "id firstName lastName totalStrokes totalScore countryFlag countryName purse "
    "displayPoints displayPurse points seed pointsLabel } } }"
)

_STAT_OVERVIEW_QUERY = (
    "query StatOverview($tourCode: TourCode!, $year: Int) { statOverview(tourCode: "
    "$tourCode, year: $year) { tourCode year categories { category displayName "
    "subCategories { displayName stats { statId statTitle } } categoryType } stats { "
    "statName tourAvg statId players { statId playerId statTitle statValue playerName rank "
    "country countryFlag } } } }"
)

# Shared by season_stat, money_list, fedex_bonus and owgr: only the statId variable differs.
_STAT_DETAILS_QUERY = (
    "query StatDetails($tourCode: TourCode!, $statId: String!, $year: Int, $eventQuery: "
    "StatDetailEventQuery) { statDetails(tourCode: $tourCode, statId: $statId, year: "
    "$year, eventQuery: $eventQuery) { __typename tourCode year displaySeason statId "
    "statType tournamentPills { tournamentId displayName } yearPills { year displaySeason "
    "} statTitle statDescription tourAvg lastProcessed statHeaders rows { ... on "
    "StatDetailsPlayer { __typename playerId playerName country countryFlag rank rankDiff "
    "rankChangeTendency stats { statName statValue color supportingData "
    "supportingDataColor } } ... on StatDetailTourAvg { __typename displayName value } } } "
    "}"
)

_PLAYER_DIRECTORY_QUERY = (
    "query PlayerDirectory($tourCode: TourCode!, $active: Boolean) { "
    "playerDirectory(tourCode: $tourCode, active: $active) { tourCode players { id "
    "isActive firstName lastName shortName displayName alphaSort country countryFlag "
    "playerBio { id education turnedPro age } } } }"
)

_PLAYER_QUERY = (
    "query Player($playerId: ID!) { player(id: $playerId) { id firstName lastName "
    "displayName country countryFlag owgr rank { rank statName } playerBio { "
    "heightImperial heightMeters weightImperial weightKilograms age born birthplace { city "
    "state stateCode country countryCode } turnedPro school degree graduationYear "
    "residence { city state stateCode country countryCode } playsFrom { city state "
    "stateCode country countryCode } family personal careerEarnings pronunciation "
    "exemptions { tour description expirationDate } deceased deceasedDate primaryTour } } }"
)


def _endpoint(family: str, query: str, notes: str) -> Endpoint:
    return Endpoint(family=family, method="POST", url=GRAPHQL_URL, query=query, notes=notes)


ENDPOINTS: dict[str, Endpoint] = {
    e.family: e
    for e in (
        _endpoint(
            "schedule",
            _SCHEDULE_QUERY,
            'Schedule; variables tourCode "R", year as a String season ("2019"). Months under '
            "completed/upcoming hold tournaments. Earliest season 2012. No pagination.",
        ),
        _endpoint(
            "event_results",
            _TOURNAMENT_PAST_RESULTS_QUERY,
            "TournamentPastResults; variables tournamentPastResultsId (any id of the "
            "tournament) and year as an Int season*10+n (20240); a plain 1995 returns the "
            "latest edition. Team events fill teams[] and leave players[] empty.",
        ),
        _endpoint(
            "stat_catalog",
            _STAT_OVERVIEW_QUERY,
            'StatOverview; variables tourCode "R", year as an Int season. Dedupe stats by '
            "statId: a stat appears under several subcategories.",
        ),
        _endpoint(
            "season_stat",
            _STAT_DETAILS_QUERY,
            'StatDetails; variables tourCode "R", statId, year as an Int season, eventQuery '
            "null. All rows in one response; rows are a union, guard on __typename. "
            "SG: Total (02675) starts in 2004.",
        ),
        _endpoint(
            "money_list",
            _STAT_DETAILS_QUERY,
            f"StatDetails with statId {OFFICIAL_MONEY_STAT_ID} (Official Money), 1987 onward. "
            "Values are strings like $29,228,357.",
        ),
        _endpoint(
            "player_list",
            _PLAYER_DIRECTORY_QUERY,
            'PlayerDirectory; variables tourCode "R", active null. Current members only; '
            "historical players appear only in results and stat rows.",
        ),
        _endpoint(
            "player_bio",
            _PLAYER_QUERY,
            "Player; variable playerId. Names, country, current owgr and playerBio.",
        ),
        _endpoint(
            "fedex_bonus",
            _STAT_DETAILS_QUERY,
            "StatDetails with statId 02396 (FedExCup Bonus Money), 2007 onward; no dedicated "
            "query. Values have no $ sign; ties share a rank.",
        ),
        _endpoint(
            "owgr",
            _STAT_DETAILS_QUERY,
            "StatDetails with statId 186 (Official World Golf Ranking, season end), 1995 "
            "onward; no dedicated query.",
        ),
    )
}

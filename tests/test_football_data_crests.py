from data.providers import football_data


def test_crest_from_team_id_builds_url():
    assert football_data._crest_from_team_id(64) == "https://crests.football-data.org/64.png"
    assert football_data._crest_from_team_id(None) is None


def test_get_team_crest_falls_back_to_team_id_url(monkeypatch):
    monkeypatch.setattr(football_data, "_get", lambda path, params=None: {"crest": None})
    assert football_data.get_team_crest(99) == "https://crests.football-data.org/99.png"


def test_get_upcoming_matches_applies_team_id_fallback(monkeypatch):
    monkeypatch.setattr(
        football_data,
        "_get",
        lambda path, params=None: {
            "matches": [
                {
                    "utcDate": "2026-02-21T12:00:00Z",
                    "homeTeam": {"id": 1, "name": "A", "crest": None},
                    "awayTeam": {"id": 2, "name": "B", "crest": None},
                }
            ]
        },
    )

    out = football_data.get_upcoming_matches("PL")

    assert out[0]["home_crest"] == "https://crests.football-data.org/1.png"
    assert out[0]["away_crest"] == "https://crests.football-data.org/2.png"
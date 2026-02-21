from daily import refresh


def test_fill_missing_crests_uses_existing_cache_and_payload():
    matches = [
        {
            "home": "Team A",
            "away": "Team B",
            "home_team_id": 1,
            "away_team_id": 2,
            "home_crest": "https://img/a.png",
            "away_crest": None,
            "utcDate": "2026-02-21T12:00:00Z",
        }
    ]
    cache = {"id:2": "https://img/b.png"}

    out = refresh._fill_missing_crests(matches, cache)

    assert out[0]["home_crest"] == "https://img/a.png"
    assert out[0]["away_crest"] == "https://img/b.png"
    assert cache["id:1"] == "https://img/a.png"


def test_fill_missing_crests_fetches_provider_when_needed(monkeypatch):
    matches = [
        {
            "home": "Team C",
            "away": "Team D",
            "home_team_id": 3,
            "away_team_id": 4,
            "home_crest": None,
            "away_crest": None,
            "utcDate": "2026-02-22T12:00:00Z",
        }
    ]
    cache = {}

    def fake_get_team_crest(team_id):
        return {3: "https://img/c.png", 4: "https://img/d.png"}.get(team_id)

    monkeypatch.setattr(refresh, "get_team_crest", fake_get_team_crest)

    out = refresh._fill_missing_crests(matches, cache)

    assert out[0]["home_crest"] == "https://img/c.png"
    assert out[0]["away_crest"] == "https://img/d.png"
    assert cache["id:3"] == "https://img/c.png"
    assert cache["id:4"] == "https://img/d.png"
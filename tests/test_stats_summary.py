from app.routes import picks as picks_route


def test_compute_metrics_with_win_loss_push_pending():
    rows = [
        {"result": "WIN", "best_fair": 2.0, "best_prob": 0.5},
        {"result": "LOSS", "best_fair": 2.0, "best_prob": 0.5},
        {"result": "PUSH", "best_fair": None, "best_prob": None},
        {"result": "PENDING", "best_fair": None, "best_prob": None},
    ]

    out = picks_route._compute_metrics(rows)

    assert out["total_picks"] == 4
    assert out["wins"] == 1
    assert out["losses"] == 1
    assert out["push"] == 1
    assert out["pending"] == 1
    assert out["winrate"] == 50.0
    assert out["roi"] == 0.0
    assert out["yield"] == 0.0
    assert out["net_units"] == 0.0


def test_compute_metrics_implied_odds_when_fair_missing():
    rows = [
        {"result": "WIN", "best_fair": None, "best_prob": 0.5},
        {"result": "LOSS", "best_fair": None, "best_prob": 0.5},
    ]

    out = picks_route._compute_metrics(rows)

    assert out["net_units"] == 0.0
    assert out["winrate"] == 50.0


def test_get_stats_summary_uses_json_fallback_source(monkeypatch):
    monkeypatch.setattr(picks_route, "_read_pick_rows", lambda league, db_path: ([], "sqlite"))
    monkeypatch.setattr(picks_route, "read_json", lambda path: [{"id": 1}, {"id": 2}, {"id": 3}])

    out = picks_route.get_stats_summary("PL")

    assert out["source"] == "json"
    assert out["total_picks"] == 3
    assert out["pending"] == 3


def test_get_stats_summary_marks_json_source_even_without_valid_list(monkeypatch):
    monkeypatch.setattr(picks_route, "_read_pick_rows", lambda league, db_path: ([], "sqlite"))
    monkeypatch.setattr(picks_route, "read_json", lambda path: {"unexpected": True})

    out = picks_route.get_stats_summary("PL")

    assert out["source"] == "json"
    assert out["total_picks"] == 0
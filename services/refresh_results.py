"""
Evaluación de resultados, merge por match_id, historial y ventana diaria.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

from core.evaluation import evaluate_market
from data.cache import backup_current_to_prev, write_json
from services.refresh_utils import _safe_int, _parse_utcdate_str, _read_json_list


# -------------------------
# Lookups de resultados
# -------------------------

def _build_finished_lookup_by_id(finished_matches: list[dict]) -> dict[int, tuple[int, int]]:
    """Arma lookup {match_id: (home_goals, away_goals)} desde partidos finalizados de la API."""
    lookup: dict[int, tuple[int, int]] = {}
    for m in finished_matches or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("match_id")
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        if mid is None or hg is None or ag is None:
            continue
        try:
            lookup[int(mid)] = (int(hg), int(ag))
        except Exception:
            continue
    return lookup


def _scores_lookup_from_match_list(matches: list[dict]) -> dict[int, tuple[int, int]]:
    """Arma lookup de goles desde daily_matches normalizados (score dict o home_goals)."""
    lookup: dict[int, tuple[int, int]] = {}
    for m in matches or []:
        if not isinstance(m, dict):
            continue
        mid = _safe_int(m.get("match_id") or m.get("id"))
        if mid is None:
            continue
        h: Any = None
        a: Any = None
        sc = m.get("score")
        if isinstance(sc, dict):
            h, a = sc.get("home"), sc.get("away")
        if h is None and m.get("home_goals") is not None:
            h, a = m.get("home_goals"), m.get("away_goals")
        if h is None or a is None:
            continue
        try:
            lookup[mid] = (int(h), int(a))
        except Exception:
            continue
    return lookup


def _apply_results_by_match_id(
    picks: list[dict], finished_by_id: dict[int, tuple[int, int]]
) -> list[dict]:
    """Aplica resultados (WIN/LOSS/PUSH) a picks según scores finales."""
    for p in picks or []:
        if not isinstance(p, dict):
            continue

        mid = p.get("match_id")
        if mid is None:
            continue

        try:
            mid_i = int(mid)
        except Exception:
            continue

        if mid_i not in finished_by_id:
            continue

        hg, ag = finished_by_id[mid_i]
        p["score_home"] = int(hg)
        p["score_away"] = int(ag)

        # Siempre re-evaluar cuando el partido está confirmado FINISHED.
        # Evita que resultados parciales (de live) queden como definitivos.
        market = (p.get("best_market") or "").strip()
        result, _reason = evaluate_market(market, hg, ag)
        p["result"] = result

    # Second pass: fix stale PUSH picks whose market was set after evaluation
    _reevaluate_stale_push(picks)

    return picks


def _reevaluate_stale_push(picks: list[dict]) -> list[dict]:
    """
    Re-evalúa picks que tienen score final cargado pero result=='PUSH' y market conocido.
    Ocurre cuando best_market se pobló después de que el partido salió de la ventana
    de finished_by_id, dejando el resultado incorrecto.
    """
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        if p.get("result") != "PUSH":
            continue
        market = (p.get("best_market") or "").strip()
        if not market:
            continue
        hg = p.get("score_home")
        ag = p.get("score_away")
        if hg is None or ag is None:
            continue
        try:
            result, _reason = evaluate_market(market, int(hg), int(ag))
        except Exception:
            continue
        # Only override if we get a definitive answer (not another PUSH)
        if result != "PUSH":
            p["result"] = result
    return picks


def _apply_live_scores_only(
    picks: list[dict], live_scores: dict[int, tuple[int, int]]
) -> list[dict]:
    """
    Actualiza score_home/score_away en picks con scores parciales de un partido en vivo.
    NO evalúa resultado (WIN/LOSS) — eso solo ocurre cuando el partido está FINISHED.
    """
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        mid = p.get("match_id")
        if mid is None:
            continue
        try:
            mid_i = int(mid)
        except Exception:
            continue
        if mid_i not in live_scores:
            continue
        hg, ag = live_scores[mid_i]
        p["score_home"] = int(hg)
        p["score_away"] = int(ag)
    return picks


# -------------------------
# Merge por match_id
# -------------------------

def _merge_by_match_id(existing: list[dict], new: list[dict]) -> list[dict]:
    """
    Combina dos listas de dicts por match_id (o id).
    Los items de `new` tienen precedencia sobre `existing`.
    """
    by_id: dict[int, dict] = {}

    for item in existing or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("match_id") or item.get("id")
        mid_i = _safe_int(mid)
        if mid_i is None:
            continue
        by_id[mid_i] = item

    for item in new or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("match_id") or item.get("id")
        mid_i = _safe_int(mid)
        if mid_i is None:
            continue
        by_id[mid_i] = item

    return list(by_id.values())


# -------------------------
# Historial y ventana diaria
# -------------------------

def _save_history(league_code: str, picks: list[dict]) -> None:
    """Guarda historial eterno de picks por liga (merge acumulativo)."""
    hist_file = f"picks_history_{league_code}.json"
    history = _read_json_list(hist_file)
    # Sanity: si un pick tiene result WIN/LOSS/PUSH pero sin score, resetear a PENDING
    # para que el próximo ciclo lo re-evalúe correctamente con el score real.
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        r = (p.get("result") or "").strip().upper()
        if r in ("WIN", "LOSS", "PUSH"):
            if p.get("score_home") is None or p.get("score_away") is None:
                p["result"] = "PENDING"
    merged = _merge_by_match_id(history, picks)
    write_json(hist_file, merged)


def _window_daily(picks: list[dict], keep_days: int | None) -> list[dict]:
    """
    Filtra picks para el cache diario:
    - Siempre incluye PENDING (sin importar edad)
    - Para settled (WIN/LOSS/PUSH): solo incluye los de los últimos `keep_days` días
    """
    try:
        kd = int(keep_days) if keep_days is not None else None
    except Exception:
        kd = None

    if not kd or kd <= 0:
        return picks

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=kd)

    out: list[dict] = []
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        r = (p.get("result") or "").strip().upper()
        if r == "PENDING":
            out.append(p)
            continue
        dt = _parse_utcdate_str(p.get("utcDate"))
        if dt >= cutoff:
            out.append(p)
    return out


# -------------------------
# Helpers de I/O de cache
# -------------------------

def _write_league_cache(league_code: str, matches: list[dict], picks: list[dict]) -> None:
    """Guarda matches y picks al cache con backup previo."""
    backup_current_to_prev(f"daily_matches_{league_code}.json")
    write_json(f"daily_matches_{league_code}.json", matches)
    backup_current_to_prev(f"daily_picks_{league_code}.json")
    write_json(f"daily_picks_{league_code}.json", picks)

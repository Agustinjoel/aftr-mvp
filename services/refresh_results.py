"""
Evaluación de resultados, merge por match_id, historial y ventana diaria.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any

import json as _json

from core.evaluation import evaluate_market
from data.cache import backup_current_to_prev, write_json, CACHE_DIR
from services.refresh_utils import _safe_int, _parse_utcdate_str, _read_json_list


# -------------------------
# Lookups de resultados
# -------------------------

# Statuses que confirman que un partido terminó definitivamente
_FINAL_STATUSES: frozenset[str] = frozenset({
    "FINISHED", "FT", "FINAL", "AWARDED", "FINALIZADO",
    "AET", "PEN", "FT_PEN",
})


def _build_finished_lookup_from_cache(matches: list[dict]) -> dict[int, tuple[int, int]]:
    """
    Arma lookup {match_id: (home_goals, away_goals)} desde el caché local de partidos.
    Solo incluye partidos con status definitivamente finalizado y scores válidos.
    Usado para resolver picks históricos fuera de la ventana de fetch de la API.
    """
    lookup: dict[int, tuple[int, int]] = {}
    for m in matches or []:
        if not isinstance(m, dict):
            continue
        status = (m.get("status") or "").upper()
        if status not in _FINAL_STATUSES:
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

        # Si no hay market pero el pick tiene probs guardados, re-derivar el mejor.
        # Ocurre cuando Model B produjo xG tan bajo que ningún mercado superó min_prob
        # y best_market quedó vacío. Usamos los probs almacenados para recuperarlo.
        if not market:
            stored_probs = p.get("probs")
            if isinstance(stored_probs, dict) and stored_probs:
                try:
                    from core.poisson import build_candidates, select_best_candidate
                    cands = build_candidates(stored_probs, min_prob=0.0)
                    best = select_best_candidate(cands)
                    if best and best.get("market"):
                        market = best["market"]
                        p["best_market"] = market
                except Exception:
                    pass

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

def _fix_push_with_score(picks: list[dict]) -> None:
    """
    In-place: re-evalúa picks que tienen result=PUSH pero tienen score y market
    (o probs almacenados). PUSH solo es válido para mercados genuinamente desconocidos;
    si el partido terminó y hay datos suficientes, debería ser WIN o LOSS.
    """
    from core.evaluation import evaluate_market as _eval
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        if (p.get("result") or "").strip().upper() != "PUSH":
            continue
        hg = p.get("score_home")
        ag = p.get("score_away")
        if hg is None or ag is None:
            continue
        try:
            hg, ag = int(hg), int(ag)
        except (TypeError, ValueError):
            continue

        market = (p.get("best_market") or "").strip()

        # Si no hay market, intentar derivarlo de probs almacenados
        if not market:
            stored_probs = p.get("probs")
            if isinstance(stored_probs, dict) and stored_probs:
                try:
                    from core.poisson import build_candidates, select_best_candidate
                    cands = build_candidates(stored_probs, min_prob=0.0)
                    best = select_best_candidate(cands)
                    if best and best.get("market"):
                        market = best["market"]
                        p["best_market"] = market
                except Exception:
                    pass

        if not market:
            continue

        result, _ = _eval(market, hg, ag)
        if result != "PUSH":
            p["result"] = result


def _save_history(league_code: str, picks: list[dict]) -> None:
    """Guarda historial eterno de picks por liga (merge acumulativo).

    El historial es MONOTÓNICO: una vez que un pick tiene WIN/LOSS, nunca
    vuelve a PENDING por un refresh que traiga datos incompletos.
    """
    hist_file = f"picks_history_{league_code}.json"
    history = _read_json_list(hist_file)
    merged = _merge_by_match_id(history, picks)
    # Picks que vienen como PENDING nunca deben sobrescribir WIN/LOSS ya
    # registrados. Esto cubre: ventana days_finished < edad del partido,
    # job LIVE con days_finished=1, race conditions entre jobs, etc.
    _restore_settled_picks(merged, history)
    # Fix: re-evaluar picks PUSH del historial que tienen score + market
    _fix_push_with_score(merged)
    write_json(hist_file, merged)


def _window_daily(picks: list[dict], keep_days: int | None) -> list[dict]:
    """
    Filtra picks para el cache diario:
    - Siempre incluye PENDING (sin importar edad)
    - Para settled (WIN/LOSS/PUSH): solo incluye los de los últimos `keep_days` días
    - Descarta picks PUSH sin market ni probs (basura irrecuperable de antes del fix Model B)
    """
    try:
        kd = int(keep_days) if keep_days is not None else 14
    except Exception:
        kd = 14

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=kd)

    out: list[dict] = []
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        r = (p.get("result") or "").strip().upper()

        # Descartar PUSH irrecuperables: sin market ni probs almacenados
        if r == "PUSH" and not (p.get("best_market") or p.get("probs")):
            continue

        if r == "PENDING":
            out.append(p)
            continue
        try:
            dt = _parse_utcdate_str(p.get("utcDate"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt >= cutoff:
                out.append(p)
        except Exception:
            out.append(p)  # si falla la comparación, incluir el pick
    return out


# -------------------------
# Helpers de I/O de cache
# -------------------------

def _write_league_cache(league_code: str, matches: list[dict], picks: list[dict]) -> None:
    """Guarda matches y picks al cache con backup previo.

    Antes de escribir daily_picks_*.json hace una lectura fresca del disco para
    preservar cualquier WIN/LOSS que haya escrito un job concurrente (ej: LIVE)
    entre el inicio del ciclo y este punto. Sin esto, el job RESULTS puede
    sobreescribir un WIN del job LIVE porque leyó existing_picks stale al inicio.
    """
    backup_current_to_prev(f"daily_matches_{league_code}.json")
    write_json(f"daily_matches_{league_code}.json", matches)

    picks_file = f"daily_picks_{league_code}.json"
    backup_current_to_prev(picks_file)
    # Leer el estado ACTUAL del disco DIRECTO (sin TTL cache) para capturar
    # cualquier WIN/LOSS escrito por un job concurrente (ej: LIVE thread) entre
    # el inicio de este ciclo y ahora.
    _p = CACHE_DIR / picks_file
    try:
        if _p.exists():
            _raw = _json.loads(_p.read_text(encoding="utf-8"))
            fresh = [x for x in _raw if isinstance(x, dict)] if isinstance(_raw, list) else []
        else:
            fresh = []
    except Exception:
        fresh = []
    if fresh:
        _restore_settled_picks(picks, fresh)
    write_json(picks_file, picks)


# -------------------------
# Settlement preservation
# -------------------------

def _restore_settled_picks(picks: list[dict], existing: list[dict]) -> None:
    """
    In-place: restores WIN/LOSS results from `existing` for any pick in `picks`
    that lost its settlement during the refresh cycle (e.g. due to fresh
    computation overwriting a previously settled pick, or a match falling
    outside the days_finished API window).

    Called as a final safety net after all merge/apply steps.
    """
    settled: dict[int, dict] = {}
    for p in existing or []:
        if not isinstance(p, dict):
            continue
        mid = _safe_int(p.get("match_id") or p.get("id"))
        if mid is None:
            continue
        r = (p.get("result") or "").upper()
        if r in ("WIN", "LOSS"):
            settled[mid] = p

    if not settled:
        return

    for p in picks or []:
        if not isinstance(p, dict):
            continue
        mid = _safe_int(p.get("match_id") or p.get("id"))
        if mid is None or mid not in settled:
            continue
        r = (p.get("result") or "PENDING").upper()
        if r in ("WIN", "LOSS"):
            continue  # already correctly settled, nothing to restore
        # Pick lost its settlement — restore ALL data from the settled pick
        # (not just result/score, but also best_market, best_prob, candidates, etc.)
        orig = settled[mid]
        p.update(orig)

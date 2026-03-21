"""
Pipeline de refresco único: obtener partidos → calcular picks (Poisson) → guardar en cache.
Punto de entrada para cron/run_daily.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any

from config.settings import settings
from core.evaluation import evaluate_market
from core.poisson import build_candidates, estimate_xg, match_probs, select_best_candidate
from core.model_b import estimate_xg_dynamic_split
from core.combos import build_global_combos

from data.cache import (
    CACHE_META_FILENAME,
    backup_current_to_prev,
    read_json,
    release_refresh_running_meta,
    write_cache_meta,
    write_json,
)
from data.providers.football_data import (
    UnsupportedCompetitionError,
    football_data_refresh_cycle,
    get_finished_matches,
    get_football_data_cycle_stats_snapshot,
    get_live_matches,
    get_upcoming_matches,
)
from data.providers.team_form import get_team_recent_matches

from core.odds import edge as odds_edge, get_decimal_and_implied_for_market
from services.aftr_score import enrich_pick_with_aftr_score, filter_premium_picks
from data.providers.odds_football import (
    ensure_odds_for_league,
    get_odds_for_match,
    match_odds_to_matches,
)

logger = logging.getLogger(__name__)

TEAM_NAMES_FILE = "team_names.json"
LEAGUE_REFRESH_STATE_FILE = "league_refresh_state.json"

# Single in-process refresh (CLI + auto); use non_blocking=True for auto scheduler.
_refresh_global_lock = threading.Lock()
_auto_rr_index = 0


@dataclass
class RefreshMetrics:
    """Aggregated during refresh_all (optional, passed into refresh_league)."""

    matches_updated: int = 0


@dataclass
class RefreshAllResult:
    ran: bool
    skipped_busy: bool = False
    light_mode: bool = False
    leagues_refreshed: int = 0
    leagues_skipped_fresh: int = 0
    football_http_requests: int = 0
    football_cache_hits: int = 0
    rate_limit_sleep_sec: int = 0
    matches_updated: int = 0


def _load_league_last_refresh() -> dict[str, str]:
    raw = read_json(LEAGUE_REFRESH_STATE_FILE)
    if isinstance(raw, dict):
        inner = raw.get("last_ok")
        if isinstance(inner, dict):
            return {str(k): str(v) for k, v in inner.items() if v}
    return {}


def _save_league_last_refresh(updates: dict[str, str]) -> None:
    current = _load_league_last_refresh()
    current.update(updates)
    write_json(LEAGUE_REFRESH_STATE_FILE, {"last_ok": current})


def _parse_iso_utc(s: str) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        if s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _league_is_fresh(code: str, last_ok: dict[str, str], min_minutes: int) -> bool:
    if min_minutes <= 0:
        return False
    iso = last_ok.get(code)
    dt = _parse_iso_utc(iso or "")
    if dt is None:
        return False
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age < (min_minutes * 60)


# -------------------------
# Helpers generales
# -------------------------
def _parse_utcdate(m: dict) -> datetime:
    s = (m or {}).get("utcDate") or ""
    try:
        if isinstance(s, str) and s.endswith("Z"):
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.now(timezone.utc)


def _parse_utcdate_str(s: Any) -> datetime:
    try:
        if isinstance(s, str) and s:
            if s.endswith("Z"):
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            return datetime.fromisoformat(s)
    except Exception:
        pass
    return datetime.now(timezone.utc)


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _safe_int(x: Any, default: int | None = None) -> int | None:
    try:
        if x is None:
            return default
        return int(x)
    except Exception:
        return default


def _best_prob(p: dict) -> float:
    try:
        return float((p or {}).get("best_prob") or 0.0)
    except Exception:
        return 0.0


def _read_json_list(filename: str) -> list[dict]:
    data = read_json(filename)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def _normalize_match(m: dict) -> dict:
    """
    Deja el match con:
    - home_crest/away_crest
    - status
    - score: {home, away} (si hay goles disponibles, sino None/None)
    - sport: "football" | "basketball" (preserved or default football)
    """
    out = dict(m) if isinstance(m, dict) else {}

    out.setdefault("home_crest", None)
    out.setdefault("away_crest", None)
    out.setdefault("status", out.get("status") or "TIMED")
    out.setdefault("sport", "football")

    sc = out.get("score")
    if isinstance(sc, dict) and ("home" in sc or "away" in sc):
        out["score"] = {"home": sc.get("home"), "away": sc.get("away")}
        return out

    hg = out.get("home_goals", None)
    ag = out.get("away_goals", None)
    if hg is not None and ag is not None:
        try:
            out["score"] = {"home": int(hg), "away": int(ag)}
        except Exception:
            out["score"] = {"home": hg, "away": ag}
    else:
        out["score"] = {"home": None, "away": None}

    return out


# -------------------------
# Team names cache + crests
# -------------------------
def _load_team_names_cache() -> dict[int, str]:
    raw = read_json(TEAM_NAMES_FILE)
    if isinstance(raw, dict):
        out: dict[int, str] = {}
        for k, v in raw.items():
            try:
                out[int(k)] = str(v)
            except Exception:
                pass
        return out
    return {}


def _save_team_names_cache(cache: dict[int, str]) -> None:
    out = {str(k): str(v) for k, v in (cache or {}).items()}
    write_json(TEAM_NAMES_FILE, out)


def _crest_from_id(team_id: int | None) -> str | None:
    if not team_id:
        return None
    try:
        return f"https://crests.football-data.org/{int(team_id)}.png"
    except Exception:
        return None


def _update_team_names_from_matches(team_names: dict[int, str], matches: list[dict]) -> None:
    for m in matches or []:
        if not isinstance(m, dict):
            continue
        hid = m.get("home_team_id")
        aid = m.get("away_team_id")
        hname = m.get("home")
        aname = m.get("away")
        try:
            if hid and hname:
                team_names[int(hid)] = str(hname)
            if aid and aname:
                team_names[int(aid)] = str(aname)
        except Exception:
            continue


# -------------------------
# Team stats (from recent matches)
# -------------------------
def _result_letter_from_goals(gf: int, ga: int) -> str:
    if gf > ga:
        return "W"
    if gf < ga:
        return "L"
    return "D"


def _calc_team_stats_from_recent(team_id: int, recent: list[dict]) -> dict:
    """
    recent (tu provider):
      { utcDate, home_id, away_id, home_goals, away_goals }
    """
    if not recent:
        return {"gf": "—", "ga": "—", "form": "—", "over25": "—", "btts": "—", "n": 0}

    gf_total = 0
    ga_total = 0
    over25_cnt = 0
    btts_cnt = 0
    letters: list[str] = []
    n = 0

    for m in recent:
        if not isinstance(m, dict):
            continue

        hid = m.get("home_id")
        aid = m.get("away_id")
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        if hid is None or aid is None or hg is None or ag is None:
            continue

        try:
            hid_i = int(hid)
            aid_i = int(aid)
            hg_i = int(hg)
            ag_i = int(ag)
        except Exception:
            continue

        if team_id == hid_i:
            gf, ga = hg_i, ag_i
        elif team_id == aid_i:
            gf, ga = ag_i, hg_i
        else:
            continue

        n += 1
        gf_total += gf
        ga_total += ga

        letters.append(_result_letter_from_goals(gf, ga))

        if (hg_i + ag_i) >= 3:
            over25_cnt += 1
        if hg_i > 0 and ag_i > 0:
            btts_cnt += 1

    if n == 0:
        return {"gf": "—", "ga": "—", "form": "—", "over25": "—", "btts": "—", "n": 0}

    gf_avg = round(gf_total / n, 2)
    ga_avg = round(ga_total / n, 2)
    form = " ".join(letters[:5]) if letters else "—"

    return {
        "gf": gf_avg,
        "ga": ga_avg,
        "form": form,
        "over25": round(over25_cnt / n, 2),
        "btts": round(btts_cnt / n, 2),
        "n": n,
    }


def _build_recent_compact(
    team_id: int,
    recent_raw: list[dict],
    team_names: dict[int, str],
    n: int = 3,
) -> list[dict]:
    """
    Output (últimos N):
    {
      utcDate, is_home, opp_id, opp_name, opp_crest,
      gf, ga, res
    }
    """
    if not recent_raw or not isinstance(recent_raw, list):
        return []

    out: list[dict] = []

    for m in recent_raw:
        if not isinstance(m, dict):
            continue

        hid = m.get("home_id")
        aid = m.get("away_id")
        hg = m.get("home_goals")
        ag = m.get("away_goals")
        utc = m.get("utcDate", "")

        if hid is None or aid is None or hg is None or ag is None:
            continue

        try:
            hid_i = int(hid)
            aid_i = int(aid)
            hg_i = int(hg)
            ag_i = int(ag)
        except Exception:
            continue

        is_home = (hid_i == team_id)
        if not is_home and aid_i != team_id:
            continue

        if is_home:
            gf, ga = hg_i, ag_i
            opp_id = aid_i
        else:
            gf, ga = ag_i, hg_i
            opp_id = hid_i

        res = _result_letter_from_goals(gf, ga)
        opp_name = team_names.get(opp_id) or f"Equipo {opp_id}"
        opp_crest = _crest_from_id(opp_id)

        out.append(
            {
                "utcDate": utc,
                "is_home": is_home,
                "opp_id": opp_id,
                "opp_name": opp_name,
                "opp_crest": opp_crest,
                "gf": gf,
                "ga": ga,
                "res": res,
            }
        )

        if len(out) >= int(n):
            break

    return out


# -------------------------
# Candidates: top2 + confidence
# -------------------------
def _top2_from_candidates(
    candidates: list[dict],
) -> tuple[str | None, float | None, str | None, float | None]:
    if not candidates:
        return None, None, None, None

    ordered = sorted(
        [c for c in candidates if isinstance(c, dict)],
        key=lambda c: _safe_float(c.get("prob")),
        reverse=True,
    )

    best = ordered[0] if len(ordered) >= 1 else None
    second = ordered[1] if len(ordered) >= 2 else None

    best_m = best.get("market") if best else None
    best_p = _safe_float(best.get("prob")) if best and best.get("prob") is not None else None

    sec_m = second.get("market") if second else None
    sec_p = _safe_float(second.get("prob")) if second and second.get("prob") is not None else None

    return best_m, best_p, sec_m, sec_p


def _confidence_score(
    best_prob: float | None,
    second_prob: float | None,
    xg_total: float,
    model: str,
) -> int:
    bp = float(best_prob or 0.0)
    sp = float(second_prob or 0.0)
    edge = max(0.0, bp - sp)

    score = 0.0
    score += bp * 7.0
    score += edge * 12.0

    if xg_total < 1.8:
        score -= 1.0
    elif xg_total > 3.2:
        score += 0.5

    if (model or "").strip().upper() == "B":
        score += 0.6

    score_i = int(round(score))
    return max(1, min(10, score_i))


# -------------------------
# Picks builder (A + topN B) — football only
# -------------------------
def _build_picks_from_matches(matches: list[dict], team_names: dict[int, str]) -> list[dict]:
    if not matches:
        return []

    sorted_matches = sorted(matches, key=_parse_utcdate)

    topn_b = int(getattr(settings, "refresh_topn_model_b", 10) or 10)
    days_back = int(getattr(settings, "team_form_days_back", 30) or 30)
    limit = int(getattr(settings, "team_form_limit", 10) or 10)

    team_recent_cache: dict[int, list[dict]] = {}

    def _tm(team_id: int) -> list[dict]:
        if team_id not in team_recent_cache:
            team_recent_cache[team_id] = get_team_recent_matches(
                team_id, days_back=days_back, limit=limit
            )
        return team_recent_cache[team_id]

    picks: list[dict] = []

    # 1) Todos con modelo A
    for m in sorted_matches:
        xg_h = float(settings.default_xg_home)
        xg_a = float(settings.default_xg_away)

        try:
            axg_h, axg_a = estimate_xg(
                m,
                default_home=settings.default_xg_home,
                default_away=settings.default_xg_away,
            )
            xg_h, xg_a = float(axg_h), float(axg_a)
        except Exception:
            pass

        probs = match_probs(xg_h, xg_a, max_goals=settings.max_goals_poisson)
        candidates = build_candidates(probs, min_prob=settings.min_prob_for_candidate)
        best = select_best_candidate(candidates)

        best_market, best_prob, second_market, second_prob = _top2_from_candidates(candidates)
        if best_market is None and best:
            best_market = best.get("market")
        if best_prob is None and best and best.get("prob") is not None:
            best_prob = _safe_float(best.get("prob"))

        best_fair = best.get("fair") if best and best.get("prob") else None
        if best_fair is None and best_prob and best_prob > 0:
            best_fair = round(1.0 / float(best_prob), 2)

        xg_total = float(xg_h + xg_a)

        edge_val: float | None = None
        if best_prob is not None and second_prob is not None:
            edge_val = float(best_prob) - float(second_prob)

        conf = _confidence_score(best_prob, second_prob, xg_total, "A")

        # --- Team stats + recent (últimos 3) ---
        stats_home: dict = {}
        stats_away: dict = {}

        hid = m.get("home_team_id")
        aid = m.get("away_team_id")

        try:
            if hid:
                hm = _tm(int(hid))
                base = _calc_team_stats_from_recent(int(hid), hm)
                base["recent"] = _build_recent_compact(int(hid), hm, team_names, n=3)
                stats_home = base

            if aid:
                am = _tm(int(aid))
                base = _calc_team_stats_from_recent(int(aid), am)
                base["recent"] = _build_recent_compact(int(aid), am, team_names, n=3)
                stats_away = base
        except Exception as e:
            logger.warning("Team stats error (%s vs %s): %s", hid, aid, e)
            stats_home = {}
            stats_away = {}

        picks.append(
            {
                "match_id": m.get("match_id"),
                "utcDate": m.get("utcDate", ""),
                "home": m.get("home", ""),
                "away": m.get("away", ""),
                "home_crest": m.get("home_crest"),
                "away_crest": m.get("away_crest"),
                "home_team_id": m.get("home_team_id"),
                "away_team_id": m.get("away_team_id"),
                "xg_home": round(float(xg_h), 2),
                "xg_away": round(float(xg_a), 2),
                "xg_total": round(float(xg_total), 2),
                "model": "A",
                "probs": probs,
                "candidates": candidates,
                "best_market": best_market,
                "best_prob": best_prob,
                "best_fair": best_fair,
                "second_market": second_market,
                "second_prob": second_prob,
                "edge": round(float(edge_val), 4) if edge_val is not None else None,
                "confidence": conf,
                "result": "PENDING",
                "score_home": None,
                "score_away": None,
                "stats_home": stats_home,
                "stats_away": stats_away,
            }
        )

    if topn_b <= 0:
        return picks

    # 2) Top N por best_prob para recalcular con B
    ranked = sorted(enumerate(picks), key=lambda t: _best_prob(t[1]), reverse=True)
    top_idxs = [idx for idx, _p in ranked[:topn_b]]

    # 3) Recalcular esos con modelo B
    for idx in top_idxs:
        p = picks[idx]
        hid = p.get("home_team_id")
        aid = p.get("away_team_id")
        if not hid or not aid:
            continue

        try:
            hm = _tm(int(hid))
            am = _tm(int(aid))
            xg_h, xg_a = estimate_xg_dynamic_split(int(hid), int(aid), hm, am)

            probs = match_probs(xg_h, xg_a, max_goals=settings.max_goals_poisson)
            candidates = build_candidates(probs, min_prob=settings.min_prob_for_candidate)
            best = select_best_candidate(candidates)

            best_market, best_prob, second_market, second_prob = _top2_from_candidates(candidates)
            if best_market is None and best:
                best_market = best.get("market")
            if best_prob is None and best and best.get("prob") is not None:
                best_prob = _safe_float(best.get("prob"))

            best_fair = best.get("fair") if best and best.get("prob") else None
            if best_fair is None and best_prob and best_prob > 0:
                best_fair = round(1.0 / float(best_prob), 2)

            xg_total = float(xg_h + xg_a)

            edge_val: float | None = None
            if best_prob is not None and second_prob is not None:
                edge_val = float(best_prob) - float(second_prob)

            conf = _confidence_score(best_prob, second_prob, xg_total, "B")

            p.update(
                {
                    "xg_home": round(float(xg_h), 2),
                    "xg_away": round(float(xg_a), 2),
                    "xg_total": round(float(xg_total), 2),
                    "model": "B",
                    "probs": probs,
                    "candidates": candidates,
                    "best_market": best_market,
                    "best_prob": best_prob,
                    "best_fair": best_fair,
                    "second_market": second_market,
                    "second_prob": second_prob,
                    "edge": round(float(edge_val), 4) if edge_val is not None else None,
                    "confidence": conf,
                }
            )

        except Exception as e:
            logger.warning("Modelo B fallback a A (%s vs %s): %s", p.get("home"), p.get("away"), e)

    return picks


# -------------------------
# Odds enrichment (football only)
# -------------------------
def _enrich_football_picks_with_odds(
    league_code: str,
    matches: list[dict],
    picks: list[dict],
    *,
    debug_watch_keys: set[str] | None = None,
) -> list[dict]:
    """
    Attach odds_decimal, implied_prob, edge to each pick when odds are available.
    Match by (home, away, date).
    Notes:
    - When odds are refreshed, we avoid leaving stale odds fields behind:
      if a pick doesn't match any provider odds event, odds fields are cleared.
    - AFTR score + edge are recomputed later via `enrich_pick_with_aftr_score`.
    """
    if not picks:
        return picks

    def _pick_debug_key(p: dict) -> str:
        mid = p.get("match_id")
        if mid is None:
            mid = p.get("id")
        bm = (p.get("best_market") or "").strip()
        utc = str(p.get("utcDate") or "").strip()
        home = str(p.get("home") or "").strip()
        away = str(p.get("away") or "").strip()
        return f"{mid}|{bm}|{utc}|{home}|{away}"

    try:
        # Important: when we are refreshing picks with odds, we must fetch fresh provider data.
        # Otherwise `use_cache_first=True` can keep stale odds forever.
        odds_events = ensure_odds_for_league(league_code, matches, use_cache_first=False)
        if not odds_events:
            return picks
        odds_lookup = match_odds_to_matches(odds_events, matches)
    except Exception as e:
        logger.debug("Odds enrichment skipped for %s: %s", league_code, e)
        return picks

    for p in picks:
        if not isinstance(p, dict):
            continue

        watch = (debug_watch_keys is not None) and (_pick_debug_key(p) in debug_watch_keys)
        key_pick = _pick_debug_key(p) if watch else None

        old_odds_decimal = p.get("odds_decimal")
        old_implied_prob = p.get("implied_prob")
        old_edge = p.get("edge")

        match_placeholder = {
            "home": p.get("home"),
            "away": p.get("away"),
            "utcDate": p.get("utcDate"),
        }
        odds_row = get_odds_for_match(match_placeholder, odds_lookup)
        best_market = (p.get("best_market") or "").strip()

        if not odds_row or not best_market:
            # Prevent stale odds from previous cache loads.
            p.pop("odds_decimal", None)
            p.pop("implied_prob", None)
            p.pop("bookmaker_title", None)
            p.pop("edge", None)
            if watch:
                logger.info(
                    "ODDS DEBUG clear | league=%s pick=%s home=%s away=%s utcDate=%s best_market=%s | old_odds_decimal=%s old_implied_prob=%s old_edge=%s | provider_match=%s",
                    league_code,
                    key_pick,
                    p.get("home"),
                    p.get("away"),
                    p.get("utcDate"),
                    best_market,
                    old_odds_decimal,
                    old_implied_prob,
                    old_edge,
                    "NONE",
                )
            continue

        decimal_odds, implied_prob = get_decimal_and_implied_for_market(odds_row, best_market)
        if decimal_odds is not None and implied_prob is not None:
            new_odds_decimal = round(float(decimal_odds), 2)
            if watch:
                logger.info(
                    "ODDS DEBUG update | league=%s pick=%s home=%s away=%s utcDate=%s best_market=%s | old_odds_decimal=%s old_implied_prob=%s old_edge=%s | provider_decimal=%s provider_implied_prob=%s bookmaker=%s",
                    league_code,
                    key_pick,
                    p.get("home"),
                    p.get("away"),
                    p.get("utcDate"),
                    best_market,
                    old_odds_decimal,
                    old_implied_prob,
                    old_edge,
                    decimal_odds,
                    implied_prob,
                    (odds_row.get("bookmaker_title") or "").strip(),
                )
            p["odds_decimal"] = new_odds_decimal
            p["implied_prob"] = implied_prob
            aftr_prob = _safe_float(p.get("best_prob"))
            if aftr_prob is not None:
                e = odds_edge(aftr_prob, implied_prob)
                if e is not None:
                    p["edge"] = e
            bookmaker_title = (odds_row.get("bookmaker_title") or "").strip()
            if bookmaker_title:
                p["bookmaker_title"] = bookmaker_title
        else:
            # Provider match exists, but we couldn't map the pick market => clear stale odds.
            p.pop("odds_decimal", None)
            p.pop("implied_prob", None)
            p.pop("bookmaker_title", None)
            p.pop("edge", None)
            if watch:
                logger.info(
                    "ODDS DEBUG clear-mapping | league=%s pick=%s home=%s away=%s utcDate=%s best_market=%s | old_odds_decimal=%s old_implied_prob=%s old_edge=%s | provider_decimal=%s provider_implied_prob=%s",
                    league_code,
                    key_pick,
                    p.get("home"),
                    p.get("away"),
                    p.get("utcDate"),
                    best_market,
                    old_odds_decimal,
                    old_implied_prob,
                    old_edge,
                    decimal_odds,
                    implied_prob,
                )
    return picks


# -------------------------
# Finished => result
# -------------------------
def _build_finished_lookup_by_id(finished_matches: list[dict]) -> dict[int, tuple[int, int]]:
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


def _refresh_league_live_only(
    league_code: str,
    metrics: RefreshMetrics | None,
) -> tuple[int, int]:
    try:
        raw_live = get_live_matches(league_code)
    except UnsupportedCompetitionError:
        return 0, 0
    except Exception as e:
        logger.warning("live refresh %s: %s", league_code, e)
        return 0, 0
    if not raw_live:
        return 0, 0
    team_names = _load_team_names_cache()
    live_matches = [_normalize_match(m) for m in raw_live]
    _update_team_names_from_matches(team_names, live_matches)
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    merged_matches = _merge_by_match_id(existing_matches, live_matches)
    backup_current_to_prev(f"daily_matches_{league_code}.json")
    write_json(f"daily_matches_{league_code}.json", merged_matches)

    partial_by_id = _scores_lookup_from_match_list(live_matches)
    if partial_by_id:
        existing_picks = _read_json_list(f"daily_picks_{league_code}.json")
        picks_all = _apply_results_by_match_id(existing_picks, partial_by_id)
        for p in picks_all:
            if isinstance(p, dict):
                enrich_pick_with_aftr_score(p)
        keep_days = getattr(settings, "daily_keep_days", None)
        picks_daily = _window_daily(picks_all, keep_days)
        backup_current_to_prev(f"daily_picks_{league_code}.json")
        write_json(f"daily_picks_{league_code}.json", picks_daily)
        _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)
    if metrics is not None:
        metrics.matches_updated += len(merged_matches)
    return len(raw_live), len(merged_matches)


def _refresh_league_upcoming_only(
    league_code: str,
    fetch_odds: bool,
    metrics: RefreshMetrics | None,
) -> tuple[int, int]:
    try:
        raw_upcoming = get_upcoming_matches(league_code)
        for m in raw_upcoming:
            m["sport"] = "football"
    except UnsupportedCompetitionError:
        return 0, 0

    team_names = _load_team_names_cache()
    upcoming_matches = [_normalize_match(m) for m in (raw_upcoming or [])]
    _update_team_names_from_matches(team_names, upcoming_matches)
    upcoming_picks = _build_picks_from_matches(upcoming_matches, team_names)
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    merged_matches = _merge_by_match_id(existing_matches, upcoming_matches)
    finished_by_id = _scores_lookup_from_match_list(merged_matches)
    merged_picks = _merge_by_match_id(existing_picks, upcoming_picks)
    picks_all = _apply_results_by_match_id(merged_picks, finished_by_id)
    if fetch_odds:
        odds_debug_enabled = bool(getattr(settings, "debug", False)) or str(os.getenv("AFTR_ODDS_DEBUG", "0")).lower() in (
            "1",
            "true",
            "yes",
        )
        odds_debug_samples = int(os.getenv("AFTR_ODDS_DEBUG_SAMPLES", "3"))
        debug_watch_keys: set[str] | None = None
        if odds_debug_enabled and odds_debug_samples > 0:
            debug_watch_keys = set()

            def _pick_debug_key(p: dict) -> str:
                mid = p.get("match_id")
                if mid is None:
                    mid = p.get("id")
                bm = (p.get("best_market") or "").strip()
                utc = str(p.get("utcDate") or "").strip()
                home = str(p.get("home") or "").strip()
                away = str(p.get("away") or "").strip()
                return f"{mid}|{bm}|{utc}|{home}|{away}"

            for p in picks_all:
                if not isinstance(p, dict):
                    continue
                k = _pick_debug_key(p)
                if not k:
                    continue
                debug_watch_keys.add(k)
                if len(debug_watch_keys) >= odds_debug_samples:
                    break

        picks_all = _enrich_football_picks_with_odds(
            league_code, merged_matches, picks_all, debug_watch_keys=debug_watch_keys
        )
    for p in picks_all:
        if isinstance(p, dict):
            enrich_pick_with_aftr_score(p)
    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)
    backup_current_to_prev(f"daily_matches_{league_code}.json")
    write_json(f"daily_matches_{league_code}.json", merged_matches)
    backup_current_to_prev(f"daily_picks_{league_code}.json")
    write_json(f"daily_picks_{league_code}.json", picks_daily)

    if fetch_odds:
        odds_debug_enabled = bool(getattr(settings, "debug", False)) or str(os.getenv("AFTR_ODDS_DEBUG", "0")).lower() in (
            "1",
            "true",
            "yes",
        )
        odds_debug_samples = int(os.getenv("AFTR_ODDS_DEBUG_SAMPLES", "3"))
        if odds_debug_enabled and odds_debug_samples > 0:
            # Read back what we actually wrote to disk (primary cache) to ensure odds updated end-to-end.
            def _pick_debug_key(p: dict) -> str:
                mid = p.get("match_id")
                if mid is None:
                    mid = p.get("id")
                bm = (p.get("best_market") or "").strip()
                utc = str(p.get("utcDate") or "").strip()
                home = str(p.get("home") or "").strip()
                away = str(p.get("away") or "").strip()
                return f"{mid}|{bm}|{utc}|{home}|{away}"

            saved = _read_json_list(f"daily_picks_{league_code}.json")
            saved_map = {_pick_debug_key(p): p for p in saved if isinstance(p, dict)}

            watch_keys = list(debug_watch_keys or [])
            for k in watch_keys:
                sp = saved_map.get(k) or {}
                logger.info(
                    "ODDS DEBUG saved | league=%s pick_key=%s home=%s away=%s best_market=%s | odds_decimal=%s implied_prob=%s edge=%s",
                    league_code,
                    k,
                    sp.get("home"),
                    sp.get("away"),
                    sp.get("best_market"),
                    sp.get("odds_decimal"),
                    sp.get("implied_prob"),
                    sp.get("edge"),
                )
    _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)
    if metrics is not None:
        metrics.matches_updated += len(merged_matches)
    return len(upcoming_matches), len(picks_daily)


def _refresh_league_results_only(
    league_code: str,
    finished_days_back: int,
    fetch_odds: bool,
    metrics: RefreshMetrics | None,
) -> tuple[int, int]:
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    team_names = _load_team_names_cache()
    try:
        finished_matches = get_finished_matches(league_code, days_back=finished_days_back)
        for m in finished_matches or []:
            m["sport"] = "football"
    except UnsupportedCompetitionError:
        return 0, 0
    except Exception as e:
        logger.warning("results refresh %s: %s", league_code, e)
        return 0, 0

    finished_by_id = _build_finished_lookup_by_id(finished_matches or [])
    finished_matches_norm = [_normalize_match(m) for m in (finished_matches or [])]
    _update_team_names_from_matches(team_names, finished_matches_norm)
    finished_picks = _build_picks_from_matches(finished_matches_norm, team_names)
    merged_matches = _merge_by_match_id(existing_matches, finished_matches_norm)
    merged_picks = _merge_by_match_id(existing_picks, finished_picks)
    picks_all = _apply_results_by_match_id(merged_picks, finished_by_id)
    if fetch_odds:
        odds_debug_enabled = bool(getattr(settings, "debug", False)) or str(os.getenv("AFTR_ODDS_DEBUG", "0")).lower() in (
            "1",
            "true",
            "yes",
        )
        odds_debug_samples = int(os.getenv("AFTR_ODDS_DEBUG_SAMPLES", "3"))
        debug_watch_keys: set[str] | None = None
        if odds_debug_enabled and odds_debug_samples > 0:
            debug_watch_keys = set()

            def _pick_debug_key(p: dict) -> str:
                mid = p.get("match_id")
                if mid is None:
                    mid = p.get("id")
                bm = (p.get("best_market") or "").strip()
                utc = str(p.get("utcDate") or "").strip()
                home = str(p.get("home") or "").strip()
                away = str(p.get("away") or "").strip()
                return f"{mid}|{bm}|{utc}|{home}|{away}"

            for p in picks_all:
                if not isinstance(p, dict):
                    continue
                k = _pick_debug_key(p)
                if not k:
                    continue
                debug_watch_keys.add(k)
                if len(debug_watch_keys) >= odds_debug_samples:
                    break

        picks_all = _enrich_football_picks_with_odds(
            league_code, merged_matches, picks_all, debug_watch_keys=debug_watch_keys
        )
    for p in picks_all:
        if isinstance(p, dict):
            enrich_pick_with_aftr_score(p)
    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)
    backup_current_to_prev(f"daily_matches_{league_code}.json")
    write_json(f"daily_matches_{league_code}.json", merged_matches)
    backup_current_to_prev(f"daily_picks_{league_code}.json")
    write_json(f"daily_picks_{league_code}.json", picks_daily)

    if fetch_odds:
        odds_debug_enabled = bool(getattr(settings, "debug", False)) or str(os.getenv("AFTR_ODDS_DEBUG", "0")).lower() in (
            "1",
            "true",
            "yes",
        )
        odds_debug_samples = int(os.getenv("AFTR_ODDS_DEBUG_SAMPLES", "3"))
        if odds_debug_enabled and odds_debug_samples > 0:
            def _pick_debug_key(p: dict) -> str:
                mid = p.get("match_id")
                if mid is None:
                    mid = p.get("id")
                bm = (p.get("best_market") or "").strip()
                utc = str(p.get("utcDate") or "").strip()
                home = str(p.get("home") or "").strip()
                away = str(p.get("away") or "").strip()
                return f"{mid}|{bm}|{utc}|{home}|{away}"

            saved = _read_json_list(f"daily_picks_{league_code}.json")
            saved_map = {_pick_debug_key(p): p for p in saved if isinstance(p, dict)}
            for k in list(debug_watch_keys or []):
                sp = saved_map.get(k) or {}
                logger.info(
                    "ODDS DEBUG saved | league=%s pick_key=%s home=%s away=%s best_market=%s | odds_decimal=%s implied_prob=%s edge=%s",
                    league_code,
                    k,
                    sp.get("home"),
                    sp.get("away"),
                    sp.get("best_market"),
                    sp.get("odds_decimal"),
                    sp.get("implied_prob"),
                    sp.get("edge"),
                )
    _save_history(league_code, picks_all)
    _save_team_names_cache(team_names)
    if metrics is not None:
        metrics.matches_updated += len(merged_matches)
    return len(finished_matches_norm), len(picks_daily)


def _apply_results_by_match_id(
    picks: list[dict], finished_by_id: dict[int, tuple[int, int]]
) -> list[dict]:
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

        res = (p.get("result") or "").strip().upper()
        if res in ("", "PENDING", "NONE"):
            market = (p.get("best_market") or "").strip()
            result, _reason = evaluate_market(market, hg, ag)
            p["result"] = result

    return picks


# -------------------------
# Merge + History
# -------------------------
def _merge_by_match_id(existing: list[dict], new: list[dict]) -> list[dict]:
    by_id: dict[int, dict] = {}

    for item in existing or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("match_id")
        if mid is None:
            mid = item.get("id")
        mid_i = _safe_int(mid)
        if mid_i is None:
            continue
        by_id[mid_i] = item

    for item in new or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("match_id")
        if mid is None:
            mid = item.get("id")
        mid_i = _safe_int(mid)
        if mid_i is None:
            continue
        by_id[mid_i] = item

    return list(by_id.values())


def _save_history(league_code: str, picks: list[dict]) -> None:
    hist_file = f"picks_history_{league_code}.json"
    history = _read_json_list(hist_file)
    merged = _merge_by_match_id(history, picks)
    write_json(hist_file, merged)


def _window_daily(picks: list[dict], keep_days: int | None) -> list[dict]:
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
# Public API
# -------------------------
def refresh_league(
    league_code: str,
    *,
    mode: str = "full",
    finished_days_back: int = 7,
    fetch_odds: bool = True,
    metrics: RefreshMetrics | None = None,
) -> tuple[int, int]:
    if league_code not in settings.leagues:
        logger.warning("Liga desconocida: %s", league_code)
        return 0, 0

    mode_norm = (mode or "full").strip().lower()

    sport = getattr(settings, "league_sport", {}).get(league_code, "football")
    if sport == "basketball":
        if mode_norm != "full":
            return 0, 0
        from services import refresh_basketball
        return refresh_basketball.refresh_league_basketball(
            league_code,
            finished_days_back=finished_days_back,
            metrics=metrics,
        )

    if mode_norm == "live":
        return _refresh_league_live_only(league_code, metrics)
    if mode_norm == "upcoming":
        return _refresh_league_upcoming_only(league_code, fetch_odds, metrics)
    if mode_norm == "results":
        return _refresh_league_results_only(league_code, finished_days_back, fetch_odds, metrics)
    if mode_norm != "full":
        logger.warning("Modo refresh desconocido %s (liga %s)", mode_norm, league_code)
        return 0, 0

    # --- Football-only from here --- (mode full)
    # 1) Upcoming
    try:
        raw_upcoming = get_upcoming_matches(league_code)
        for m in raw_upcoming:
            m["sport"] = "football"
    except UnsupportedCompetitionError as e:
        logger.warning("Liga no disponible con la API actual (403): %s", e.league_code)
        return 0, 0

    team_names = _load_team_names_cache()
    upcoming_matches = [_normalize_match(m) for m in (raw_upcoming or [])]
    _update_team_names_from_matches(team_names, upcoming_matches)
    upcoming_picks = _build_picks_from_matches(upcoming_matches, team_names)

    # 2) Existing cache
    existing_picks = _read_json_list(f"daily_picks_{league_code}.json")

    # 3) Finished
    finished_by_id: dict[int, tuple[int, int]] = {}
    finished_picks: list[dict] = []
    finished_matches_norm: list[dict] = []
    try:
        finished_matches = get_finished_matches(league_code, days_back=finished_days_back)
        for m in finished_matches or []:
            m["sport"] = "football"
        finished_by_id = _build_finished_lookup_by_id(finished_matches or [])
        finished_matches_norm = [_normalize_match(m) for m in (finished_matches or [])]
        _update_team_names_from_matches(team_names, finished_matches_norm)
        finished_picks = _build_picks_from_matches(finished_matches_norm, team_names)
    except Exception as e:
        logger.warning("No pude traer FINISHED para %s (sigo sin evaluar): %s", league_code, e)

    # 4) Merge picks
    merged = _merge_by_match_id(existing_picks, upcoming_picks)
    merged = _merge_by_match_id(merged, finished_picks)

    # 5) Apply results + scores
    picks_all = _apply_results_by_match_id(merged, finished_by_id)

    # 5b) Enrich football picks with odds (implied prob, edge) — optional to save Odds API calls
    merged_matches_for_odds = _merge_by_match_id(upcoming_matches, finished_matches_norm)
    if fetch_odds:
        odds_debug_enabled = bool(getattr(settings, "debug", False)) or str(os.getenv("AFTR_ODDS_DEBUG", "0")).lower() in (
            "1",
            "true",
            "yes",
        )
        odds_debug_samples = int(os.getenv("AFTR_ODDS_DEBUG_SAMPLES", "3"))
        debug_watch_keys: set[str] | None = None
        if odds_debug_enabled and odds_debug_samples > 0:
            debug_watch_keys = set()

            def _pick_debug_key(p: dict) -> str:
                mid = p.get("match_id")
                if mid is None:
                    mid = p.get("id")
                bm = (p.get("best_market") or "").strip()
                utc = str(p.get("utcDate") or "").strip()
                home = str(p.get("home") or "").strip()
                away = str(p.get("away") or "").strip()
                return f"{mid}|{bm}|{utc}|{home}|{away}"

            for p in picks_all:
                if not isinstance(p, dict):
                    continue
                k = _pick_debug_key(p)
                if not k:
                    continue
                debug_watch_keys.add(k)
                if len(debug_watch_keys) >= odds_debug_samples:
                    break

        picks_all = _enrich_football_picks_with_odds(
            league_code, merged_matches_for_odds, picks_all, debug_watch_keys=debug_watch_keys
        )

    # 5c) AFTR Score (model_score, value_score, form_score, xg_score, aftr_score, tier, edge, confidence, confidence_level)
    for p in picks_all:
        if isinstance(p, dict):
            enrich_pick_with_aftr_score(p)

    premium_picks = filter_premium_picks(picks_all)
    logger.info("AFTR premium picks: %s / %s", len(premium_picks), len(picks_all))
    if picks_all:
        sample = next((p for p in picks_all if isinstance(p, dict)), None)
        if sample:
            logger.info("AFTR SAMPLE PICK: %s", {k: sample.get(k) for k in ("aftr_score", "tier", "edge", "confidence", "confidence_level", "home", "away", "best_market")})

    # 6) Save daily (ventana opcional)
    keep_days = getattr(settings, "daily_keep_days", None)
    picks_daily = _window_daily(picks_all, keep_days)

    # 7) Guardar matches (para score/compat UI). Backup actual a .prev antes de sobrescribir (fallback UI durante refresh).
    existing_matches = _read_json_list(f"daily_matches_{league_code}.json")
    merged_matches = _merge_by_match_id(merged_matches_for_odds, existing_matches)
    backup_current_to_prev(f"daily_matches_{league_code}.json")
    write_json(f"daily_matches_{league_code}.json", merged_matches)

    # daily picks
    backup_current_to_prev(f"daily_picks_{league_code}.json")
    write_json(f"daily_picks_{league_code}.json", picks_daily)

    if fetch_odds:
        odds_debug_enabled = bool(getattr(settings, "debug", False)) or str(os.getenv("AFTR_ODDS_DEBUG", "0")).lower() in (
            "1",
            "true",
            "yes",
        )
        odds_debug_samples = int(os.getenv("AFTR_ODDS_DEBUG_SAMPLES", "3"))
        if odds_debug_enabled and odds_debug_samples > 0:
            def _pick_debug_key(p: dict) -> str:
                mid = p.get("match_id")
                if mid is None:
                    mid = p.get("id")
                bm = (p.get("best_market") or "").strip()
                utc = str(p.get("utcDate") or "").strip()
                home = str(p.get("home") or "").strip()
                away = str(p.get("away") or "").strip()
                return f"{mid}|{bm}|{utc}|{home}|{away}"

            saved = _read_json_list(f"daily_picks_{league_code}.json")
            saved_map = {_pick_debug_key(p): p for p in saved if isinstance(p, dict)}
            for k in list(debug_watch_keys or []):
                sp = saved_map.get(k) or {}
                logger.info(
                    "ODDS DEBUG saved | league=%s pick_key=%s home=%s away=%s best_market=%s | odds_decimal=%s implied_prob=%s edge=%s",
                    league_code,
                    k,
                    sp.get("home"),
                    sp.get("away"),
                    sp.get("best_market"),
                    sp.get("odds_decimal"),
                    sp.get("implied_prob"),
                    sp.get("edge"),
                )

    # 8) history eterno
    _save_history(league_code, picks_all)

    # 9) persist team names
    _save_team_names_cache(team_names)

    settled = sum(1 for p in picks_daily if (p.get("result") or "").upper() in ("WIN", "LOSS", "PUSH"))
    pending = sum(1 for p in picks_daily if (p.get("result") or "").upper() == "PENDING")

    logger.info(
        "Liga %s: upcoming=%d | daily picks=%d (settled=%d pending=%d) | history updated",
        league_code,
        len(upcoming_matches),
        len(picks_daily),
        settled,
        pending,
    )
    if metrics is not None:
        metrics.matches_updated += len(merged_matches)
    return len(upcoming_matches), len(picks_daily)


def _tier_from_name_or_prob(combo: dict) -> str:
    name = (combo.get("name") or "").upper()
    if "SAFE" in name:
        return "SAFE"
    if "MEDIUM" in name:
        return "MEDIUM"
    if "SPICY" in name:
        return "SPICY"

    pct = combo.get("combo_prob_pct")
    try:
        pct = float(pct)
    except Exception:
        pct = None

    if pct is None:
        return "SPICY"
    if pct >= 55:
        return "SAFE"
    if pct >= 45:
        return "MEDIUM"
    return "SPICY"

def _dedupe_window(win: dict) -> None:
    """Saca premium clones (vs free y entre sí)."""
    if not isinstance(win, dict):
        return

    free = win.get("free") if isinstance(win.get("free"), dict) else {}
    free_sig = _combo_sig(free) if free else ""

    prem = win.get("premium")
    if not isinstance(prem, list):
        return

    seen = set()
    out = []
    for c in prem:
        if not isinstance(c, dict):
            continue
        sig = _combo_sig(c)
        if not sig:
            continue
        if sig == free_sig:
            continue
        if sig in seen:
            continue
        seen.add(sig)
        out.append(c)

    win["premium"] = out

def _build_and_save_combos() -> None:
    """
    Genera combinadas globales y las guarda en cache:
      - today: solo partidos del día (UTC)
      - next3d: próximos 3 días (72hs, UTC)
    """
    picks_by_league: dict[str, list[dict]] = {}

    for c in settings.league_codes():
        p = read_json(f"daily_picks_{c}.json") or []
        picks_by_league[c] = [x for x in p if isinstance(x, dict)]

    today = build_global_combos(picks_by_league, mode="today")
    next3d = build_global_combos(picks_by_league, mode="3d")

    payload = {
        "today": today,
        "next3d": next3d,
        "meta": {
            "generated_utc": datetime.now(timezone.utc).isoformat(),
            "leagues": list(settings.league_codes()),
        },
    }  # <-- ACÁ NO VA COMA

    # 1) (opcional) si NO querés que 72hs incluya HOY:
    _prune_next3d_overlap(payload)

    # 2) matar clones premium (vs free y entre sí)
    _dedupe_window(payload.get("today") or {})
    _dedupe_window(payload.get("next3d") or {})

    # 3) arreglar tiers: SAFE/MEDIUM/SPICY como corresponde
    _fix_tiers(payload.get("today") or {})
    _fix_tiers(payload.get("next3d") or {})

    write_json("daily_combos.json", payload)

    logger.info(
        "Combos OK: today=%s uniq=%s | next3d=%s uniq=%s",
        ((today.get("meta") or {}).get("total_candidates") if isinstance(today, dict) else None),
        ((today.get("meta") or {}).get("total_unique_matches") if isinstance(today, dict) else None),
        ((next3d.get("meta") or {}).get("total_candidates") if isinstance(next3d, dict) else None),
        ((next3d.get("meta") or {}).get("total_unique_matches") if isinstance(next3d, dict) else None),
    )

def refresh_all(
    *,
    non_blocking: bool = False,
    light: bool = False,
) -> RefreshAllResult:
    """
    Refresca picks/partidos. `light=True` (auto-refresh): menos ventana FINISHED, menos ligas por ciclo,
    sin odds por defecto, salta ligas recién actualizadas. `non_blocking=True`: no espera si ya hay refresco.
    """
    global _auto_rr_index

    if not _refresh_global_lock.acquire(blocking=not non_blocking):
        logger.info("refresh_all: skipped (already running)")
        return RefreshAllResult(ran=False, skipped_busy=True, light_mode=light)

    result = RefreshAllResult(ran=True, light_mode=light)
    auto_log = logging.getLogger("aftr.auto_refresh")
    try:
        mode_label = "ligero" if light else "completo"
        logger.info("Iniciando refresco (%s)", mode_label)

        now_iso = datetime.now(timezone.utc).isoformat()
        raw_meta = read_json(CACHE_META_FILENAME)
        meta_base = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        meta_base["refresh_running"] = True
        meta_base["refresh_started_at"] = now_iso
        meta_base["last_updated"] = meta_base.get("last_updated") or now_iso
        write_cache_meta(meta_base)

        if light:
            auto_log.info(
                "AUTO REFRESH START | refresh_running=true | started_at=%s | stuck_ttl=%ss",
                now_iso,
                int(getattr(settings, "refresh_running_ttl_sec", 0) or 0),
            )
        else:
            logger.info(
                "REFRESH START | refresh_running=true | started_at=%s | %s",
                now_iso,
                mode_label,
            )

        m_metrics = RefreshMetrics()
        with football_data_refresh_cycle():
            codes = list(settings.league_codes())
            skip_min = int(getattr(settings, "refresh_skip_if_fresh_min", 0) or 0) if light else 0
            batch_n = int(getattr(settings, "auto_refresh_leagues_per_cycle", 0) or 0)

            if light:
                finished_days = max(1, int(getattr(settings, "auto_refresh_finished_days", 3) or 3))
                fetch_odds = bool(getattr(settings, "auto_refresh_fetch_odds", False))
            else:
                finished_days = 7
                fetch_odds = True

            if light and batch_n > 0 and batch_n < len(codes):
                n = len(codes)
                batch = [codes[(_auto_rr_index + i) % n] for i in range(batch_n)]
                _auto_rr_index = (_auto_rr_index + batch_n) % n
                logger.info(
                    "refresh (light): round-robin batch %s (%d de %d ligas por ciclo)",
                    batch,
                    batch_n,
                    n,
                )
            else:
                batch = codes

            last_ok = _load_league_last_refresh()

            for code in batch:
                if light and skip_min > 0 and _league_is_fresh(code, last_ok, skip_min):
                    result.leagues_skipped_fresh += 1
                    logger.info(
                        "refresh: skipping league %s (updated within last %d min)",
                        code,
                        skip_min,
                    )
                    continue
                try:
                    refresh_league(
                        code,
                        finished_days_back=finished_days,
                        fetch_odds=fetch_odds,
                        metrics=m_metrics,
                    )
                    result.leagues_refreshed += 1
                    _save_league_last_refresh(
                        {code: datetime.now(timezone.utc).isoformat()}
                    )
                except Exception as e:
                    logger.exception("Error refrescando liga %s: %s", code, e)

        fd = get_football_data_cycle_stats_snapshot()
        result.football_http_requests = int(fd.get("http_requests", 0))
        result.football_cache_hits = int(fd.get("cache_hits", 0))
        result.rate_limit_sleep_sec = int(fd.get("rate_limit_sleep_sec", 0))
        result.matches_updated = m_metrics.matches_updated

        logger.info(
            "refresh summary: football_http_requests=%d cache_hits=%d rate_limit_sleep_s=%d "
            "leagues_refreshed=%d skipped_fresh=%d matches_updated=%d",
            result.football_http_requests,
            result.football_cache_hits,
            result.rate_limit_sleep_sec,
            result.leagues_refreshed,
            result.leagues_skipped_fresh,
            result.matches_updated,
        )
        logger.info("✅ Refresco finalizado")
        if light:
            auto_log.info(
                "AUTO REFRESH SUCCESS | leagues_refreshed=%d matches_updated=%d | %s",
                result.leagues_refreshed,
                result.matches_updated,
                datetime.now(timezone.utc).isoformat(),
            )
        return result
    except Exception as e:
        ts = datetime.now(timezone.utc).isoformat()
        if light:
            auto_log.error("AUTO REFRESH ERROR: %s | %s", e, ts)
        logger.exception("REFRESH ERROR: %s | %s", e, ts)
        raise
    finally:
        try:
            release_refresh_running_meta()
        except Exception as fin_e:
            logger.critical(
                "CRITICAL: release_refresh_running_meta falló (lock de proceso se libera igual): %s",
                fin_e,
                exc_info=True,
            )
        _refresh_global_lock.release()


def _combo_sig(c: dict) -> str:
    if not isinstance(c, dict):
        return ""
    legs = c.get("legs") or []
    if not isinstance(legs, list):
        return ""
    parts = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        mid = it.get("match_id") or it.get("id") or ""
        mkt = (it.get("market") or "").strip().upper()
        parts.append(f"{mid}:{mkt}")
    return "|".join(sorted(parts))

def _fix_tiers(win: dict) -> None:
    """Alinea tier con el nombre (SAFE/MEDIUM/SPICY)."""
    if not isinstance(win, dict):
        return

    free = win.get("free")
    if isinstance(free, dict):
        free["tier"] = _tier_from_name_or_prob(free)

    prem = win.get("premium")
    if isinstance(prem, list):
        for c in prem:
            if isinstance(c, dict):
                c["tier"] = _tier_from_name_or_prob(c)


def _prune_next3d_overlap(payload: dict) -> None:
    """
    Si querés que 72HS NO incluya HOY, removemos legs cuya fecha sea hoy (UTC).
    """
    if not isinstance(payload, dict):
        return

    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()

    next3d = payload.get("next3d")
    if not isinstance(next3d, dict):
        return

    def prune_combo(combo: dict) -> None:
        if not isinstance(combo, dict):
            return
        legs = combo.get("legs") or []
        if not isinstance(legs, list):
            return
        kept = []
        for it in legs:
            if not isinstance(it, dict):
                continue
            dt = it.get("utcDate") or ""
            try:
                s = dt.replace("Z", "+00:00") if isinstance(dt, str) else ""
                d = datetime.fromisoformat(s).date()
            except Exception:
                # si no parsea, lo dejamos
                kept.append(it)
                continue

            if d != today_utc:
                kept.append(it)

        combo["legs"] = kept

    if isinstance(next3d.get("free"), dict):
        prune_combo(next3d["free"])

    if isinstance(next3d.get("premium"), list):
        for c in next3d["premium"]:
            if isinstance(c, dict):
                prune_combo(c)


   
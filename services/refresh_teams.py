"""
Manejo de nombres de equipos, estadísticas recientes y estado de refresco por liga.
"""
from __future__ import annotations

from datetime import datetime, timezone

from data.cache import read_json, write_json
from services.refresh_utils import _parse_iso_utc, _safe_int

TEAM_NAMES_FILE = "team_names.json"
LEAGUE_REFRESH_STATE_FILE = "league_refresh_state.json"


# -------------------------
# League freshness state
# -------------------------

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
    # Los crests vienen directamente del payload de API-Football en cada partido.
    # Esta función se mantiene por compatibilidad pero ya no construye URLs externas.
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
# Team stats from recent matches
# -------------------------

def _result_letter_from_goals(gf: int, ga: int) -> str:
    if gf > ga:
        return "W"
    if gf < ga:
        return "L"
    return "D"


def _calc_team_stats_from_recent(team_id: int, recent: list[dict]) -> dict:
    """
    recent (del provider):
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

    return {
        "gf": round(gf_total / n, 2),
        "ga": round(ga_total / n, 2),
        "form": " ".join(letters[:5]) if letters else "—",
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
    Últimos N partidos compactados:
    { utcDate, is_home, opp_id, opp_name, opp_crest, gf, ga, res }
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
        opp_name = team_names.get(opp_id) or ""
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

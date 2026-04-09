"""
AFTR Live Events — detecta eventos en tiempo real y envía push notifications a:
  - Usuarios con tracker bets en esos partidos
  - Usuarios que siguen picks de esos partidos

Eventos detectados:
  - Kick-off (inicio del partido)
  - Goles
  - Half-time (fin del primer tiempo)
  - Inicio del segundo tiempo
  - Full-time (resultado final)

Dos motores:
  - process_live_events(): usa API-Football (RapidAPI) si API_FOOTBALL_KEY está configurada.
  - process_cache_live_events(): usa daily_matches_*.json vs .prev (football-data.org, siempre gratis).

Ambos comparten el mismo state file para evitar notificaciones duplicadas.
Corre al final de cada live refresh job.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("aftr.live_events")

# Archivo de estado: {fixture_id_str: {home, away, score_h, score_a, status, ...notified flags...}}
_STATE_FILE = "live_events_state.json"

# Ventana: solo bets con kickoff en las últimas N horas
_KICKOFF_WINDOW_H = 4

# Short-codes de API-Football
_STATUS_KICKOFF    = frozenset({"1H"})
_STATUS_HALFTIME   = frozenset({"HT"})
_STATUS_SECOND     = frozenset({"2H", "ET"})
_STATUS_FINAL      = frozenset({"FT", "AET", "PEN"})
_STATUS_LIVE       = frozenset({"1H", "2H", "ET", "LIVE"})


def _load_state() -> dict:
    from data.cache import read_json
    raw = read_json(_STATE_FILE)
    return raw if isinstance(raw, dict) else {}


def _save_state(state: dict) -> None:
    from data.cache import write_json
    write_json(_STATE_FILE, state)


def _normalize(name: str) -> str:
    return (name or "").lower().strip()


def _teams_match(fix_home: str, fix_away: str, leg_home: str, leg_away: str) -> bool:
    fh, fa = _normalize(fix_home), _normalize(fix_away)
    lh, la = _normalize(leg_home), _normalize(leg_away)
    if not fh or not fa or not lh or not la:
        return False
    if fh == lh and fa == la:
        return True
    if (lh in fh or fh in lh) and (la in fa or fa in la):
        return True
    return False


def _load_pending_legs() -> list[dict]:
    """Carga bet_legs PENDING con kickoff en ventana de las últimas _KICKOFF_WINDOW_H horas."""
    from app.db import get_conn, put_conn
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=_KICKOFF_WINDOW_H)

    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """SELECT bl.id AS leg_id, bl.bet_id, bl.home_team, bl.away_team,
                      bl.market, bl.kickoff_time, ub.user_id
               FROM bet_legs bl
               JOIN user_bets ub ON bl.bet_id = ub.id
               WHERE bl.status = 'PENDING'
                 AND bl.kickoff_time IS NOT NULL
                 AND bl.kickoff_time BETWEEN %s AND %s""",
            (window_start, now + timedelta(minutes=10)),
        )
        return list(cur.fetchall())
    finally:
        put_conn(conn)


def _load_followed_picks() -> list[dict]:
    """
    Retorna lista de {home, away, user_ids, pick_id} para picks seguidas activas.
    Usa el mismo índice que notify_upcoming_picks.
    """
    try:
        from services.push_notifications import load_user_follows_index
        from config.settings import settings
        from data.cache import read_json_with_fallback

        follows_index = load_user_follows_index()
        if not follows_index:
            return []

        result = []
        for code in settings.league_codes():
            picks = read_json_with_fallback(f"daily_picks_{code}.json")
            if not isinstance(picks, list):
                continue
            for pick in picks:
                if not isinstance(pick, dict):
                    continue
                pick_id = str(
                    pick.get("pick_id") or pick.get("id") or pick.get("match_id") or ""
                )
                if not pick_id:
                    continue
                uids = follows_index.get(pick_id, [])
                if not uids:
                    continue
                home = pick.get("home_team") or pick.get("home") or ""
                away = pick.get("away_team") or pick.get("away") or ""
                if home and away:
                    result.append(
                        {"home": home, "away": away, "user_ids": list(uids), "pick_id": pick_id}
                    )
        return result
    except Exception as e:
        logger.warning("_load_followed_picks error: %s", e)
        return []


def _users_for_fixture(
    home_name: str,
    away_name: str,
    matched_legs: list[dict],
    followed_picks: list[dict],
) -> set[int]:
    """Unión de usuarios de tracker bets + usuarios que siguen la pick."""
    users: set[int] = set()
    for leg in matched_legs:
        users.add(leg["user_id"])
    for fp in followed_picks:
        if _teams_match(home_name, away_name, fp["home"], fp["away"]):
            users.update(fp["user_ids"])
    return users


def _resolve_market_for_push(market: str, home_goals: int, away_goals: int) -> str | None:
    """Retorna 'WON' / 'LOST' si ya se puede resolver el mercado, None si aún no."""
    market = (market or "").lower().strip()
    total = home_goals + away_goals

    if market in ("1", "x", "2", "1x", "x2", "12", "btts_yes", "btts_no", "dnb_1", "dnb_2"):
        return None

    if market.startswith("over_"):
        try:
            line = float(market[5:])
            if total > line:
                return "WON"
        except ValueError:
            pass
        return None

    if market.startswith("under_"):
        try:
            line = float(market[6:])
            if total >= line:
                return "LOST"
        except ValueError:
            pass
        return None

    return None


def _send_all(send_to_user, user_ids: set[int], payload: dict) -> int:
    sent = 0
    for uid in user_ids:
        sent += send_to_user(uid, payload)
    return sent


def _update_cache_live_minutes(live_fixtures: list[dict]) -> None:
    """
    Parcha el caché diario con el minuto actual de cada partido en vivo
    (API-Football → elapsed). Así la UI puede mostrar '67'' en las live cards.
    """
    from data.cache import read_json, write_json
    from config.settings import settings

    live_by_teams: dict[str, dict] = {}
    for fix in live_fixtures:
        teams = fix.get("teams") or {}
        home_n = _normalize((teams.get("home") or {}).get("name", ""))
        away_n = _normalize((teams.get("away") or {}).get("name", ""))
        if not home_n or not away_n:
            continue
        fix_info = fix.get("fixture") or {}
        status_info = fix_info.get("status") or {}
        live_by_teams[f"{home_n}|{away_n}"] = {
            "elapsed": status_info.get("elapsed"),
            "status_short": status_info.get("short", ""),
        }

    if not live_by_teams:
        return

    for code in settings.league_codes():
        fname = f"daily_matches_{code}.json"
        data = read_json(fname)
        if not isinstance(data, list):
            continue
        changed = False
        for m in data:
            if not isinstance(m, dict):
                continue
            h = _normalize(m.get("home") or m.get("home_team") or "")
            a = _normalize(m.get("away") or m.get("away_team") or "")
            info = live_by_teams.get(f"{h}|{a}")
            if info:
                m["elapsed"] = info["elapsed"]
                if info["status_short"]:
                    m["status"] = info["status_short"]
                changed = True
        if changed:
            write_json(fname, data)


def process_live_events() -> int:
    """
    Busca partidos en vivo via API-Football, detecta eventos de estado y goles,
    y envía pushes a usuarios con tracker bets o picks seguidas en esos partidos.
    Retorna cantidad de notificaciones enviadas.
    """
    from data.providers.api_football import fetch_live_fixtures, _api_key
    from services.push_notifications import send_to_user

    if not _api_key():
        return 0

    live_fixtures = fetch_live_fixtures()
    if not live_fixtures:
        return 0

    # Actualizar minutos en caché para que la UI los muestre
    try:
        _update_cache_live_minutes(live_fixtures)
    except Exception as _e:
        logger.warning("update_cache_live_minutes error (non-fatal): %s", _e)

    pending_legs    = _load_pending_legs()
    followed_picks  = _load_followed_picks()

    if not pending_legs and not followed_picks:
        return 0

    state = _load_state()
    state_changed = False
    notifications_sent = 0

    for fixture in live_fixtures:
        if not isinstance(fixture, dict):
            continue

        fix_info    = fixture.get("fixture") or {}
        fix_id      = str(fix_info.get("id") or "")
        if not fix_id:
            continue

        fix_status  = (fix_info.get("status") or {}).get("short", "")
        fix_elapsed = (fix_info.get("status") or {}).get("elapsed") or 0

        teams     = fixture.get("teams") or {}
        home_name = (teams.get("home") or {}).get("name", "")
        away_name = (teams.get("away") or {}).get("name", "")
        home_logo = (teams.get("home") or {}).get("logo", "") or "/static/logo_aftr.png"
        away_logo = (teams.get("away") or {}).get("logo", "") or "/static/logo_aftr.png"

        goals   = fixture.get("goals") or {}
        score_h = goals.get("home")
        score_a = goals.get("away")
        if score_h is None or score_a is None:
            continue
        try:
            score_h, score_a = int(score_h), int(score_a)
        except (TypeError, ValueError):
            continue

        # Buscar tracker legs que correspondan a este fixture
        matched_legs = [
            leg for leg in pending_legs
            if _teams_match(home_name, away_name, leg["home_team"], leg["away_team"])
        ]

        # Todos los usuarios interesados (tracker + siguiendo pick)
        all_users = _users_for_fixture(home_name, away_name, matched_legs, followed_picks)
        if not all_users:
            continue

        prev            = state.get(fix_id) or {}
        prev_h          = prev.get("score_h", -1)
        prev_a          = prev.get("score_a", -1)
        prev_status     = prev.get("status", "")
        first_time_seen = prev_h < 0 and prev_a < 0
        is_final        = fix_status in _STATUS_FINAL
        goal_scored     = (
            (score_h != prev_h or score_a != prev_a)
            and not first_time_seen
        )

        # Actualizar estado
        state[fix_id] = {
            "home":             home_name,
            "away":             away_name,
            "score_h":          score_h,
            "score_a":          score_a,
            "status":           fix_status,
            "notified_final":   prev.get("notified_final", False),
            "notified_kickoff": prev.get("notified_kickoff", False),
            "notified_ht":      prev.get("notified_ht", False),
            "notified_2h":      prev.get("notified_2h", False),
            "notified_goals":   prev.get("notified_goals") or [],
        }
        state_changed = True

        score_str = f"{score_h}–{score_a}"
        match_title = f"{home_name} vs {away_name}"

        # ── KICK-OFF ──────────────────────────────────────────────────────────
        if (
            fix_status in _STATUS_KICKOFF
            and prev_status not in _STATUS_KICKOFF
            and not state[fix_id]["notified_kickoff"]
            and int(fix_elapsed or 0) <= 30
        ):
            payload = {
                "title": f"Arrancó: {match_title}",
                "body":  f"El partido comenzó • min {fix_elapsed}",
                "icon":  home_logo,
                "tag":   f"kickoff-{fix_id}",
                "url":   "/",
                "data":  {"fixture_id": fix_id},
            }
            n = _send_all(send_to_user, all_users, payload)
            notifications_sent += n
            state[fix_id]["notified_kickoff"] = True
            if n:
                logger.info("live_events KICKOFF push: fix=%s %s users=%d", fix_id, match_title, n)

        # ── HALF-TIME ─────────────────────────────────────────────────────────
        if (
            fix_status in _STATUS_HALFTIME
            and prev_status not in _STATUS_HALFTIME
            and not state[fix_id]["notified_ht"]
        ):
            payload = {
                "title": f"Descanso: {home_name} {score_str} {away_name}",
                "body":  "Fin del primer tiempo",
                "icon":  home_logo,
                "tag":   f"ht-{fix_id}",
                "url":   "/",
                "data":  {"fixture_id": fix_id},
            }
            n = _send_all(send_to_user, all_users, payload)
            notifications_sent += n
            state[fix_id]["notified_ht"] = True
            if n:
                logger.info("live_events HT push: fix=%s %s %s users=%d", fix_id, match_title, score_str, n)

        # ── INICIO 2° TIEMPO ──────────────────────────────────────────────────
        if (
            fix_status in _STATUS_SECOND
            and prev_status not in _STATUS_SECOND
            and not state[fix_id]["notified_2h"]
        ):
            payload = {
                "title": f"2° tiempo: {home_name} {score_str} {away_name}",
                "body":  "Arrancó el segundo tiempo",
                "icon":  home_logo,
                "tag":   f"2h-{fix_id}",
                "url":   "/",
                "data":  {"fixture_id": fix_id},
            }
            n = _send_all(send_to_user, all_users, payload)
            notifications_sent += n
            state[fix_id]["notified_2h"] = True
            if n:
                logger.info("live_events 2H push: fix=%s %s %s users=%d", fix_id, match_title, score_str, n)

        # ── FULL-TIME ─────────────────────────────────────────────────────────
        if is_final and not state[fix_id]["notified_final"]:
            # Para tracker legs, agregar los mercados en juego
            legs_by_user: dict[int, list[dict]] = {}
            for leg in matched_legs:
                legs_by_user.setdefault(leg["user_id"], []).append(leg)

            for uid in all_users:
                legs = legs_by_user.get(uid, [])
                if legs:
                    markets = ", ".join(leg["market"] for leg in legs if leg.get("market"))
                    body = f"Tu apuesta: {markets}" if markets else "Partido terminado"
                else:
                    body = "Partido terminado"

                payload = {
                    "title": f"FT: {home_name} {score_str} {away_name}",
                    "body":  body,
                    "icon":  home_logo,
                    "tag":   f"ft-{fix_id}",
                    "url":   "/tracker",
                    "data":  {"fixture_id": fix_id},
                }
                sent = send_to_user(uid, payload)
                notifications_sent += sent
                if sent:
                    logger.info(
                        "live_events FT push: fix=%s %s %s user=%s", fix_id, match_title, score_str, uid
                    )

            state[fix_id]["notified_final"] = True

        # ── GOLES ─────────────────────────────────────────────────────────────
        elif goal_scored and not is_final:
            notified_goals = state[fix_id]["notified_goals"]
            score_key = f"{score_h}-{score_a}"
            if score_key not in notified_goals:
                state[fix_id]["notified_goals"] = notified_goals + [score_key]

                legs_by_user = {}
                for leg in matched_legs:
                    legs_by_user.setdefault(leg["user_id"], []).append(leg)

                for uid in all_users:
                    legs = legs_by_user.get(uid, [])
                    if legs:
                        early_results = []
                        for leg in legs:
                            early = _resolve_market_for_push(leg["market"], score_h, score_a)
                            if early == "WON":
                                early_results.append(f"{leg['market']} ✅")
                        if early_results:
                            body = f"⚽ {score_str} | {', '.join(early_results)}"
                        else:
                            markets = ", ".join(leg["market"] for leg in legs if leg.get("market"))
                            body = f"⚽ {score_str} min {fix_elapsed}" + (f" | {markets}" if markets else "")
                    else:
                        body = f"⚽ Gol — {score_str} · min {fix_elapsed}"

                    payload = {
                        "title": f"{home_name} {score_str} {away_name}",
                        "body":  body,
                        "icon":  home_logo,
                        "tag":   f"goal-{fix_id}-{score_key}",
                        "url":   "/",
                        "data":  {"fixture_id": fix_id},
                    }
                    sent = send_to_user(uid, payload)
                    notifications_sent += sent
                    if sent:
                        logger.info(
                            "live_events GOAL push: fix=%s %s %s min=%s user=%s",
                            fix_id, match_title, score_str, fix_elapsed, uid,
                        )

    # Limpiar estado de fixtures finalizados y ya notificados
    keys_to_remove = [
        k for k, v in state.items()
        if isinstance(v, dict)
        and v.get("status") in _STATUS_FINAL
        and v.get("notified_final")
    ]
    for k in keys_to_remove:
        del state[k]
        state_changed = True

    if state_changed:
        _save_state(state)

    return notifications_sent


# ──────────────────────────────────────────────────────────────────────────────
# MOTOR GRATUITO: detección via daily_matches_*.json vs .prev
# Usa football-data.org (ya disponible), sin costo adicional.
# ──────────────────────────────────────────────────────────────────────────────

# Statuses de football-data.org
_FDO_KICKOFF  = frozenset({"IN_PLAY"})
_FDO_HALFTIME = frozenset({"PAUSED"})
_FDO_FINAL    = frozenset({"FINISHED", "FT", "AWARDED"})
_FDO_LIVE     = frozenset({"IN_PLAY", "PAUSED"})


def process_cache_live_events(league_codes: list[str]) -> int:
    """
    Detecta eventos comparando daily_matches_{code}.json (actual) vs .prev (anterior).
    Funciona sin API-Football — solo usa football-data.org que ya tenemos.

    Detecta: kickoff, goles, half-time, inicio 2T, full-time.
    Comparte el mismo state file que process_live_events para evitar duplicados.
    """
    from data.cache import read_json
    from services.push_notifications import send_to_user, load_user_follows_index
    from config.settings import settings as _s

    pending_legs   = _load_pending_legs()
    follows_index  = load_user_follows_index()   # {pick_id: [user_ids]}
    followed_picks = _load_followed_picks()       # lista para matching por nombre

    if not pending_legs and not follows_index:
        return 0

    state = _load_state()
    state_changed = False
    notifications_sent = 0

    for code in league_codes:
        curr_list = read_json(f"daily_matches_{code}.json")
        prev_list = read_json(f"daily_matches_{code}.json.prev")

        if not isinstance(curr_list, list) or not isinstance(prev_list, list):
            continue

        # Índice del estado previo por match_id
        prev_index: dict[str, dict] = {}
        for m in prev_list:
            if isinstance(m, dict) and m.get("match_id"):
                prev_index[str(m["match_id"])] = m

        for match in curr_list:
            if not isinstance(match, dict):
                continue

            mid        = str(match.get("match_id") or "")
            home_name  = match.get("home") or match.get("home_team") or ""
            away_name  = match.get("away") or match.get("away_team") or ""
            home_crest = match.get("home_crest") or "/static/logo_aftr.png"
            away_crest = match.get("away_crest") or "/static/logo_aftr.png"
            cur_status = (match.get("status") or "").upper()
            score      = match.get("score") or {}
            score_h    = score.get("home")
            score_a    = score.get("away")

            if not mid or not home_name or not away_name:
                continue
            if score_h is None or score_a is None:
                score_h = score_h or 0
                score_a = score_a or 0
            try:
                score_h, score_a = int(score_h), int(score_a)
            except (TypeError, ValueError):
                score_h, score_a = 0, 0

            # Solo procesar partidos que estén o hayan estado en vivo
            if cur_status not in (_FDO_LIVE | _FDO_FINAL):
                continue

            prev_match  = prev_index.get(mid) or {}
            prev_status = (prev_match.get("status") or "").upper()
            prev_score  = prev_match.get("score") or {}
            prev_h      = prev_score.get("home")
            prev_a      = prev_score.get("away")
            try:
                prev_h = int(prev_h) if prev_h is not None else -1
                prev_a = int(prev_a) if prev_a is not None else -1
            except (TypeError, ValueError):
                prev_h, prev_a = -1, -1

            first_time_seen = not prev_match

            # Usuarios interesados: tracker bets + picks seguidas
            matched_legs = [
                leg for leg in pending_legs
                if _teams_match(home_name, away_name, leg["home_team"], leg["away_team"])
            ]
            # También buscar por pick_id directo en follows_index
            pick_followers: set[int] = set()
            for fp in followed_picks:
                if _teams_match(home_name, away_name, fp["home"], fp["away"]):
                    pick_followers.update(fp["user_ids"])
            all_users = _users_for_fixture(home_name, away_name, matched_legs, followed_picks)
            if not all_users:
                continue

            # Clave de estado: prefijo "c_" para diferenciar de API-Football
            state_key = f"c_{mid}"
            prev_state = state.get(state_key) or {}

            state[state_key] = {
                "home":             home_name,
                "away":             away_name,
                "score_h":          score_h,
                "score_a":          score_a,
                "status":           cur_status,
                "notified_kickoff": prev_state.get("notified_kickoff", False),
                "notified_ht":      prev_state.get("notified_ht", False),
                "notified_2h":      prev_state.get("notified_2h", False),
                "notified_final":   prev_state.get("notified_final", False),
                "notified_goals":   prev_state.get("notified_goals") or [],
            }
            state_changed = True

            score_str   = f"{score_h}–{score_a}"
            match_title = f"{home_name} vs {away_name}"
            is_final    = cur_status in _FDO_FINAL
            # prev_h puede ser -1 si el partido aún no había arrancado (score null).
            # Aceptamos la transición null→gol cuando el score actual ya tiene algún gol.
            goal_scored = (
                not first_time_seen
                and (score_h != prev_h or score_a != prev_a)
                and (prev_h >= 0 or score_h > 0 or score_a > 0)
            )

            # ── KICK-OFF ──────────────────────────────────────────────────────
            if (
                cur_status in _FDO_KICKOFF
                and prev_status not in _FDO_KICKOFF
                and not prev_state.get("notified_kickoff")
            ):
                payload = {
                    "title": f"Arrancó: {match_title}",
                    "body":  "El partido comenzó",
                    "icon":  home_crest,
                    "tag":   f"kickoff-c-{mid}",
                    "url":   "/",
                }
                n = _send_all(send_to_user, all_users, payload)
                notifications_sent += n
                state[state_key]["notified_kickoff"] = True
                if n:
                    logger.info("cache_live KICKOFF: mid=%s %s users=%d", mid, match_title, n)

            # ── HALF-TIME ─────────────────────────────────────────────────────
            if (
                cur_status in _FDO_HALFTIME
                and prev_status not in _FDO_HALFTIME
                and not prev_state.get("notified_ht")
            ):
                payload = {
                    "title": f"Descanso: {home_name} {score_str} {away_name}",
                    "body":  "Fin del primer tiempo",
                    "icon":  home_crest,
                    "tag":   f"ht-c-{mid}",
                    "url":   "/",
                }
                n = _send_all(send_to_user, all_users, payload)
                notifications_sent += n
                state[state_key]["notified_ht"] = True
                if n:
                    logger.info("cache_live HT: mid=%s %s %s users=%d", mid, match_title, score_str, n)

            # ── INICIO 2° TIEMPO ──────────────────────────────────────────────
            # Condición principal: venía de PAUSED (HT detectado en ciclo anterior).
            # Condición alternativa: si el poll salteó el ciclo de PAUSED, detectamos
            # 2H cuando estamos IN_PLAY + ya se había notificado el HT + aún no se
            # notificó el 2H. Esto evita perder la notificación por backoff/lag.
            if (
                cur_status in _FDO_KICKOFF
                and not prev_state.get("notified_2h")
                and (
                    prev_status in _FDO_HALFTIME                     # camino normal
                    or prev_state.get("notified_ht")                 # camino fallback
                )
            ):
                payload = {
                    "title": f"2° tiempo: {home_name} {score_str} {away_name}",
                    "body":  "Arrancó el segundo tiempo",
                    "icon":  home_crest,
                    "tag":   f"2h-c-{mid}",
                    "url":   "/",
                }
                n = _send_all(send_to_user, all_users, payload)
                notifications_sent += n
                state[state_key]["notified_2h"] = True
                if n:
                    logger.info("cache_live 2H: mid=%s %s %s users=%d", mid, match_title, score_str, n)

            # ── FULL-TIME ─────────────────────────────────────────────────────
            if is_final and not prev_state.get("notified_final"):
                legs_by_user: dict[int, list[dict]] = {}
                for leg in matched_legs:
                    legs_by_user.setdefault(leg["user_id"], []).append(leg)

                for uid in all_users:
                    legs = legs_by_user.get(uid, [])
                    body = (
                        f"Tu apuesta: {', '.join(l['market'] for l in legs if l.get('market'))}"
                        if legs else "Partido terminado"
                    )
                    payload = {
                        "title": f"FT: {home_name} {score_str} {away_name}",
                        "body":  body,
                        "icon":  home_crest,
                        "tag":   f"ft-c-{mid}",
                        "url":   "/tracker",
                    }
                    sent = send_to_user(uid, payload)
                    notifications_sent += sent
                    if sent:
                        logger.info("cache_live FT: mid=%s %s %s user=%s", mid, match_title, score_str, uid)

                state[state_key]["notified_final"] = True

            # ── GOLES ─────────────────────────────────────────────────────────
            elif goal_scored and not is_final:
                score_key      = f"{score_h}-{score_a}"
                notified_goals = state[state_key]["notified_goals"]
                if score_key not in notified_goals:
                    state[state_key]["notified_goals"] = notified_goals + [score_key]

                    legs_by_user = {}
                    for leg in matched_legs:
                        legs_by_user.setdefault(leg["user_id"], []).append(leg)

                    for uid in all_users:
                        legs = legs_by_user.get(uid, [])
                        if legs:
                            early = [
                                f"{l['market']} ✅"
                                for l in legs
                                if _resolve_market_for_push(l["market"], score_h, score_a) == "WON"
                            ]
                            if early:
                                body = f"⚽ {score_str} | {', '.join(early)}"
                            else:
                                markets = ", ".join(l["market"] for l in legs if l.get("market"))
                                body = f"⚽ {score_str}" + (f" | {markets}" if markets else "")
                        else:
                            body = f"⚽ Gol — {score_str}"

                        payload = {
                            "title": f"{home_name} {score_str} {away_name}",
                            "body":  body,
                            "icon":  home_crest,
                            "tag":   f"goal-c-{mid}-{score_key}",
                            "url":   "/",
                        }
                        sent = send_to_user(uid, payload)
                        notifications_sent += sent
                        if sent:
                            logger.info(
                                "cache_live GOAL: mid=%s %s %s user=%s", mid, match_title, score_str, uid
                            )

    # Limpiar estado de partidos finalizados
    keys_to_remove = [
        k for k, v in state.items()
        if k.startswith("c_")
        and isinstance(v, dict)
        and v.get("status") in _FDO_FINAL
        and v.get("notified_final")
    ]
    for k in keys_to_remove:
        del state[k]
        state_changed = True

    if state_changed:
        _save_state(state)

    return notifications_sent

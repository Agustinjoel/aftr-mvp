"""
Lógica y rendering de combinadas (combos): construcción, deduplicación y HTML de cards.
"""
from __future__ import annotations

import html as html_lib
import logging
from datetime import datetime, timedelta
from typing import Any, Callable

from app.timefmt import AFTR_DISPLAY_TZ, format_match_kickoff_ar
from app.ui_helpers import _safe_float, _safe_int
from app.ui_picks_calc import _aftr_score, _pick_score, _pick_local_date
from app.ui_team import _team_with_crest

logger = logging.getLogger("aftr.ui.combos")


# =========================================================
# Kickoff line para patas de combo
# =========================================================

def _combo_leg_kickoff_html(leg: dict) -> str:
    """HTML de kickoff opcional para una pata de combo (hora en America/Argentina/Buenos_Aires)."""
    ko = format_match_kickoff_ar(leg.get("utcDate"))
    if ko == "—":
        return ""
    return f'<div class="combo-kickoff muted">{html_lib.escape(ko)}</div>'


# =========================================================
# Firmas y deduplicación
# =========================================================

def _leg_sig(it: dict) -> str:
    if not isinstance(it, dict):
        return ""
    mid = it.get("match_id") or it.get("id") or ""
    mkt = (it.get("market") or "").strip().upper()
    return f"{mid}:{mkt}"


def _combo_sig(combo: dict) -> str:
    """Firma estable: mismos partidos+mercados → mismo combo."""
    if not isinstance(combo, dict):
        return ""
    legs = combo.get("legs") or []
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


def _uniq_combos(combos: list[dict]) -> list[dict]:
    """Filtra duplicados por firma de patas (home+away+market)."""
    seen = set()
    out = []
    for c in combos or []:
        if not isinstance(c, dict):
            continue
        legs = c.get("legs") or []
        sig = "|".join(
            f"{(x.get('home') or '').strip()}-{(x.get('away') or '').strip()}:{(x.get('market') or '').strip()}"
            for x in legs if isinstance(x, dict)
        )
        if not sig or sig in seen:
            continue
        seen.add(sig)
        out.append(c)
    return out


# =========================================================
# Helpers internos de construcción
# =========================================================

def _combo_match_key_for_home(p: dict) -> tuple[str, int] | None:
    """Clave global de partido para deduplicación en combos home: (league, match_id)."""
    if not isinstance(p, dict):
        return None
    league = (p.get("_league") or p.get("league") or "").strip()
    if not league:
        return None
    mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
    if mid is None:
        return None
    return (league, int(mid))


def _combo_leg_odds_value(p: dict) -> float | None:
    """Odds de una pata para el cálculo de cuota combinada: odds_decimal → best_fair → 1/best_prob."""
    if not isinstance(p, dict):
        return None
    od = p.get("odds_decimal")
    if od is not None:
        try:
            return float(od)
        except (TypeError, ValueError):
            pass
    bf = p.get("best_fair")
    if bf is not None:
        try:
            return float(bf)
        except (TypeError, ValueError):
            pass
    bp = _safe_float(p.get("best_prob"))
    if bp > 0:
        try:
            return 1.0 / bp
        except (TypeError, ValueError):
            pass
    return None


def _combo_calc_combined(legs: list[dict]) -> tuple[float, float | None]:
    """Calcula (prob_combinada, odds_combinadas | None) a partir de la lista de patas."""
    combined_prob  = 1.0
    combined_odds: float | None = None
    odds_ok = True

    for leg in legs:
        combined_prob *= leg.get("prob") or 0
        od = leg.get("odds_decimal") if "odds_decimal" in leg else leg.get("odds_value")
        if od is None:
            odds_ok = False
        else:
            try:
                o = float(od)
                combined_odds = o if combined_odds is None else combined_odds * o
            except (TypeError, ValueError):
                odds_ok = False

    if not odds_ok:
        combined_odds = None
    return combined_prob, combined_odds


def _combo_risk(combined_prob: float) -> str:
    if combined_prob >= 0.20:
        return "Safe"
    if combined_prob >= 0.10:
        return "Medium"
    return "Aggressive"


def _combo_score_from_candidates(legs: list[dict], candidates: list[dict]) -> int:
    """Promedio de AFTR scores de los picks que forman las patas."""
    scores: list[int] = []
    for leg in legs:
        mid        = leg.get("match_id")
        leg_league = leg.get("_league")
        for p in candidates:
            if leg_league is not None:
                match_ok = p.get("_league") == leg_league and (
                    _safe_int(p.get("match_id")) == mid or _safe_int(p.get("id")) == mid
                )
            else:
                match_ok = _safe_int(p.get("match_id")) == mid or _safe_int(p.get("id")) == mid
            if match_ok:
                scores.append(_aftr_score(p))
                break
        else:
            scores.append(min(100, int((leg.get("prob") or 0) * 100)))
    return max(0, min(100, int(round(sum(scores) / len(scores))))) if scores else 0


# =========================================================
# Build: Combo del Día (dashboard de liga)
# =========================================================

def _build_combo_of_the_day(
    upcoming_picks: list[dict],
    match_by_id: dict[Any, dict],
    match_key_fn: Callable[[dict], Any] | None = None,
) -> dict | None:
    """
    Construye un combo desde los picks del día: hasta 3 patas, AFTR ≥ 75, confianza ≥ 6,
    edge positivo cuando disponible, máximo un pick por partido.
    match_key_fn: opcional; si se pasa, se usa para deduplicar por (liga, match_id).
    """
    get_key = match_key_fn or (lambda p: _safe_int(p.get("match_id")) or _safe_int(p.get("id")))

    def _valid(p: dict) -> bool:
        if _aftr_score(p) < 75:
            return False
        if (_safe_int(p.get("confidence")) or 0) < 6:
            return False
        edge_val = p.get("edge")
        if edge_val is not None:
            try:
                if float(edge_val) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    candidates = sorted(
        [p for p in (upcoming_picks or []) if isinstance(p, dict) and _valid(p)],
        key=lambda p: -_pick_score(p),
    )

    used_match_keys: set[Any] = set()
    leg_picks: list[dict] = []
    for p in candidates:
        if len(leg_picks) >= 3:
            break
        key = get_key(p)
        if key is None or key in used_match_keys:
            continue
        leg_picks.append(p)
        used_match_keys.add(key)

    if len(leg_picks) < 2:
        return None

    legs: list[dict] = []
    for p in leg_picks:
        m   = (match_by_id or {}).get(get_key(p))
        mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
        entry: dict = {
            "home":       p.get("home") or (m.get("home") if isinstance(m, dict) else "") or "—",
            "away":       p.get("away") or (m.get("away") if isinstance(m, dict) else "") or "—",
            "market":     (p.get("best_market") or "").strip() or "—",
            "prob":       _safe_float(p.get("best_prob"), 0),
            "odds_decimal": p.get("odds_decimal"),
            "home_crest": p.get("home_crest") or (m.get("home_crest") if isinstance(m, dict) else "") or "",
            "away_crest": p.get("away_crest") or (m.get("away_crest") if isinstance(m, dict) else "") or "",
            "match_id":   mid,
            "utcDate":    p.get("utcDate") or (m.get("utcDate") if isinstance(m, dict) else None),
        }
        if p.get("_league") is not None:
            entry["_league"] = p["_league"]
        legs.append(entry)

    combined_prob, combined_odds = _combo_calc_combined(legs)
    return {
        "legs":            legs,
        "combo_prob_pct":  round(combined_prob * 100, 1),
        "combined_odds":   combined_odds,
        "risk":            _combo_risk(combined_prob),
        "combo_score":     _combo_score_from_candidates(legs, candidates),
    }


# =========================================================
# Build: Combos por tier (home page antigua)
# =========================================================

def _build_combos_by_tier(
    upcoming_picks: list[dict],
    match_by_id: dict[Any, dict],
    match_key_fn: Callable[[dict], Any] | None = None,
    max_combos: int = 3,
) -> list[dict]:
    """
    Construye hasta max_combos combos para la home (SAFE / MEDIUM / AGGRESSIVE).
    Cada combo usa partidos distintos; el tier se asigna por probabilidad combinada.
    """
    get_key = match_key_fn or (lambda p: _safe_int(p.get("match_id")) or _safe_int(p.get("id")))

    def _valid(p: dict) -> bool:
        if _aftr_score(p) < 75:
            return False
        if (_safe_int(p.get("confidence")) or 0) < 6:
            return False
        edge_val = p.get("edge")
        if edge_val is not None:
            try:
                if float(edge_val) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    candidates = sorted(
        [p for p in (upcoming_picks or []) if isinstance(p, dict) and _valid(p)],
        key=lambda p: -_pick_score(p),
    )

    combos:          list[dict] = []
    used_match_keys: set[Any]   = set()

    for _ in range(max_combos):
        leg_picks: list[dict] = []
        for p in candidates:
            if len(leg_picks) >= 3:
                break
            key = get_key(p)
            if key is None or key in used_match_keys:
                continue
            leg_picks.append(p)
            used_match_keys.add(key)

        if len(leg_picks) < 2:
            break

        legs: list[dict] = []
        for p in leg_picks:
            m   = (match_by_id or {}).get(get_key(p))
            mid = _safe_int(p.get("match_id")) or _safe_int(p.get("id"))
            entry: dict = {
                "home":        p.get("home") or (m.get("home") if isinstance(m, dict) else "") or "—",
                "away":        p.get("away") or (m.get("away") if isinstance(m, dict) else "") or "—",
                "market":      (p.get("best_market") or "").strip() or "—",
                "prob":        _safe_float(p.get("best_prob"), 0),
                "odds_decimal": p.get("odds_decimal"),
                "home_crest":  p.get("home_crest") or (m.get("home_crest") if isinstance(m, dict) else "") or "",
                "away_crest":  p.get("away_crest") or (m.get("away_crest") if isinstance(m, dict) else "") or "",
                "match_id":    mid,
                "utcDate":     p.get("utcDate") or (m.get("utcDate") if isinstance(m, dict) else None),
            }
            if p.get("_league") is not None:
                entry["_league"] = p["_league"]
            legs.append(entry)

        combined_prob, combined_odds = _combo_calc_combined(legs)
        combos.append({
            "legs":           legs,
            "combo_prob_pct": round(combined_prob * 100, 1),
            "combined_odds":  combined_odds,
            "risk":           _combo_risk(combined_prob),
            "combo_score":    _combo_score_from_candidates(legs, candidates),
        })

    return combos


# =========================================================
# Build: Premium combos para home (3 slots con roles distintos)
# =========================================================

def _build_home_premium_combos(
    upcoming_picks: list[dict],
    match_by_key: dict[Any, dict],
    *,
    log_context: str | None = None,
) -> list[dict]:
    """
    Genera exactamente 3 combo cards premium con roles distintos:
      1) Combo del Día  — solo hoy, 3-5 patas
      2) Combo 72h      — hoy..+3 días, 3-5 patas
      3) Combo Value    — hoy..+3 días, prioriza edge/valor

    Deduplicación: sin repetir el mismo partido dentro de un combo;
    si hay inventario suficiente, evita repetir entre combos.
    Con thresholds estrictos; si no hay suficiente, reintenta con thresholds relajados.
    """
    today_local = datetime.now(AFTR_DISPLAY_TZ).date()
    upcoming    = [p for p in (upcoming_picks or []) if isinstance(p, dict)]
    ctx         = log_context or ""

    def _valid_strict(p: dict) -> bool:
        if _aftr_score(p) < 75:
            return False
        if (_safe_int(p.get("confidence")) or 0) < 6:
            return False
        edge_val = p.get("edge")
        if edge_val is not None:
            try:
                if float(edge_val) <= 0:
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _valid_relaxed(p: dict) -> bool:
        if _aftr_score(p) < 62:
            return False
        if (_safe_int(p.get("confidence")) or 0) < 5:
            return False
        return True

    def _in_local_window(p: dict, start_day: int, end_day: int) -> bool:
        d = _pick_local_date(p, match_by_key)
        if d is None:
            return False
        return (today_local + timedelta(days=start_day)) <= d <= (today_local + timedelta(days=end_day))

    def _target_leg_count(candidates: list[dict]) -> int:
        uniq = {k for p in candidates if (k := _combo_match_key_for_home(p))}
        return 5 if len(uniq) >= 5 else (4 if len(uniq) >= 4 else 3)

    def _select_legs(
        candidates: list[dict],
        target_count: int,
        global_used: set[tuple[str, int]],
    ) -> list[dict]:
        legs:       list[dict]           = []
        used_local: set[tuple[str, int]] = set()

        for p in candidates:
            k = _combo_match_key_for_home(p)
            if not k or k in used_local or k in global_used:
                continue
            legs.append(p)
            used_local.add(k)
            if len(legs) >= target_count:
                break

        # Si faltan patas, relaja solo el constraint global
        if len(legs) < 3:
            for p in candidates:
                k = _combo_match_key_for_home(p)
                if not k or k in used_local:
                    continue
                legs.append(p)
                used_local.add(k)
                if len(legs) >= target_count:
                    break
        return legs

    def _assemble(valid_fn) -> list[dict]:
        all_candidates = [p for p in upcoming if valid_fn(p)]

        def _pool(start_day: int, end_day: int) -> list[dict]:
            return [p for p in all_candidates if _in_local_window(p, start_day, end_day)]

        day_sorted   = sorted(_pool(0, 0), key=lambda p: -_pick_score(p))
        h72_sorted   = sorted(_pool(0, 3), key=lambda p: -_pick_score(p))
        value_sorted = sorted(
            _pool(0, 3),
            key=lambda p: (
                -float(_safe_float(p.get("edge"), 0) or 0),
                -_aftr_score(p),
                -_pick_score(p),
            ),
        )

        combo_specs = [
            {
                "key":         "day",
                "title":       "🔥 Combo del Día",
                "description": "Las mejores selecciones de hoy",
                "tier_badge":  "Seguro",
                "tier_class":  "seguro",
                "sorted":      day_sorted,
            },
            {
                "key":         "72h",
                "title":       "⏳ Combo 72h",
                "description": "Ventana ampliada de oportunidades",
                "tier_badge":  "Balanceado",
                "tier_class":  "balanceado",
                "sorted":      h72_sorted,
            },
            {
                "key":         "value",
                "title":       "💎 Combo Value",
                "description": "Más edge, más cuota, más riesgo controlado",
                "tier_badge":  "Value",
                "tier_class":  "value",
                "sorted":      value_sorted,
            },
        ]

        global_used: set[tuple[str, int]] = set()
        combos: list[dict] = []

        for spec in combo_specs:
            candidates  = spec.get("sorted") or []
            target_count = _target_leg_count(candidates)
            legs_picks   = _select_legs(candidates, target_count, global_used)

            if len(legs_picks) < 3:
                combos.append({
                    **spec,
                    "legs":           [],
                    "combined_odds":  None,
                    "combo_prob_pct": None,
                    "combo_score":    None,
                })
                continue

            for lp in legs_picks:
                k = _combo_match_key_for_home(lp)
                if k:
                    global_used.add(k)

            legs: list[dict] = []
            combined_prob    = 1.0
            combined_odds: float | None = None
            odds_ok          = True
            score_sum        = 0

            for lp in legs_picks:
                mid         = _safe_int(lp.get("match_id")) or _safe_int(lp.get("id"))
                league_code = (lp.get("_league") or lp.get("league") or "").strip()
                m           = match_by_key.get((league_code, mid)) if mid is not None and league_code else None

                home       = lp.get("home") or (m.get("home") if isinstance(m, dict) else "") or "—"
                away       = lp.get("away") or (m.get("away") if isinstance(m, dict) else "") or "—"
                market     = (lp.get("best_market") or lp.get("best_market_name") or "").strip() or "—"
                home_crest = lp.get("home_crest") or (m.get("home_crest") if isinstance(m, dict) else None)
                away_crest = lp.get("away_crest") or (m.get("away_crest") if isinstance(m, dict) else None)
                prob       = _safe_float(lp.get("best_prob"), 0) or 0.0
                leg_odds   = _combo_leg_odds_value(lp)

                aftr_sc_raw = lp.get("aftr_score")
                try:
                    aftr_sc = int(round(float(aftr_sc_raw))) if aftr_sc_raw is not None else _aftr_score(lp)
                except (TypeError, ValueError):
                    aftr_sc = _aftr_score(lp)

                legs.append({
                    "home":       home,
                    "away":       away,
                    "market":     market,
                    "prob":       prob,
                    "home_crest": home_crest,
                    "away_crest": away_crest,
                    "match_id":   mid,
                    "odds_value": leg_odds,
                    "aftr_score": aftr_sc,
                    "utcDate":    lp.get("utcDate") or (m.get("utcDate") if isinstance(m, dict) else None),
                })

                combined_prob *= prob
                score_sum     += aftr_sc
                if leg_odds is None:
                    odds_ok = False
                else:
                    combined_odds = float(leg_odds) if combined_odds is None else combined_odds * float(leg_odds)

            if not odds_ok:
                combined_odds = None

            combos.append({
                **spec,
                "legs":           legs,
                "combo_prob_pct": round(combined_prob * 100, 1),
                "combined_odds":  combined_odds,
                "combo_score":    int(round(score_sum / max(1, len(legs)))),
            })

        return combos

    combos = _assemble(_valid_strict)
    filled = sum(1 for c in combos if len(c.get("legs") or []) >= 3)

    if filled < 3 and upcoming:
        n_strict  = sum(1 for p in upcoming if _valid_strict(p))
        n_relaxed = sum(1 for p in upcoming if _valid_relaxed(p))
        no_key    = sum(1 for p in upcoming if _combo_match_key_for_home(p) is None)
        no_date   = sum(1 for p in upcoming if _pick_local_date(p, match_by_key) is None)
        in_day    = sum(1 for p in upcoming if _in_local_window(p, 0, 0))
        in_72     = sum(1 for p in upcoming if _in_local_window(p, 0, 3))
        logger.warning(
            "premium_combos[%s]: 0 filled | upcoming=%s strict_ok=%s relaxed_ok=%s "
            "no_match_key=%s no_local_date=%s in_today=%s in_72h=%s",
            ctx or "—", len(upcoming), n_strict, n_relaxed, no_key, no_date, in_day, in_72,
        )

        if n_relaxed >= 3:
            combos_r = _assemble(_valid_relaxed)
            filled_r = sum(1 for c in combos_r if len(c.get("legs") or []) >= 3)
            if filled_r > 0:
                logger.warning("premium_combos[%s]: usando thresholds RELAJADOS (AFTR≥62, CONF≥5)", ctx or "—")
                # Merge: keep strict combos where they have legs, fill empty slots with relaxed
                merged = []
                for cs, cr in zip(combos, combos_r):
                    if len(cs.get("legs") or []) >= 3:
                        merged.append(cs)
                    else:
                        merged.append(cr)
                return merged

    return combos


# =========================================================
# Rendering
# =========================================================

def _cv2_short_name(name: str) -> str:
    """Nombre de equipo acortado para cards compactos."""
    n = (name or "").strip()
    n = n.replace("Football Club", "FC").replace("Club Atlético", "Atl.")
    n = " ".join(w for w in n.split() if w.lower() != "hotspur")
    return n.strip()


def _render_home_premium_combo_card(combo: dict) -> str:
    """Renderiza uno de los 3 combo cards premium de la home (diseño cv2 compacto)."""
    if not combo or not isinstance(combo, dict):
        return ""

    title      = combo.get("title") or "Combo"
    tier_badge = combo.get("tier_badge") or "—"
    tier_class = combo.get("tier_class") or "seguro"
    legs       = combo.get("legs") or []
    n          = len(legs)

    prob_pct      = combo.get("combo_prob_pct")
    prob_clamped  = min(100, max(0, float(prob_pct or 0)))
    prob_str      = f"{prob_pct:.1f}%" if prob_pct is not None else "—"
    combined_odds = combo.get("combined_odds")
    odds_str      = f"{combined_odds:.2f}×" if combined_odds is not None else "—"
    score         = combo.get("combo_score")
    score_str     = str(score) if score is not None else "—"

    tc = html_lib.escape(tier_class)
    tb = html_lib.escape(tier_badge)
    ti = html_lib.escape(title)

    if n == 0:
        return (
            f'<div class="card cv2 cv2--{tc} home-premium-combo-card">'
            f'<div class="cv2-head">'
            f'<span class="cv2-badge cv2-badge--{tc}">{tb}</span>'
            f'<span class="cv2-title">{ti}</span>'
            f'</div>'
            f'<div class="cv2-empty muted">No hay inventario suficiente.</div>'
            f'</div>'
        )

    _FALLBACK = "/static/teams/default.svg"
    _fb_esc   = html_lib.escape(_FALLBACK)

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home   = html_lib.escape(_cv2_short_name(it.get("home") or "—"))
        away   = html_lib.escape(_cv2_short_name(it.get("away") or "—"))
        market = html_lib.escape(str(it.get("market") or "—"))
        pct    = round(_safe_float(it.get("prob"), 0) * 100)
        h_src  = html_lib.escape((it.get("home_crest") or "").strip() or _FALLBACK)
        a_src  = html_lib.escape((it.get("away_crest") or "").strip() or _FALLBACK)

        rows.append(
            f'<div class="cv2-leg">'
            f'<div class="cv2-leg-match">'
            f'<img src="{h_src}" class="cv2-crest" loading="lazy" width="16" height="16" '
            f'onerror="this.src=\'{_fb_esc}\';this.onerror=null;"/>'
            f'<span class="cv2-team">{home}</span>'
            f'<span class="cv2-vs">vs</span>'
            f'<img src="{a_src}" class="cv2-crest" loading="lazy" width="16" height="16" '
            f'onerror="this.src=\'{_fb_esc}\';this.onerror=null;"/>'
            f'<span class="cv2-team">{away}</span>'
            f'</div>'
            f'<span class="cv2-mkt">{market}</span>'
            f'<span class="cv2-leg-pct">{pct}%</span>'
            f'</div>'
        )

    return (
        f'<div class="card cv2 cv2--{tc} home-premium-combo-card">'
        # Cabecera: badge de tier + título
        f'<div class="cv2-head">'
        f'<span class="cv2-badge cv2-badge--{tc}">{tb}</span>'
        f'<span class="cv2-title">{ti}</span>'
        f'</div>'
        # Hero: cuota grande + barra de probabilidad
        f'<div class="cv2-hero">'
        f'<div class="cv2-odds-block">'
        f'<div class="cv2-odds-num">{html_lib.escape(odds_str)}</div>'
        f'<div class="cv2-odds-lbl">cuota</div>'
        f'</div>'
        f'<div class="cv2-prob-block">'
        f'<div class="cv2-prob-row">'
        f'<span class="cv2-prob-lbl">Probabilidad</span>'
        f'<span class="cv2-prob-val">{html_lib.escape(prob_str)}</span>'
        f'</div>'
        f'<div class="cv2-bar">'
        f'<div class="cv2-bar-fill cv2-bar-fill--{tc}" style="width:{prob_clamped:.0f}%"></div>'
        f'</div>'
        f'</div>'
        f'</div>'
        # Separador tipo ticket
        f'<div class="cv2-sep"></div>'
        # Legs compactos
        f'<div class="cv2-legs">{"".join(rows)}</div>'
        # Footer: conteo + AFTR score badge
        f'<div class="cv2-foot">'
        f'<span class="cv2-count">{n} sel.</span>'
        f'<span class="cv2-aftr-badge">AFTR {html_lib.escape(score_str)}</span>'
        f'</div>'
        f'</div>'
    )


def _render_combo_of_the_day(combo: dict) -> str:
    """Renderiza la sección Combo del Día (estilo combo-card)."""
    if not combo or not isinstance(combo, dict):
        return ""
    legs = combo.get("legs") or []
    if not legs:
        return ""

    risk          = html_lib.escape(str(combo.get("risk") or "—"))
    score         = combo.get("combo_score")
    score_str     = str(score) if score is not None else "—"
    prob_pct      = combo.get("combo_prob_pct")
    prob_str      = f"{prob_pct}%" if prob_pct is not None else "—"
    combined_odds = combo.get("combined_odds")
    odds_str      = f" • Odds combinadas: {combined_odds:.2f}" if combined_odds is not None else ""

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home   = it.get("home") or "—"
        away   = it.get("away") or "—"
        market = it.get("market") or "—"
        p      = round(float(it.get("prob") or 0) * 100, 0)
        rows.append(
            f'<div class="combo-leg">'
            f'<div class="combo-leg-top">'
            f'<span class="combo-match">'
            f'{_team_with_crest(it.get("home_crest"), home)}'
            f'<span class="vs">vs</span>'
            f'{_team_with_crest(it.get("away_crest"), away)}'
            f'</span>'
            f'<span class="combo-pct">{p:.0f}%</span>'
            f'</div>'
            f'<div class="combo-market">{html_lib.escape(str(market))}</div>'
            f'{_combo_leg_kickoff_html(it)}'
            f'</div>'
        )

    return (
        f'<div class="card combo-card combo-of-the-day">'
        f'<div class="combo-head">'
        f'<div class="combo-title">🔥 AFTR Combo del Día</div>'
        f'<span class="combo-tier {risk.lower()}">{risk}</span>'
        f'</div>'
        f'<div class="combo-sub">Prob total: <b>{prob_str}</b>{odds_str} • Puntuación combo: <b>{score_str}</b></div>'
        f'<div class="combo-legs">{"".join(rows)}</div>'
        f'</div>'
    )


def _render_combo_card(combo: dict | None, tier_label: str) -> str:
    """Renderiza un combo card de home (slot SAFE / MEDIUM / AGGRESSIVE)."""
    tier_lower = tier_label.lower()
    if not combo or not isinstance(combo, dict):
        return (
            f'<div class="card combo-card combo-card-slot combo-tier-{tier_lower}">'
            f'<div class="combo-head">'
            f'<div class="combo-title">{html_lib.escape(tier_label)}</div>'
            f'<span class="combo-tier {tier_lower}">{html_lib.escape(tier_label)}</span>'
            f'</div>'
            f'<div class="combo-empty muted">No {html_lib.escape(tier_label)} combo today.</div>'
            f'</div>'
        )

    legs          = combo.get("legs") or []
    risk          = html_lib.escape(str(combo.get("risk") or tier_label))
    score         = combo.get("combo_score")
    score_str     = str(score) if score is not None else "—"
    prob_pct      = combo.get("combo_prob_pct")
    prob_str      = f"{prob_pct}%" if prob_pct is not None else "—"
    combined_odds = combo.get("combined_odds")
    odds_str      = f" • Odds {combined_odds:.2f}" if combined_odds is not None else ""

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home   = it.get("home") or "—"
        away   = it.get("away") or "—"
        market = it.get("market") or "—"
        p      = round(float(it.get("prob") or 0) * 100, 0)
        rows.append(
            f'<div class="combo-leg">'
            f'<div class="combo-leg-top">'
            f'<span class="combo-match">'
            f'{_team_with_crest(it.get("home_crest"), home)}'
            f'<span class="vs">vs</span>'
            f'{_team_with_crest(it.get("away_crest"), away)}'
            f'</span>'
            f'<span class="combo-pct">{p:.0f}%</span>'
            f'</div>'
            f'<div class="combo-market">{html_lib.escape(str(market))}</div>'
            f'{_combo_leg_kickoff_html(it)}'
            f'</div>'
        )

    return (
        f'<div class="card combo-card combo-card-slot combo-tier-{tier_lower}">'
        f'<div class="combo-head">'
        f'<div class="combo-title">{html_lib.escape(tier_label)}</div>'
        f'<span class="combo-tier {tier_lower}">{risk}</span>'
        f'</div>'
        f'<div class="combo-sub">Prob: <b>{prob_str}</b>{odds_str} • AFTR score: <b>{score_str}</b></div>'
        f'<div class="combo-legs">{"".join(rows)}</div>'
        f'</div>'
    )


def _render_combo_box(combo: dict) -> str:
    """Renderiza un combo box genérico (dashboard de liga)."""
    if not isinstance(combo, dict):
        return ""

    legs = combo.get("legs") or []
    if not isinstance(legs, list) or not legs:
        return "<div class='muted'>No hay combinada disponible.</div>"

    tier     = html_lib.escape(str(combo.get("tier") or "—"))
    name     = html_lib.escape(str(combo.get("name") or "Combinada"))
    prob     = html_lib.escape(str(combo.get("combo_prob_pct") or "—"))
    fair     = combo.get("fair")
    fair_txt = f" • cuota ~ {html_lib.escape(str(fair))}" if fair is not None else ""

    rows = []
    for it in legs:
        if not isinstance(it, dict):
            continue
        home   = it.get("home") or "—"
        away   = it.get("away") or "—"
        market = it.get("market") or "—"
        p      = round(float(it.get("prob") or 0) * 100, 0)
        rows.append(
            f'<div class="combo-leg">'
            f'<div class="combo-leg-top">'
            f'<span class="combo-match">'
            f'{_team_with_crest(it.get("home_crest"), home)}'
            f'<span class="vs">vs</span>'
            f'{_team_with_crest(it.get("away_crest"), away)}'
            f'</span>'
            f'<span class="combo-pct">{p:.0f}%</span>'
            f'</div>'
            f'<div class="combo-market">{html_lib.escape(str(market))}</div>'
            f'{_combo_leg_kickoff_html(it)}'
            f'</div>'
        )

    return (
        f'<div class="card combo-card">'
        f'<div class="combo-head">'
        f'<div class="combo-title">{name}</div>'
        f'<span class="combo-tier {tier.lower()}">{tier}</span>'
        f'</div>'
        f'<div class="combo-sub">Prob total: <b>{prob}%</b>{fair_txt}</div>'
        f'<div class="combo-legs">{"".join(rows)}</div>'
        f'</div>'
    )

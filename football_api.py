# -*- coding: utf-8 -*-
"""Conectores de resultados para el Mundial 2026.

Proveedores soportados:
  - "espn" (POR DEFECTO): API pública de ESPN, GRATIS y SIN API key. Casi en
        tiempo real (marcador en vivo y resultado a los pocos minutos del final).
  - "openfootball": datos públicos y GRATIS, SIN API key, pero tarda unas horas.
        https://github.com/openfootball/worldcup.json
  - "apifootball": API-Football / api-sports.io (requiere PLAN DE PAGO para 2026;
        el plan gratuito NO da acceso a la temporada 2026).

Los resultados se aplican comparando por nombre de equipo (inglés -> español).
También existe edición manual desde el panel de Admin como respaldo.
"""
import re
import json
import time
import unicodedata
from datetime import datetime, timedelta
import requests

import config
from models import db, Team, Fixture, set_setting, get_setting
from seed_data import TEAM_ALIASES


# ---------------------------------------------------------------------------
# Normalización y alias de nombres (proveedor en inglés <-> español)
# ---------------------------------------------------------------------------
def _norm(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


_ALIAS_INDEX = {}
for es_name, aliases in TEAM_ALIASES.items():
    _ALIAS_INDEX[_norm(es_name)] = es_name
    for a in aliases:
        _ALIAS_INDEX[_norm(a)] = es_name


def resolve_team_es(name):
    """Nombre en español a partir de cualquier alias, o None si no se reconoce
    (p. ej. marcadores de posición como '2A' o 'UEFA Path D winner')."""
    return _ALIAS_INDEX.get(_norm(name))


def map_round(api_round, group=None):
    r = (api_round or "").lower()
    if group or "group" in r or "matchday" in r:
        return "GROUP"
    if "round of 32" in r or "1/16" in r:
        return "R32"
    if "round of 16" in r or "1/8" in r:
        return "R16"
    if "quarter" in r or "1/4" in r:
        return "QF"
    if "semi" in r or "1/2" in r:
        return "SF"
    if "third" in r or "3rd" in r:      # antes que 'final'
        return "3RD"
    if "final" in r:
        return "FINAL"
    return None


def _status_short(short):
    if short in ("FT", "AET", "PEN", "WO"):
        return "FINISHED"
    if short in ("1H", "2H", "HT", "ET", "BT", "P", "LIVE", "INT", "SUSP"):
        return "LIVE"
    return "SCHEDULED"


# ===========================================================================
# PROVEEDOR 1: openfootball (gratis, sin key)
# ===========================================================================
OPENFOOTBALL_URL = ("https://raw.githubusercontent.com/openfootball/"
                    "worldcup.json/master/2026/worldcup.json")
_KO_NUM = {"3RD": 103, "FINAL": 104}  # estas dos no traen 'num'


def fetch_openfootball():
    r = requests.get(OPENFOOTBALL_URL, timeout=25)
    r.raise_for_status()
    return r.json()


def _normalize_openfootball(data):
    """Convierte el JSON de openfootball a la lista normalizada interna."""
    out = []
    for m in data.get("matches", []):
        group = m.get("group")
        stage = map_round(m.get("round"), group)
        if not stage:
            continue
        sc = m.get("score") or {}
        ft = sc.get("ft")
        status = "SCHEDULED"
        h90 = a90 = None
        winner_es = None
        if isinstance(ft, list) and len(ft) == 2 and ft[0] is not None:
            h90, a90 = ft[0], ft[1]
            status = "FINISHED"
            # ganador del cruce (penaltis/prórroga si existen)
            seq = sc.get("p") or sc.get("et") or ft
            if isinstance(seq, list) and len(seq) == 2 and seq[0] != seq[1]:
                winner_es = resolve_team_es(m["team1"] if seq[0] > seq[1] else m["team2"])
        out.append({
            "stage": stage,
            "group_letter": group[-1] if group else None,
            "match_num": m.get("num") or (_KO_NUM.get(stage) if stage in _KO_NUM else None),
            "home": resolve_team_es(m.get("team1")),
            "away": resolve_team_es(m.get("team2")),
            "status": status, "home_goals": h90, "away_goals": a90,
            "winner_es": winner_es,
            "date": m.get("date"), "stadium": m.get("ground"),
        })
    return out


# ===========================================================================
# PROVEEDOR 2: API-Football (de pago para 2026)
# ===========================================================================
def _api_get(path, api_key, params):
    resp = requests.get(f"{config.API_FOOTBALL_BASE}{path}",
                        headers={"x-apisports-key": api_key}, params=params, timeout=25)
    resp.raise_for_status()
    data = resp.json()
    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))
    return data.get("response", [])


def _normalize_apifootball(api_key):
    raw = _api_get("/fixtures", api_key,
                   {"league": config.API_LEAGUE_ID, "season": config.API_SEASON})
    out = []
    for item in raw:
        fixture, league, teams = item["fixture"], item["league"], item["teams"]
        score = item.get("score", {}) or {}
        ft = score.get("fulltime", {}) or {}
        goals = item.get("goals", {}) or {}
        stage = map_round(league.get("round"))
        if not stage:
            continue
        h = ft.get("home") if ft.get("home") is not None else goals.get("home")
        a = ft.get("away") if ft.get("away") is not None else goals.get("away")
        win_es = None
        if (teams.get("home") or {}).get("winner"):
            win_es = resolve_team_es(teams["home"]["name"])
        elif (teams.get("away") or {}).get("winner"):
            win_es = resolve_team_es(teams["away"]["name"])
        out.append({
            "stage": stage, "group_letter": None,
            "match_num": None, "api_id": fixture["id"],
            "home": resolve_team_es(teams["home"]["name"]),
            "away": resolve_team_es(teams["away"]["name"]),
            "status": _status_short(fixture["status"]["short"]),
            "home_goals": h, "away_goals": a, "winner_es": win_es,
            "stadium": (fixture.get("venue") or {}).get("name"),
        })
    return out


# ===========================================================================
# PROVEEDOR 3: ESPN (GRATIS, sin clave, casi en tiempo real)
#   Su API pública trae el Mundial 2026 con nombres reales y marcador en vivo.
# ===========================================================================
ESPN_SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
                   "fifa.world/scoreboard")
# rangos de fechas del torneo (en 2 trozos para no toparse con límites)
ESPN_DATE_RANGES = ["20260611-20260628", "20260628-20260720"]


def fetch_espn():
    # Si hay un "relay" configurado (GitHub publica el JSON de ESPN en un host
    # permitido por PythonAnywhere), se lee de ahí. Así funciona incluso en el
    # plan gratis, que NO puede salir directo a site.api.espn.com.
    relay = (get_setting("espn_relay_url") or "").strip()
    if relay:
        try:
            r = requests.get(relay, params={"t": int(time.time() // 60)}, timeout=25)
            r.raise_for_status()
            return r.json().get("events", []) or []
        except Exception:  # noqa  -> si el relay falla, no rompe la sincronización
            return []
    # Sin relay: acceso DIRECTO a ESPN (sirve en local o en planes con internet
    # sin filtrar).
    events = []
    for rng in ESPN_DATE_RANGES:
        try:
            r = requests.get(ESPN_SCOREBOARD, params={"dates": rng, "limit": 300},
                             timeout=25)
            r.raise_for_status()
            events.extend(r.json().get("events", []) or [])
        except Exception:  # noqa  -> un trozo fallido no rompe la sincronización
            pass
    return events


def _espn_status(state, completed):
    if state == "post" and completed:
        return "FINISHED"
    if state == "in":
        return "LIVE"
    return "SCHEDULED"


# ESPN -> ronda del app, y primer nº de partido de cada ronda (#N -> base + N)
_ESPN_KO_ROUND = {
    "round-of-32": "R32", "round-of-16": "R16", "quarterfinals": "QF",
    "semifinals": "SF", "3rd-place-match": "3RD", "final": "FINAL",
}
_KO_BASE = {"R32": 72, "R16": 88, "QF": 96, "SF": 100, "3RD": 102, "FINAL": 103}


def _espn_sides(comp):
    """(home_es, away_es, home_goals, away_goals, winner_es) de una competición."""
    home = away = hs = as_ = winner_es = None
    for c in comp.get("competitors", []):
        team = c.get("team") or {}
        es = (resolve_team_es(team.get("name"))
              or resolve_team_es(team.get("displayName"))
              or resolve_team_es(team.get("shortDisplayName")))
        try:
            sc = int(c.get("score"))
        except (TypeError, ValueError):
            sc = None
        if c.get("homeAway") == "home":
            home, hs = es, sc
        else:
            away, as_ = es, sc
        if c.get("winner"):
            winner_es = es
    return home, away, hs, as_, winner_es


def _espn_kickoff_peru(iso):
    """ESPN entrega la hora en UTC; el app guarda en hora de Perú (UTC-5)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "").split("+")[0])
        return (dt - timedelta(hours=5)).isoformat()
    except Exception:  # noqa
        return None


def _normalize_espn(events):
    out, seen, ko = [], set(), {}
    for ev in events:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        eid = ev.get("id")
        if eid in seen:                      # evita duplicados entre rangos solapados
            continue
        seen.add(eid)
        slug = ((ev.get("season") or {}).get("slug") or "").lower()
        stt = (comp.get("status") or {}).get("type") or {}
        status = _espn_status(stt.get("state"), stt.get("completed"))
        if "group" in slug:
            home, away, hs, as_, winner_es = _espn_sides(comp)
            out.append({
                "stage": "GROUP", "group_letter": None, "match_num": None,
                "home": home, "away": away, "status": status,
                "home_goals": hs, "away_goals": as_, "winner_es": winner_es,
                "date": (ev.get("date") or "")[:10],
                "stadium": (comp.get("venue") or {}).get("fullName"),
            })
        elif slug in _ESPN_KO_ROUND:
            ko.setdefault(_ESPN_KO_ROUND[slug], []).append(ev)

    # Eliminatorias: se numeran 1..N por id dentro de cada ronda y se mapean a los
    # huecos del app (R32 #1 -> partido 73, ... #16 -> 88; R16 #1 -> 89; etc.).
    for rnd, lst in ko.items():
        lst.sort(key=lambda e: int(e.get("id") or 0))
        for i, ev in enumerate(lst, 1):
            comp = (ev.get("competitions") or [{}])[0]
            stt = (comp.get("status") or {}).get("type") or {}
            home, away, hs, as_, winner_es = _espn_sides(comp)
            out.append({
                "stage": rnd, "group_letter": None,
                "match_num": _KO_BASE[rnd] + i,
                "home": home, "away": away,
                "status": _espn_status(stt.get("state"), stt.get("completed")),
                "home_goals": hs, "away_goals": as_, "winner_es": winner_es,
                "date": _espn_kickoff_peru(ev.get("date")),
                "stadium": (comp.get("venue") or {}).get("fullName"),
            })
    return out


# ===========================================================================
# Aplicación a la base de datos (común a todos los proveedores)
# ===========================================================================
def _team_id(es, cache):
    if es not in cache:
        t = Team.query.filter_by(name_es=es).first()
        cache[es] = t.id if t else None
    return cache[es]


def _apply_fixture(fx, cache, summary):
    stage = fx.get("stage")
    if not stage:
        return
    hid = _team_id(fx["home"], cache) if fx.get("home") else None
    aid = _team_id(fx["away"], cache) if fx.get("away") else None

    if stage == "GROUP":
        if not (hid and aid):
            return
        f = Fixture.query.filter_by(stage="GROUP", home_team_id=hid, away_team_id=aid).first()
        if not f:  # orden invertido
            f = Fixture.query.filter_by(stage="GROUP", home_team_id=aid, away_team_id=hid).first()
            if f and fx["home_goals"] is not None:
                fx = dict(fx, home_goals=fx["away_goals"], away_goals=fx["home_goals"])
        if not f:
            summary["unmatched"].append(f"GROUP {fx.get('home')} vs {fx.get('away')}")
            return
        # Solo escribir si algo cambió de verdad: así una sincronización sin
        # novedades no genera escrituras (clave para no saturar el disco de red).
        changed = False
        if f.status != fx["status"]:
            f.status = fx["status"]
            changed = True
        if fx["status"] == "FINISHED":
            if f.home_goals != fx["home_goals"] or f.away_goals != fx["away_goals"]:
                f.home_goals, f.away_goals = fx["home_goals"], fx["away_goals"]
                changed = True
            summary["finished"] += 1
        if changed:
            summary["groups_updated"] += 1
        return

    # --- Eliminatorias: upsert por match_num o api_id ---
    f = None
    if fx.get("match_num"):
        f = Fixture.query.filter_by(match_num=fx["match_num"]).first()
    if not f and fx.get("api_id"):
        f = Fixture.query.filter_by(api_fixture_id=fx["api_id"]).first()
    if not f:
        f = Fixture(stage=stage, match_num=fx.get("match_num"), api_fixture_id=fx.get("api_id"))
        db.session.add(f)
        summary["ko_created"] += 1
    else:
        summary["ko_updated"] += 1
    f.stage = stage
    if fx.get("stadium"):
        f.stadium = fx["stadium"]
    if fx.get("date"):                     # actualiza la hora del cruce cada sync
        try:
            f.kickoff = datetime.fromisoformat(fx["date"])
        except Exception:  # noqa
            pass
    if hid:
        f.home_team_id = hid
    if aid:
        f.away_team_id = aid
    f.status = fx["status"]
    if fx["status"] == "FINISHED":
        f.home_goals, f.away_goals = fx["home_goals"], fx["away_goals"]
        summary["finished"] += 1
        if stage == "FINAL":
            champ = fx.get("winner_es")
            if not champ and fx["home_goals"] is not None and fx["home_goals"] != fx["away_goals"]:
                champ = fx["home"] if fx["home_goals"] > fx["away_goals"] else fx["away"]
            if champ:
                set_setting("champion_es", champ)


def compute_group_standings():
    """Clasificación real de cada grupo (solo grupos COMPLETOS) a partir
    de los resultados: puntos, dif. de goles, goles a favor."""
    teams = Team.query.all()
    out = {}
    for g in "ABCDEFGHIJKL":
        gteams = [t for t in teams if t.group_letter == g]
        stat = {t.id: {"pts": 0, "gf": 0, "ga": 0, "name": t.name_es} for t in gteams}
        fixtures = Fixture.query.filter_by(stage="GROUP", group_letter=g).all()
        if not fixtures or any(not f.finished for f in fixtures):
            continue  # grupo incompleto -> no fijar todavía
        for f in fixtures:
            hs, as_ = stat.get(f.home_team_id), stat.get(f.away_team_id)
            if not hs or not as_:
                continue
            hs["gf"] += f.home_goals; hs["ga"] += f.away_goals
            as_["gf"] += f.away_goals; as_["ga"] += f.home_goals
            if f.home_goals > f.away_goals:
                hs["pts"] += 3
            elif f.home_goals < f.away_goals:
                as_["pts"] += 3
            else:
                hs["pts"] += 1; as_["pts"] += 1
        from scoring import rank_group_2026   # desempate oficial 2026 (mano a mano)
        ordered_ids = rank_group_2026(
            list(stat.keys()),
            {tid: s["name"] for tid, s in stat.items()},
            [(f.home_team_id, f.away_team_id, f.home_goals, f.away_goals) for f in fixtures])
        out[g] = [stat[tid]["name"] for tid in ordered_ids]
    return out


# ===========================================================================
# Punto de entrada: sincronizar
# ===========================================================================
def sync_results(api_key=None, provider=None, ingest_fixtures=None, ingest_standings=None):
    """Trae y aplica los resultados del proveedor configurado.

    Para pruebas se puede pasar `ingest_fixtures` (lista ya normalizada) y/o
    `ingest_standings` ({grupo: [equipos...]})."""
    from datetime import datetime
    provider = provider or get_setting("provider") or config.RESULTS_PROVIDER
    summary = {"provider": provider, "groups_updated": 0, "ko_created": 0,
               "ko_updated": 0, "finished": 0, "unmatched": []}

    if ingest_fixtures is not None:
        fixtures = ingest_fixtures
    elif provider == "apifootball":
        if not api_key:
            raise RuntimeError("Falta la API key de API-Football.")
        fixtures = _normalize_apifootball(api_key)
    elif provider == "espn":
        fixtures = _normalize_espn(fetch_espn())
    else:  # openfootball (por defecto)
        fixtures = _normalize_openfootball(fetch_openfootball())

    cache = {}
    for fx in fixtures:
        _apply_fixture(fx, cache, summary)
    db.session.commit()

    # (La Fase 2 se abre sola con el modo "Automático" en cuanto hay equipos en
    # dieciseisavos; no hace falta tocar el ajuste aquí.)

    # Clasificación de grupos (para campeón de grupo)
    standings = ingest_standings if ingest_standings is not None else compute_group_standings()
    if standings:
        set_setting("actual_standings", json.dumps(standings, ensure_ascii=False))

    set_setting("last_sync", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"))
    db.session.commit()
    return summary

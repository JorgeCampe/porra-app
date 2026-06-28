# -*- coding: utf-8 -*-
"""Motor de puntuación de la Porra Mundial 2026 (dos fases).

Todo se deriva de los marcadores que predice el usuario:
 - FASE 1 (grupos): la clasificación de cada grupo sale de los marcadores
   (puntos, dif. de goles, goles a favor). De ahí: campeón de grupo y
   los que pasan a dieciseisavos (2 primeros + 8 mejores terceros).
 - FASE 2 (eliminatorias): el usuario predice el marcador de cada cruce y el
   ganador avanza (cuadro interactivo). De ahí: quién llega a octavos, cuartos,
   semis, la final y el campeón.
"""
import json

from config import (MATCH_POINTS, BONUS_POINTS, PHASE2_MATCH_STAGES,
                    KO_ROUND_NUMS, BRACKET_FEEDS)
from models import (db, User, Team, Fixture, MatchPrediction,
                    AdvancePrediction, get_setting)
from seed_data import GROUP_LETTERS


def _sign(h, a):
    return 0 if h == a else (1 if h > a else -1)


def score_match(pred_h, pred_a, act_h, act_a, pts):
    if pred_h is None or pred_a is None or act_h is None or act_a is None:
        return 0
    s = 0
    if _sign(pred_h, pred_a) == _sign(act_h, act_a):
        s += pts["signo"]
    if pred_h == act_h:
        s += pts["goal_side"]
    if pred_a == act_a:
        s += pts["goal_side"]
    if pred_h == act_h and pred_a == act_a:
        s += pts["exact"]
    return s


def _match_flags(ph, pa, ah, aa):
    """(signo_ok, exacto_ok) como 0/1, para los desempates."""
    if ph is None or pa is None or ah is None or aa is None:
        return (0, 0)
    return (1 if _sign(ph, pa) == _sign(ah, aa) else 0,
            1 if (ph == ah and pa == aa) else 0)


def score_ko_cross(pred_h, pred_a, uth, utw, f, pts):
    """Puntos del MARCADOR de un cruce de eliminatorias.
       uth/utw = equipos que el usuario puso en ese cruce (local/visitante).
       - Cruce EXACTO (tus dos equipos son los que se enfrentan): puntuación
         completa -> signo + goles + marcador exacto.
       - Cruce PARCIAL (solo UNO de tus equipos está realmente ahí): cuentan el
         SIGNO (acertar el resultado/quién gana, aunque el rival sea otro) y los
         GOLES de ESE equipo si los acertaste; NO cuenta el marcador exacto ni los
         goles del rival.
       - Si no coincide ninguno: 0."""
    if pred_h is None or pred_a is None or f.home_goals is None or f.away_goals is None:
        return 0
    if uth == f.home_team_id and utw == f.away_team_id:
        return score_match(pred_h, pred_a, f.home_goals, f.away_goals, pts)
    home_ok = (uth == f.home_team_id)
    away_ok = (utw == f.away_team_id)
    if not (home_ok or away_ok):
        return 0
    s = 0
    if _sign(pred_h, pred_a) == _sign(f.home_goals, f.away_goals):
        s += pts["signo"]                       # acertaste el resultado del partido
    if home_ok and pred_h == f.home_goals:
        s += pts["goal_side"]                   # goles del equipo tuyo que sí está
    if away_ok and pred_a == f.away_goals:
        s += pts["goal_side"]
    return s


def _order(rows):
    # Orden por criterios TOTALES (puntos, dif. de goles, goles, nombre).
    # Se usa para elegir los mejores terceros (equipos de grupos distintos, donde
    # el mano a mano no aplica).
    return sorted(rows, key=lambda s: (-s["pts"], -(s["gf"] - s["ga"]), -s["gf"], s["name"]))


# ---------------------------------------------------------------------------
# Desempate OFICIAL de grupos del Mundial 2026:
#   1) Puntos.
#   2) Entre los EMPATADOS en puntos: puntos, diferencia de goles y goles del
#      MANO A MANO (solo los partidos entre esos equipos).
#   3) Si siguen iguales: diferencia de goles total y goles totales.
#   (El juego limpio y el ranking FIFA no se modelan; como último recurso se usa
#   el nombre para que el orden sea estable.)
# ---------------------------------------------------------------------------
def _grp_stats(ids, results):
    idset = set(ids)
    st = {t: [0, 0, 0] for t in idset}          # [puntos, dif_goles, goles_favor]
    for h, a, hg, ag in results:
        if h not in idset or a not in idset or hg is None or ag is None:
            continue
        st[h][2] += hg; st[h][1] += hg - ag
        st[a][2] += ag; st[a][1] += ag - hg
        if hg > ag:
            st[h][0] += 3
        elif hg < ag:
            st[a][0] += 3
        else:
            st[h][0] += 1; st[a][0] += 1
    return st


def rank_group_2026(team_ids, names, results):
    """Ordena los ids de un grupo (mejor primero) con el desempate del Mundial
    2026. results: lista de (home_id, away_id, home_goals, away_goals) jugados."""
    team_ids = list(team_ids)
    overall = _grp_stats(team_ids, results)

    def resolve(group):
        if len(group) == 1:
            return group
        h = _grp_stats(group, results)              # mano a mano entre 'group'
        ordered = sorted(group, key=lambda t: (-h[t][0], -h[t][1], -h[t][2]))
        out, i = [], 0
        while i < len(ordered):
            j, key = i, tuple(h[ordered[i]])
            while j < len(ordered) and tuple(h[ordered[j]]) == key:
                j += 1
            sub = ordered[i:j]
            if len(sub) == 1:
                out += sub
            elif len(sub) < len(group):
                out += resolve(sub)                 # reaplica mano a mano al subconjunto
            else:                                   # no separó -> criterios totales
                out += sorted(sub, key=lambda t: (-overall[t][1], -overall[t][2],
                                                  (names.get(t) or "").lower()))
            i = j
        return out

    out, i = [], 0
    by_pts = sorted(team_ids, key=lambda t: -overall[t][0])
    while i < len(by_pts):
        j, p = i, overall[by_pts[i]][0]
        while j < len(by_pts) and overall[by_pts[j]][0] == p:
            j += 1
        out += resolve(by_pts[i:j])
        i = j
    return out


def _table_from_scores(team_names, matches, get_score):
    stat = {tid: {"id": tid, "name": nm, "pts": 0, "gf": 0, "ga": 0}
            for tid, nm in team_names.items()}
    results = []
    for hid, aid in matches:
        sc = get_score(hid, aid)
        if not sc or sc[0] is None or sc[1] is None:
            continue
        if hid not in stat or aid not in stat:
            continue
        h, a = sc
        stat[hid]["gf"] += h; stat[hid]["ga"] += a
        stat[aid]["gf"] += a; stat[aid]["ga"] += h
        if h > a:
            stat[hid]["pts"] += 3
        elif h < a:
            stat[aid]["pts"] += 3
        else:
            stat[hid]["pts"] += 1; stat[aid]["pts"] += 1
        results.append((hid, aid, h, a))
    names = {tid: s["name"] for tid, s in stat.items()}
    order = rank_group_2026(stat.keys(), names, results)
    return [stat[t] for t in order]


def _group_structures():
    teams = {t.id: t for t in Team.query.all()}
    group_names, group_matches = {}, {}
    for t in teams.values():
        group_names.setdefault(t.group_letter, {})[t.id] = t.name_es
    for f in Fixture.query.filter_by(stage="GROUP").all():
        group_matches.setdefault(f.group_letter, []).append((f.home_team_id, f.away_team_id))
    return group_names, group_matches


def predicted_group_tables(user_id):
    group_names, group_matches = _group_structures()
    pred = {mp.fixture_id: (mp.pred_home, mp.pred_away)
            for mp in MatchPrediction.query.filter_by(user_id=user_id).all()}
    fx_by_teams = {(f.home_team_id, f.away_team_id): f.id
                   for f in Fixture.query.filter_by(stage="GROUP").all()}

    def get(h, a):
        fid = fx_by_teams.get((h, a))
        return pred.get(fid) if fid else None

    return {g: _table_from_scores(group_names.get(g, {}), group_matches.get(g, []), get)
            for g in GROUP_LETTERS}


def participant_breakdown(uid):
    """Desglose de puntos de FASE 1 por grupo para un participante:
       - puntos de cada partido (resultado),
       - bono de campeón de grupo (acertado o no),
       - bono por equipos que predijo a dieciseisavos y que pasaron.
    Devuelve {grupo: {...}} y un flag r32_known (si ya se conocen los clasificados)."""
    _, group_matches = _group_structures()
    fixtures = {f.id: f for f in Fixture.query.filter_by(stage="GROUP").all()}
    fx_by_teams = {(f.home_team_id, f.away_team_id): f.id for f in fixtures.values()}
    pred = {mp.fixture_id: (mp.pred_home, mp.pred_away)
            for mp in MatchPrediction.query.filter_by(user_id=uid).all()}
    tables = predicted_group_tables(uid)
    gw = actual_group_winners()
    adv = actual_advanced()["R32"]
    r32_known = bool(adv)

    # equipos que el usuario predijo que avanzan (top 2 por grupo + 8 mejores terceros)
    pred_adv, thirds = set(), []
    for g in GROUP_LETTERS:
        t = tables[g]
        if len(t) >= 1:
            pred_adv.add(t[0]["id"])
        if len(t) >= 2:
            pred_adv.add(t[1]["id"])
        if len(t) >= 3:
            thirds.append(t[2])
    for s in _order(thirds)[:8]:
        pred_adv.add(s["id"])

    out = {}
    for g in GROUP_LETTERS:
        match_pts, gpts = {}, 0
        for (hid, aid) in group_matches.get(g, []):
            fid = fx_by_teams.get((hid, aid))
            f, p = fixtures.get(fid), pred.get(fid)
            pt = None
            if f and f.finished and p:
                pt = score_match(p[0], p[1], f.home_goals, f.away_goals, MATCH_POINTS["GROUP"])
                gpts += pt
            match_pts[fid] = pt
        wp = tables[g][0] if tables[g] else None
        winner_ok = bool(wp and gw.get(g) and wp["id"] == gw.get(g))
        adv_rows = []
        for idx, s in enumerate(tables[g][:3]):
            if s["id"] not in pred_adv:
                continue
            if idx == 2:                       # solo el 3º si entró entre los 8 mejores
                label = s["name"] + " (3º)"
            else:
                label = s["name"]
            adv_rows.append({"name": label,
                             "ok": (s["id"] in adv) if r32_known else None})
        adv_pts = sum(BONUS_POINTS["R32"] for r in adv_rows if r["ok"])
        out[g] = {
            "match_pts": match_pts, "group_pts": gpts,
            "winner_name": wp["name"] if wp else None,
            "winner_ok": winner_ok,
            "winner_decided": g in gw,
            "winner_pts": BONUS_POINTS["GROUP_WINNER"] if winner_ok else 0,
            "adv_rows": adv_rows, "adv_pts": adv_pts,
        }
    return out, r32_known


# ---------------------------------------------------------------------------
# Cuadro de eliminatorias del usuario (derivado de pred_winner_id guardado)
# ---------------------------------------------------------------------------
def _other(num, teams_at, winner):
    th, tw = teams_at.get(num, (None, None))
    w = winner.get(num)
    if not w:
        return None
    return tw if w == th else th


def user_ko_bracket(ko_by_num, winner_of):
    """Devuelve (teams_at, winner) por nº de partido. winner_of(num)->team_id."""
    teams_at, winner = {}, {}
    for num in sorted(ko_by_num):
        f = ko_by_num[num]
        if num <= 88:
            teams_at[num] = (f.home_team_id, f.away_team_id)   # R32: equipos reales
        else:
            typ, a, b = BRACKET_FEEDS[num]
            ta = winner.get(a) if typ == "W" else _other(a, teams_at, winner)
            tb = winner.get(b) if typ == "W" else _other(b, teams_at, winner)
            teams_at[num] = (ta, tb)
        winner[num] = winner_of(num)
    return teams_at, winner


# ---------------------------------------------------------------------------
# Resultados reales
# ---------------------------------------------------------------------------
def actual_advanced():
    adv = {k: set() for k in ("R32", "R16", "QF", "SF", "FINAL")}
    for f in Fixture.query.filter(Fixture.stage.in_(adv.keys())).all():
        if f.home_team_id:
            adv[f.stage].add(f.home_team_id)
        if f.away_team_id:
            adv[f.stage].add(f.away_team_id)
    return adv


def actual_group_winners():
    raw = get_setting("actual_standings")
    out = {}
    if not raw:
        return out
    for g, teams_es in json.loads(raw).items():
        if teams_es:
            t = Team.query.filter_by(name_es=teams_es[0]).first()
            if t:
                out[g] = t.id
    return out


def actual_champion():
    es = get_setting("champion_es")
    if es:
        t = Team.query.filter_by(name_es=es).first()
        if t:
            return t.id
    f = Fixture.query.filter_by(stage="FINAL").filter(Fixture.home_goals.isnot(None)).first()
    if f and f.home_goals != f.away_goals:
        return f.home_team_id if f.home_goals > f.away_goals else f.away_team_id
    return None


def participant_ko_breakdown(uid):
    """Desglose de FASE 2 para un participante:
       - match_pts: puntos de cada cruce (si terminó y el cruce previsto coincide
         con el real); None si aún no aplica.
       - bonus: por ronda (R16/QF/SF/FINAL) los equipos que el usuario lleva a esa
         ronda con ✓ (acertó), ✗ (falló, ronda ya decidida) o None (pendiente).
       - champion: equipo previsto campeón con su ✓/✗/pendiente.
    Devuelve None si todavía no hay cuadro de eliminatorias."""
    ko_by_num = {f.match_num: f for f in
                 Fixture.query.filter(Fixture.match_num >= 73).all()}
    if not ko_by_num:
        return None
    mp_obj = {mp.fixture_id: mp for mp in
              MatchPrediction.query.filter_by(user_id=uid).all()}
    adv = actual_advanced()
    champ = actual_champion()
    teams = {t.id: t for t in Team.query.all()}

    def winner_of(num):
        mp = mp_obj.get(ko_by_num[num].id) if num in ko_by_num else None
        return mp.pred_winner_id if mp else None

    teams_at, winner = user_ko_bracket(ko_by_num, winner_of)

    # ¿cuántos partidos terminaron por etapa? (para saber si una ronda ya se decidió)
    done, total = {}, {}
    for f in ko_by_num.values():
        total[f.stage] = total.get(f.stage, 0) + 1
        if f.finished:
            done[f.stage] = done.get(f.stage, 0) + 1

    def all_done(stage):
        return total.get(stage, 0) > 0 and done.get(stage, 0) == total[stage]

    # puntos por cruce (si terminó y el cruce previsto coincide con el real)
    match_pts = {}
    for num, f in ko_by_num.items():
        mp = mp_obj.get(f.id)
        pt = None
        if f.finished and mp and mp.pred_home is not None:
            uth, utw = teams_at.get(num, (None, None))
            pt = score_ko_cross(mp.pred_home, mp.pred_away, uth, utw, f,
                                MATCH_POINTS[f.stage])
        match_pts[f.id] = pt

    # equipos que el usuario lleva a cada ronda (en orden, sin repetir)
    feeder = {"R16": "R32", "QF": "R16", "SF": "QF", "FINAL": "SF"}
    bonus = {}
    for rnd, frm in feeder.items():
        decided = all_done(frm)
        seen, rows = set(), []
        for n in KO_ROUND_NUMS[frm]:
            w = winner.get(n)
            if not w or w in seen:
                continue
            seen.add(w)
            ok = True if w in adv[rnd] else (False if decided else None)
            rows.append({"name": teams[w].name_es if w in teams else "?", "ok": ok})
        pts = sum(BONUS_POINTS[rnd] for r in rows if r["ok"])
        bonus[rnd] = {"rows": rows, "pts": pts, "decided": decided}

    # marcadores agrupados POR ETAPA, usando el CUADRO del usuario (los equipos que
    # él hace avanzar), porque los fixtures de R16+ aún no tienen equipos reales.
    matches_by_stage = []
    for stage in ("R32", "R16", "QF", "SF", "FINAL", "3RD"):
        rows = []
        for num in KO_ROUND_NUMS.get(stage, []):
            f = ko_by_num.get(num)
            mp = mp_obj.get(f.id) if f else None
            if not mp or mp.pred_home is None:
                continue
            uth, utw = teams_at.get(num, (None, None))
            rows.append({
                "home": teams[uth].name_es if uth in teams else "?",
                "away": teams[utw].name_es if utw in teams else "?",
                "ph": mp.pred_home, "pa": mp.pred_away,
                "pts": match_pts.get(f.id),
            })
        if rows:
            matches_by_stage.append({"stage": stage, "rows": rows})

    cp = winner.get(104)
    champion = {
        "name": teams[cp].name_es if cp in teams else None,
        "ok": (cp == champ) if champ else None,
        "pts": BONUS_POINTS["CHAMPION"] if (champ and cp == champ) else 0,
    }
    return {"match_pts": match_pts, "bonus": bonus, "champion": champion,
            "matches_by_stage": matches_by_stage}


# ---------------------------------------------------------------------------
# Clasificación de la porra
# ---------------------------------------------------------------------------
def compute_standings():
    users = User.query.all()
    group_names, group_matches = _group_structures()
    finished_group_fx = [f for f in Fixture.query.filter_by(stage="GROUP").all() if f.finished]
    fx_by_teams = {(f.home_team_id, f.away_team_id): f.id
                   for f in Fixture.query.filter_by(stage="GROUP").all()}
    ko_by_num = {f.match_num: f for f in Fixture.query.filter(Fixture.match_num >= 73).all()}

    adv = actual_advanced()
    gw = actual_group_winners()
    champ = actual_champion()

    mp_obj = {(mp.user_id, mp.fixture_id): mp for mp in MatchPrediction.query.all()}
    mpred = {k: (v.pred_home, v.pred_away) for k, v in mp_obj.items()}

    rows = []
    for u in users:
        # ---------------- FASE 1 ----------------
        def getter(h, a, uid=u.id):
            fid = fx_by_teams.get((h, a))
            return mpred.get((uid, fid)) if fid else None

        pred_tables = {g: _table_from_scores(group_names.get(g, {}),
                                             group_matches.get(g, []), getter)
                       for g in GROUP_LETTERS}
        f1_gw = 0
        pred_r32 = set()
        thirds_pool = []
        for g in GROUP_LETTERS:
            tbl = pred_tables[g]
            if not tbl:
                continue
            if gw.get(g) and tbl[0]["id"] == gw[g]:
                f1_gw += BONUS_POINTS["GROUP_WINNER"]
            if len(tbl) >= 1:
                pred_r32.add(tbl[0]["id"])
            if len(tbl) >= 2:
                pred_r32.add(tbl[1]["id"])
            if len(tbl) >= 3:
                thirds_pool.append(tbl[2])
        for s in _order(thirds_pool)[:8]:
            pred_r32.add(s["id"])
        f1_r32 = len(pred_r32 & adv["R32"]) * BONUS_POINTS["R32"]
        f1_matches = f1_exacts = f1_signs = 0
        for f in finished_group_fx:
            p = mpred.get((u.id, f.id))
            if p:
                f1_matches += score_match(p[0], p[1], f.home_goals, f.away_goals,
                                          MATCH_POINTS["GROUP"])
                sg, ex = _match_flags(p[0], p[1], f.home_goals, f.away_goals)
                f1_signs += sg; f1_exacts += ex
        phase1 = f1_matches + f1_gw + f1_r32

        # ---------------- FASE 2 (cuadro) ----------------
        def winner_of(num, uid=u.id):
            mp = mp_obj.get((uid, ko_by_num[num].id)) if num in ko_by_num else None
            return mp.pred_winner_id if mp else None

        teams_at, winner = user_ko_bracket(ko_by_num, winner_of)
        reach = {"R16": set(), "QF": set(), "SF": set(), "FINAL": set()}
        for n in KO_ROUND_NUMS["R32"]:
            if winner.get(n):
                reach["R16"].add(winner[n])
        for n in KO_ROUND_NUMS["R16"]:
            if winner.get(n):
                reach["QF"].add(winner[n])
        for n in KO_ROUND_NUMS["QF"]:
            if winner.get(n):
                reach["SF"].add(winner[n])
        for n in KO_ROUND_NUMS["SF"]:
            if winner.get(n):
                reach["FINAL"].add(winner[n])
        champ_pred = winner.get(104)

        f2_r16 = len(reach["R16"] & adv["R16"]) * BONUS_POINTS["R16"]
        f2_qf = len(reach["QF"] & adv["QF"]) * BONUS_POINTS["QF"]
        f2_sf = len(reach["SF"] & adv["SF"]) * BONUS_POINTS["SF"]
        f2_fin = len(reach["FINAL"] & adv["FINAL"]) * BONUS_POINTS["FINAL"]
        f2_champ = BONUS_POINTS["CHAMPION"] if (champ and champ_pred == champ) else 0

        # marcadores de eliminatorias: puntúan si el cruce previsto coincide
        f2_matches = f2_exacts = f2_signs = 0
        for num, f in ko_by_num.items():
            if not f.finished:
                continue
            mp = mp_obj.get((u.id, f.id))
            if not mp or mp.pred_home is None:
                continue
            uth, utw = teams_at.get(num, (None, None))
            f2_matches += score_ko_cross(mp.pred_home, mp.pred_away, uth, utw, f,
                                         MATCH_POINTS[f.stage])
            if uth == f.home_team_id and utw == f.away_team_id:
                sg, ex = _match_flags(mp.pred_home, mp.pred_away, f.home_goals, f.away_goals)
                f2_signs += sg; f2_exacts += ex

        phase2 = f2_matches + f2_r16 + f2_qf + f2_sf + f2_fin + f2_champ

        rows.append({
            "user": u.username, "user_id": u.id,
            "p1_matches": f1_matches, "p1_gw": f1_gw, "p1_r32": f1_r32, "phase1": phase1,
            "p2_matches": f2_matches, "p2_r16": f2_r16, "p2_qf": f2_qf,
            "p2_sf": f2_sf, "p2_fin": f2_fin, "p2_champ": f2_champ, "phase2": phase2,
            "general": phase1 + phase2,
            # desempates: exactos y signos acertados
            "p1_exacts": f1_exacts, "p1_signs": f1_signs,
            "p2_exacts": f2_exacts, "p2_signs": f2_signs,
            "g_exacts": f1_exacts + f2_exacts, "g_signs": f1_signs + f2_signs,
        })

    def ranked(pk, ek, sk):
        # desempate: puntos -> resultados exactos -> signos acertados -> nombre
        ordered = sorted(rows, key=lambda r: (-r[pk], -r[ek], -r[sk], r["user"].lower()))
        out, prev, rank = [], None, 0
        for i, r in enumerate(ordered, 1):
            key = (r[pk], r[ek], r[sk])
            if key != prev:
                rank = i
                prev = key
            rr = dict(r)
            rr["rank"] = rank
            out.append(rr)
        return out

    return {"phase1": ranked("phase1", "p1_exacts", "p1_signs"),
            "phase2": ranked("phase2", "p2_exacts", "p2_signs"),
            "general": ranked("general", "g_exacts", "g_signs")}

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


def _order(rows):
    return sorted(rows, key=lambda s: (-s["pts"], -(s["gf"] - s["ga"]), -s["gf"], s["name"]))


def _table_from_scores(team_names, matches, get_score):
    stat = {tid: {"id": tid, "name": nm, "pts": 0, "gf": 0, "ga": 0}
            for tid, nm in team_names.items()}
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
    return _order(list(stat.values()))


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
            if uth == f.home_team_id and utw == f.away_team_id:
                f2_matches += score_match(mp.pred_home, mp.pred_away, f.home_goals,
                                          f.away_goals, MATCH_POINTS[f.stage])
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

# -*- coding: utf-8 -*-
"""Porra Mundial 2026 - plataforma web (Flask).

Arranque local:   python app.py   ->  http://localhost:5000
"""
import os
import json
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:  # noqa
    pass

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, abort)
from flask_login import (LoginManager, login_user, logout_user,
                         login_required, current_user)

import config
from models import (db, User, Team, Fixture, MatchPrediction,
                    GroupRankPrediction, AdvancePrediction,
                    get_setting, set_setting)
from seed_data import (GROUP_LETTERS, GROUP_TEAMS, GROUP_FIXTURES)
import football_api
import scoring

login_manager = LoginManager()


# ---------------------------------------------------------------------------
# Fábrica de la aplicación
# ---------------------------------------------------------------------------
def create_app():
    app = Flask(__name__)
    app.config.from_object(config)
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "login"
    login_manager.login_message = "Inicia sesión para acceder."
    app.jinja_env.globals["flag_url"] = config.flag_url

    with app.app_context():
        _enable_sqlite_resilience()
        db.create_all()
        _migrate()
        seed_db()
        try:
            print(f"[Porra] Base de datos: {app.config['SQLALCHEMY_DATABASE_URI']}",
                  flush=True)
            print(f"[Porra] Usuarios en esa base: {User.query.count()}", flush=True)
        except Exception:  # noqa
            pass

    register_routes(app)
    return app


def _enable_sqlite_resilience():
    """Hace SQLite mucho más tolerante a accesos simultáneos sobre el disco de
    red de PythonAnywhere: espera ante bloqueos en vez de fallar, y usa un journal
    compatible con NFS (evita 'database is locked' / 'disk I/O error')."""
    try:
        if db.engine.url.get_backend_name() != "sqlite":
            return
        from sqlalchemy import event

        @event.listens_for(db.engine, "connect")
        def _set_pragmas(dbapi_con, _rec):  # noqa
            cur = dbapi_con.cursor()
            cur.execute("PRAGMA busy_timeout=30000")   # espera hasta 30 s un lock
            cur.execute("PRAGMA journal_mode=DELETE")  # NO wal (no sirve en NFS)
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()
    except Exception:  # noqa
        pass


def _migrate():
    """Migraciones ligeras para bases de datos ya existentes."""
    try:
        with db.engine.connect() as con:
            cols = [r[1] for r in con.exec_driver_sql(
                "PRAGMA table_info(match_predictions)").fetchall()]
            if "pred_winner_id" not in cols:
                con.exec_driver_sql(
                    "ALTER TABLE match_predictions ADD COLUMN pred_winner_id INTEGER")
                con.commit()
    except Exception:  # noqa
        pass
    # En Postgres, agranda settings.value a TEXT: la foto de posiciones (JSON de
    # los 12 grupos) supera los 255 caracteres y rompía la sincronización.
    try:
        if db.engine.url.get_backend_name() in ("postgresql", "postgres"):
            with db.engine.connect() as con:
                con.exec_driver_sql("ALTER TABLE settings ALTER COLUMN value TYPE TEXT")
                con.commit()
    except Exception:  # noqa
        pass


@login_manager.user_loader
def load_user(uid):
    return db.session.get(User, int(uid))


# ---------------------------------------------------------------------------
# Siembra de datos (equipos, grupos, 72 partidos de grupo)
# ---------------------------------------------------------------------------
def seed_db():
    if Team.query.first() is None:
        for g in GROUP_LETTERS:
            for name in GROUP_TEAMS[g]:
                db.session.add(Team(name_es=name, group_letter=g))
        db.session.commit()

    if Fixture.query.filter_by(stage="GROUP").first() is None:
        idx = {t.name_es: t.id for t in Team.query.all()}
        for fx in GROUP_FIXTURES:
            db.session.add(Fixture(
                match_num=fx["num"], stage="GROUP", group_letter=fx["group"],
                kickoff=datetime.fromisoformat(fx["date"]), stadium=fx["stadium"],
                home_team_id=idx[fx["home"]], away_team_id=idx[fx["away"]],
                status="SCHEDULED"))
        db.session.commit()

    # huecos de eliminatorias (73..104) para poder predecir el cuadro completo
    existing_ko = {f.match_num for f in Fixture.query.filter(Fixture.match_num >= 73).all()}
    for num, stage in config.KO_NUM_STAGE.items():
        if num not in existing_ko:
            db.session.add(Fixture(match_num=num, stage=stage, status="SCHEDULED"))
    db.session.commit()

    # estados por defecto de las fases
    if get_setting("phase1_state") is None:
        set_setting("phase1_state", "auto")     # auto/open/locked
    if get_setting("phase2_state") is None:
        set_setting("phase2_state", "closed")   # closed/open/locked
    if get_setting("provider") is None:
        set_setting("provider", config.RESULTS_PROVIDER)  # espn/openfootball/apifootball
    if get_setting("api_key") is None and config.API_FOOTBALL_KEY:
        set_setting("api_key", config.API_FOOTBALL_KEY)

    # Conversión única de horarios a hora de Perú (UTC-5).
    # Los partidos venían en hora de España (UTC+2); Perú va 7 horas por detrás.
    if get_setting("tz_peru_done") != "1":
        from datetime import timedelta
        for f in Fixture.query.filter(Fixture.stage == "GROUP",
                                      Fixture.kickoff.isnot(None)).all():
            f.kickoff = f.kickoff - timedelta(hours=7)
        set_setting("tz_peru_done", "1")
        db.session.commit()

    # Actualización única: pasar al proveedor ESPN (gratis y casi en tiempo real).
    # Solo afecta a cuentas que seguían en el openfootball por defecto; si el
    # admin elige otro proveedor a propósito más adelante, no se vuelve a tocar.
    if get_setting("provider_espn_done") != "1":
        if (get_setting("provider") or "openfootball") == "openfootball":
            set_setting("provider", "espn")
        set_setting("provider_espn_done", "1")
        db.session.commit()


# ---------------------------------------------------------------------------
# Estado de las fases
# ---------------------------------------------------------------------------
def phase1_locked():
    state = get_setting("phase1_state", "auto")
    if state == "open":
        return False
    if state == "locked":
        return True
    return datetime.utcnow() >= datetime.fromisoformat(config.PHASE1_LOCK_ISO)


def phase2_state():
    return get_setting("phase2_state", "closed")  # closed/open/locked


def get_api_key():
    return get_setting("api_key") or config.API_FOOTBALL_KEY


# Sincronización automática "perezosa" (no necesita tareas de pago): se dispara
# cuando alguien visita la web. Sincroniza cada 15 min cuando hay un partido en
# juego o recién terminado (ventana de ~2h45 desde el inicio), y cada 6 h el
# resto del tiempo como red de seguridad. Así los resultados entran ~15 min
# después de acabar cada partido.
AUTOSYNC_ACTIVE_MIN = 15        # frecuencia en horario de partidos
AUTOSYNC_IDLE_HOURS = 6         # frecuencia fuera de horario de partidos
KICKOFF_UTC_OFFSET = -5         # las horas guardadas son UTC-5 (hora de Perú)
MATCH_WINDOW_MIN = 165          # 2h45: duración del partido + margen tras el final


def maybe_autosync():
    from datetime import timedelta
    try:
        now = datetime.utcnow()
        last_iso = get_setting("auto_sync_iso")
        last = datetime.fromisoformat(last_iso) if last_iso else datetime(2000, 1, 1)

        # ¿hay algún partido (aún sin resultado) en juego o recién terminado?
        active = False
        window = timedelta(minutes=MATCH_WINDOW_MIN)
        for f in Fixture.query.filter(Fixture.kickoff.isnot(None),
                                      Fixture.status != "FINISHED").all():
            k_utc = f.kickoff - timedelta(hours=KICKOFF_UTC_OFFSET)
            if k_utc <= now <= k_utc + window:
                active = True
                break

        throttle = (timedelta(minutes=AUTOSYNC_ACTIVE_MIN) if active
                    else timedelta(hours=AUTOSYNC_IDLE_HOURS))
        if now - last < throttle:
            return
        set_setting("auto_sync_iso", now.isoformat())   # marca antes (evita dobles)
        football_api.sync_results(api_key=get_api_key(),
                                  provider=get_setting("provider") or config.RESULTS_PROVIDER)
    except Exception:  # noqa  -> nunca rompe la página
        pass


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
_DIAS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
          "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


def _fecha_es(dt):
    return f"{_DIAS[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]}"


def register_routes(app):

    @app.context_processor
    def inject_globals():
        return {"phase1_locked": phase1_locked(), "phase2_state": phase2_state(),
                "last_sync": get_setting("last_sync"), "now": datetime.utcnow(),
                "results_source": {"espn": "ESPN", "openfootball": "openfootball",
                                   "apifootball": "API-Football"}.get(
                                       get_setting("provider"), "ESPN")}

    # Ante un error transitorio de la base (lock / I/O en disco de red), en vez
    # de tumbar la web mostramos una página que se recarga sola en 3 s.
    from sqlalchemy.exc import OperationalError

    @app.errorhandler(OperationalError)
    def _db_busy(e):  # noqa
        try:
            db.session.rollback()
        except Exception:  # noqa
            pass
        return ("<!doctype html><html lang='es'><head><meta charset='utf-8'>"
                "<meta http-equiv='refresh' content='3'><title>Un momento…</title>"
                "<style>body{font-family:system-ui;background:#0b3a26;color:#e9fbf1;"
                "text-align:center;padding:18vh 8vw}</style></head><body>"
                "<h2>Un momento…</h2><p>La porra está recibiendo muchos accesos a la "
                "vez. Esta página se recargará sola en unos segundos.</p></body></html>"), 503

    # ---- Clasificación (home) ----
    @app.route("/")
    def index():
        maybe_autosync()   # actualiza resultados solo si toca (cada AUTOSYNC_HOURS)
        tab = request.args.get("tab", "general")
        if tab not in ("general", "phase1", "phase2"):
            tab = "general"
        standings = scoring.compute_standings()
        return render_template("index.html", standings=standings, tab=tab)

    # ---- Auth ----
    @app.route("/register", methods=["GET", "POST"])
    def register():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            email = request.form.get("email", "").strip().lower()
            pw = request.form.get("password", "")
            if not username or not email or len(pw) < 4:
                flash("Completa todos los campos (contraseña de 4+ caracteres).", "error")
            elif User.query.filter((User.username == username) | (User.email == email)).first():
                flash("Ese usuario o email ya existe.", "error")
            else:
                u = User(username=username, email=email)
                u.set_password(pw)
                # el primer usuario (o el ADMIN_EMAIL) es administrador
                if User.query.count() == 0 or email == os.environ.get("ADMIN_EMAIL", "").lower():
                    u.is_admin = True
                db.session.add(u)
                db.session.commit()
                login_user(u)
                flash("¡Cuenta creada! Rellena tus predicciones de la fase de grupos.", "ok")
                return redirect(url_for("predict_groups"))
        return render_template("register.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            ident = request.form.get("identifier", "").strip().lower()
            pw = request.form.get("password", "")
            u = User.query.filter((User.email == ident) |
                                  (User.username == request.form.get("identifier", "").strip())).first()
            if u and u.check_password(pw):
                login_user(u)
                return redirect(request.args.get("next") or url_for("index"))
            flash("Credenciales incorrectas.", "error")
        return render_template("login.html")

    @app.route("/logout")
    @login_required
    def logout():
        logout_user()
        return redirect(url_for("index"))

    # ---- Predicciones FASE 1: grupos ----
    @app.route("/predict/groups", methods=["GET", "POST"])
    @login_required
    def predict_groups():
        locked = phase1_locked()
        fixtures = Fixture.query.filter_by(stage="GROUP").order_by(Fixture.match_num).all()
        teams = {t.id: t for t in Team.query.all()}

        if request.method == "POST":
            if locked:
                flash("La fase de grupos está cerrada; ya no puedes editar.", "error")
                return redirect(url_for("predict_groups"))
            # solo marcadores: la clasificación de cada grupo se calcula sola
            for f in fixtures:
                h = request.form.get(f"m_{f.id}_h", "")
                a = request.form.get(f"m_{f.id}_a", "")
                _save_match_pred(current_user.id, f.id, h, a)
            db.session.commit()
            flash("Predicciones de la fase de grupos guardadas.", "ok")
            return redirect(url_for("predict_groups"))

        # GET: cargar marcadores existentes
        mp = {p.fixture_id: p for p in
              MatchPrediction.query.filter_by(user_id=current_user.id).all()}
        by_group = {g: [] for g in GROUP_LETTERS}
        for f in fixtures:
            by_group[f.group_letter].append(f)
        return render_template("predict_groups.html", by_group=by_group, teams=teams,
                               group_teams=GROUP_TEAMS, mp=mp, locked=locked)

    # ---- Predicciones FASE 2: eliminatorias ----
    @app.route("/predict/bracket", methods=["GET", "POST"])
    @login_required
    def predict_bracket():
        state = phase2_state()
        if state == "closed":
            return render_template("predict_bracket.html", closed=True)
        locked = (state == "locked")
        ko = {f.match_num: f for f in Fixture.query.filter(Fixture.match_num >= 73).all()}
        teams = {t.id: t for t in Team.query.all()}

        if request.method == "POST":
            if locked:
                flash("Las eliminatorias están cerradas.", "error")
                return redirect(url_for("predict_bracket"))
            for num, f in ko.items():
                _save_match_pred(current_user.id, f.id,
                                 request.form.get(f"m_{f.id}_h", ""),
                                 request.form.get(f"m_{f.id}_a", ""))
            db.session.commit()
            _reconstruct_user_bracket(current_user.id, ko, request.form)
            flash("Predicciones de eliminatorias guardadas.", "ok")
            return redirect(url_for("predict_bracket"))

        mp = {p.fixture_id: p for p in
              MatchPrediction.query.filter_by(user_id=current_user.id).all()}
        rounds = []
        for stage in ["R32", "R16", "QF", "SF", "FINAL", "3RD"]:
            slots = [ko[n] for n in config.KO_ROUND_NUMS[stage] if n in ko]
            rounds.append((stage, config.STAGE_LABELS[stage], slots))
        teams_json = {t.id: {"name": t.name_es, "flag": config.flag_url(t.name_es)}
                      for t in teams.values()}
        num2fid = {n: f.id for n, f in ko.items()}
        r32_ready = any(ko[n].home_team_id for n in config.KO_ROUND_NUMS["R32"] if n in ko)
        return render_template("predict_bracket.html", closed=False, locked=locked,
                               rounds=rounds, mp=mp, teams_json=teams_json,
                               feeds=config.BRACKET_FEEDS, num2fid=num2fid,
                               r32_ready=r32_ready)

    # ---- Partidos y resultados ----
    @app.route("/matches")
    def matches():
        fixtures = Fixture.query.all()
        mp, points = {}, {}
        if current_user.is_authenticated:
            mp = {p.fixture_id: p for p in
                  MatchPrediction.query.filter_by(user_id=current_user.id).all()}
            # puntos que ganó el usuario en cada partido finalizado
            for f in fixtures:
                if f.stage == "GROUP" and f.finished:
                    p = mp.get(f.id)
                    if p and p.pred_home is not None:
                        points[f.id] = scoring.score_match(
                            p.pred_home, p.pred_away, f.home_goals, f.away_goals,
                            config.MATCH_POINTS["GROUP"])
            ko_by_num = {f.match_num: f for f in fixtures if f.match_num and f.match_num >= 73}
            if ko_by_num:
                def winner_of(num):
                    fx = ko_by_num.get(num)
                    p = mp.get(fx.id) if fx else None
                    return p.pred_winner_id if p else None
                teams_at, _ = scoring.user_ko_bracket(ko_by_num, winner_of)
                for num, f in ko_by_num.items():
                    if f.finished:
                        p = mp.get(f.id)
                        if p and p.pred_home is not None:
                            uth, utw = teams_at.get(num, (None, None))
                            if uth == f.home_team_id and utw == f.away_team_id:
                                points[f.id] = scoring.score_match(
                                    p.pred_home, p.pred_away, f.home_goals,
                                    f.away_goals, config.MATCH_POINTS[f.stage])

        # Predicciones de TODOS por partido (solo si la fase ya está revelada)
        reveal_group = phase1_locked() and current_user.is_authenticated
        reveal_ko = (phase2_state() == "locked") and current_user.is_authenticated
        all_preds = {}
        if reveal_group or reveal_ko:
            unames = {u.id: u.username for u in User.query.all()}
            by_fx = {}
            for p in MatchPrediction.query.all():
                by_fx.setdefault(p.fixture_id, []).append(p)
            for f in fixtures:
                revealed = reveal_group if f.stage == "GROUP" else reveal_ko
                if not revealed:
                    continue
                rows = []
                for p in by_fx.get(f.id, []):
                    if p.pred_home is None:
                        continue
                    pts = None
                    if f.stage == "GROUP" and f.finished:
                        pts = scoring.score_match(p.pred_home, p.pred_away,
                                                  f.home_goals, f.away_goals,
                                                  config.MATCH_POINTS["GROUP"])
                    rows.append({"user": unames.get(p.user_id, "?"), "uid": p.user_id,
                                 "ph": p.pred_home, "pa": p.pred_away, "pts": pts})
                rows.sort(key=lambda r: (-(r["pts"] or 0), r["user"].lower()))
                all_preds[f.id] = rows

        # Secciones por FECHA (hora de Perú)
        def srt(fs):
            return sorted(fs, key=lambda f: (f.kickoff or datetime.max, f.match_num or 0))

        sections, cur = [], None
        for f in srt([x for x in fixtures if x.kickoff]):
            d = f.kickoff.date()
            if d != cur:
                sections.append((_fecha_es(f.kickoff), []))
                cur = d
            sections[-1][1].append(f)
        undated = srt([f for f in fixtures if not f.kickoff and
                       (f.home_team_id or f.away_team_id)])
        if undated:
            sections.append(("Eliminatorias (por definir)", undated))
        return render_template("matches.html", sections=sections, mp=mp,
                               points=points, all_preds=all_preds)

    @app.route("/rules")
    def rules():
        return render_template("rules.html", mp=config.MATCH_POINTS,
                               bp=config.BONUS_POINTS, maxs=config.MAXIMUMS,
                               labels=config.STAGE_LABELS)

    # ---- Ficha de un participante (desglose de puntos) ----
    @app.route("/participante/<int:uid>")
    def participant(uid):
        u = db.session.get(User, uid)
        if not u:
            abort(404)
        st = scoring.compute_standings()
        row = next((r for r in st["general"] if r["user_id"] == uid), None)
        p1rank = next((r["rank"] for r in st["phase1"] if r["user_id"] == uid), None)
        p2rank = next((r["rank"] for r in st["phase2"] if r["user_id"] == uid), None)
        maxes = {"p1_matches": 432, "p1_gw": 72, "p1_r32": 128,
                 "p2_matches": 480, "p2_r16": 64, "p2_qf": 48,
                 "p2_sf": 32, "p2_fin": 20, "p2_champ": 25}

        # Revelar las predicciones cuando la fase esté CERRADA (solo a usuarios logueados)
        reveal1 = phase1_locked() and current_user.is_authenticated
        reveal2 = (phase2_state() == "locked") and current_user.is_authenticated
        pred_groups = pred_tables = pred_ko = champ_pred = bd = None
        r32_known = False
        if reveal1 or reveal2:
            preds = {p.fixture_id: p for p in
                     MatchPrediction.query.filter_by(user_id=uid).all()}
        if reveal1:
            pred_tables = scoring.predicted_group_tables(uid)
            bd, r32_known = scoring.participant_breakdown(uid)
            pred_groups = {}
            for f in Fixture.query.filter_by(stage="GROUP").order_by(Fixture.match_num).all():
                pred_groups.setdefault(f.group_letter, []).append((f, preds.get(f.id)))
        if reveal2:
            ko = {f.match_num: f for f in Fixture.query.filter(Fixture.match_num >= 73).all()}
            fp = preds.get(ko[104].id) if 104 in ko else None
            champ_pred = db.session.get(Team, fp.pred_winner_id) if (fp and fp.pred_winner_id) else None
            order = ["R32", "R16", "QF", "SF", "3RD", "FINAL"]
            pred_ko = []
            for f in sorted(ko.values(), key=lambda x: (order.index(x.stage) if x.stage in order else 9, x.match_num or 0)):
                p = preds.get(f.id)
                if p and p.pred_home is not None and f.home_team_id and f.away_team_id:
                    pred_ko.append((f, p))
        return render_template("participant.html", u=u, r=row, maxes=maxes,
                               p1rank=p1rank, p2rank=p2rank, labels=config.STAGE_LABELS,
                               pred_groups=pred_groups, pred_tables=pred_tables,
                               pred_ko=pred_ko, champ_pred=champ_pred,
                               bd=bd, r32_known=r32_known,
                               revealed=(reveal1 or reveal2))

    # ---- Admin ----
    @app.route("/admin", methods=["GET", "POST"])
    @login_required
    def admin():
        if not current_user.is_admin:
            abort(403)
        msg = None
        if request.method == "POST":
            action = request.form.get("action")
            if action == "save_key":
                set_setting("api_key", request.form.get("api_key", "").strip())
                msg = "API key guardada."
            elif action == "provider":
                set_setting("provider", request.form.get("provider", "openfootball"))
                msg = "Proveedor de resultados actualizado."
            elif action == "save_relay":
                set_setting("espn_relay_url", request.form.get("espn_relay_url", "").strip())
                msg = "URL del relay de ESPN guardada."
            elif action == "phases":
                set_setting("phase1_state", request.form.get("phase1_state", "auto"))
                set_setting("phase2_state", request.form.get("phase2_state", "closed"))
                msg = "Estado de las fases actualizado."
            elif action == "refresh":
                provider = get_setting("provider", "openfootball")
                try:
                    s = football_api.sync_results(api_key=get_api_key(), provider=provider)
                    msg = (f"Sincronizado ({s['provider']}). Grupos: {s['groups_updated']}, "
                           f"KO creados: {s['ko_created']}, finalizados: {s['finished']}."
                           + (f" Sin emparejar: {len(s['unmatched'])}" if s['unmatched'] else ""))
                except Exception as e:  # noqa
                    msg = f"Error al sincronizar: {e}"
            flash(msg, "ok")
            return redirect(url_for("admin"))

        users = User.query.order_by(User.created_at).all()
        nfix = Fixture.query.count()
        nfin = Fixture.query.filter_by(status="FINISHED").count()
        return render_template("admin.html", api_key=get_api_key(), users=users,
                               provider=get_setting("provider", "openfootball"),
                               espn_relay_url=get_setting("espn_relay_url", ""),
                               p1=get_setting("phase1_state", "auto"),
                               p2=get_setting("phase2_state", "closed"),
                               nfix=nfix, nfin=nfin)

    # ---- Resultados manuales (respaldo) ----
    @app.route("/admin/results", methods=["GET", "POST"])
    @login_required
    def admin_results():
        if not current_user.is_admin:
            abort(403)
        order = {"GROUP": 0, "R32": 1, "R16": 2, "QF": 3, "SF": 4, "3RD": 5, "FINAL": 6}
        fixtures = Fixture.query.all()
        fixtures = [f for f in fixtures if f.home_team_id and f.away_team_id]
        fixtures.sort(key=lambda f: (order.get(f.stage, 9), f.match_num or 999))
        if request.method == "POST":
            for f in fixtures:
                h = request.form.get(f"h_{f.id}", "").strip()
                a = request.form.get(f"a_{f.id}", "").strip()
                if h == "" or a == "":
                    continue
                try:
                    f.home_goals, f.away_goals = int(h), int(a)
                    f.status = "FINISHED"
                except ValueError:
                    pass
            db.session.commit()
            # recomputar clasificación de grupos y campeón
            import json as _json
            st = football_api.compute_group_standings()
            if st:
                set_setting("actual_standings", _json.dumps(st, ensure_ascii=False))
            fin = Fixture.query.filter_by(stage="FINAL").filter(
                Fixture.home_goals.isnot(None)).first()
            if fin and fin.home_goals != fin.away_goals:
                w = fin.home if fin.home_goals > fin.away_goals else fin.away
                set_setting("champion_es", w.name_es)
            flash("Resultados guardados y clasificación recalculada.", "ok")
            return redirect(url_for("admin_results"))
        return render_template("admin_results.html", fixtures=fixtures,
                               labels=config.STAGE_LABELS)

    @app.route("/admin/make/<int:uid>")
    @login_required
    def make_admin(uid):
        if not current_user.is_admin:
            abort(403)
        u = db.session.get(User, uid)
        if u:
            u.is_admin = not u.is_admin
            db.session.commit()
        return redirect(url_for("admin"))


def _other_team(num, teams_at, winner):
    """El equipo de un partido que NO ganó (para el 3er puesto)."""
    th, tw = teams_at.get(num, (None, None))
    w = winner.get(num)
    if not w:
        return None
    return tw if w == th else th


def _reconstruct_user_bracket(uid, ko, form):
    """Reconstruye el cuadro del usuario a partir de sus marcadores y, en caso
    de empate, su elección de quién pasa (tb_<fid>). Guarda pred_winner_id."""
    preds = {p.fixture_id: p for p in MatchPrediction.query.filter_by(user_id=uid).all()}
    score, tb = {}, {}
    for num, f in ko.items():
        p = preds.get(f.id)
        score[num] = (p.pred_home, p.pred_away) if p else (None, None)
        v = form.get(f"tb_{f.id}", "")
        tb[num] = int(v) if v.isdigit() else None
    teams_at, winner = {}, {}
    for num in sorted(ko):
        f = ko[num]
        if num <= 88:                       # dieciseisavos: equipos reales
            teams_at[num] = (f.home_team_id, f.away_team_id)
        else:
            typ, a, b = config.BRACKET_FEEDS[num]
            ta = winner.get(a) if typ == "W" else _other_team(a, teams_at, winner)
            tbb = winner.get(b) if typ == "W" else _other_team(b, teams_at, winner)
            teams_at[num] = (ta, tbb)
        th, tw = teams_at[num]
        h, a2 = score[num]
        w = None
        if h is not None and a2 is not None and th and tw:
            if h > a2:
                w = th
            elif h < a2:
                w = tw
            elif tb[num] in (th, tw):
                w = tb[num]
        winner[num] = w
        p = preds.get(f.id)
        if p:
            p.pred_winner_id = w
    db.session.commit()


def _save_match_pred(uid, fid, h, a):
    h = h.strip() if isinstance(h, str) else h
    a = a.strip() if isinstance(a, str) else a
    p = MatchPrediction.query.filter_by(user_id=uid, fixture_id=fid).first()
    if h == "" or a == "" or h is None or a is None:
        return
    try:
        hi, ai = int(h), int(a)
    except (ValueError, TypeError):
        return
    if hi < 0 or ai < 0:
        return
    if p:
        p.pred_home, p.pred_away = hi, ai
    else:
        db.session.add(MatchPrediction(user_id=uid, fixture_id=fid,
                                       pred_home=hi, pred_away=ai))


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

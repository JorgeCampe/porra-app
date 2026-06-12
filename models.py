# -*- coding: utf-8 -*-
"""Modelos de base de datos (SQLAlchemy)."""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(60), unique=True, nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)


class Team(db.Model):
    __tablename__ = "teams"
    id = db.Column(db.Integer, primary_key=True)
    name_es = db.Column(db.String(80), unique=True, nullable=False)   # nombre en español
    group_letter = db.Column(db.String(1))                            # A..L
    logo = db.Column(db.String(255))                                  # url escudo (de la API)


class Fixture(db.Model):
    """Un partido. Los de grupos se siembran desde el Excel; los de
    eliminatorias se crean/actualizan desde la API cuando se conocen."""
    __tablename__ = "fixtures"
    id = db.Column(db.Integer, primary_key=True)
    match_num = db.Column(db.Integer, unique=True)        # 1..104 (None si KO sin nº)
    stage = db.Column(db.String(8), nullable=False)       # GROUP/R32/R16/QF/SF/3RD/FINAL
    group_letter = db.Column(db.String(1))               # solo grupos
    kickoff = db.Column(db.DateTime)
    stadium = db.Column(db.String(80))

    home_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"))
    away_team_id = db.Column(db.Integer, db.ForeignKey("teams.id"))

    # Resultado real (90'/tiempo reglamentario, usado para signo/goles/exacto)
    home_goals = db.Column(db.Integer)
    away_goals = db.Column(db.Integer)
    status = db.Column(db.String(16), default="SCHEDULED")  # SCHEDULED/LIVE/FINISHED
    api_fixture_id = db.Column(db.Integer)

    home = db.relationship("Team", foreign_keys=[home_team_id])
    away = db.relationship("Team", foreign_keys=[away_team_id])

    @property
    def finished(self):
        return self.status == "FINISHED" and self.home_goals is not None

    @property
    def label(self):
        h = self.home.name_es if self.home else "?"
        a = self.away.name_es if self.away else "?"
        return f"{h} - {a}"


class MatchPrediction(db.Model):
    __tablename__ = "match_predictions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    fixture_id = db.Column(db.Integer, db.ForeignKey("fixtures.id"), nullable=False)
    pred_home = db.Column(db.Integer)
    pred_away = db.Column(db.Integer)
    # equipo que el usuario hace avanzar en una eliminatoria (para el bracket)
    pred_winner_id = db.Column(db.Integer, db.ForeignKey("teams.id"))
    __table_args__ = (db.UniqueConstraint("user_id", "fixture_id"),)


class GroupRankPrediction(db.Model):
    """Orden previsto de un grupo (posición 1..4). pos1 = campeón de grupo."""
    __tablename__ = "group_rank_predictions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    group_letter = db.Column(db.String(1), nullable=False)
    position = db.Column(db.Integer, nullable=False)   # 1..4
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "group_letter", "position"),)


class AdvancePrediction(db.Model):
    """Bonus: equipos que el usuario cree que llegan a cada ronda.
    round_key: R32 (8 mejores terceros, fase1), R16/QF/SF/FINAL/CHAMPION (fase2)."""
    __tablename__ = "advance_predictions"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    round_key = db.Column(db.String(10), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    __table_args__ = (db.UniqueConstraint("user_id", "round_key", "team_id"),)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(255))


def get_setting(key, default=None):
    s = db.session.get(Setting, key)
    return s.value if s else default


def set_setting(key, value):
    s = db.session.get(Setting, key)
    if s:
        s.value = str(value)
    else:
        db.session.add(Setting(key=key, value=str(value)))
    db.session.commit()

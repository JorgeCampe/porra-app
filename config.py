# -*- coding: utf-8 -*-
"""Configuración y reglas de puntuación de la Porra Mundial 2026.

Los puntos son EXACTAMENTE los del Excel original (hoja 'Normas'),
solo que organizados en dos fases independientes:

  FASE 1 - Fase de grupos
  FASE 2 - Eliminatorias (knock-outs)
"""
import os

# ---------------------------------------------------------------------------
# General
# ---------------------------------------------------------------------------
SECRET_KEY = os.environ.get("SECRET_KEY", "cambia-esta-clave-secreta-porra-2026")

# Base de datos:
#   - Si DATABASE_URL está definida (p. ej. MySQL en PythonAnywhere), se usa esa.
#     MySQL es mucho más fiable que SQLite en hosting compartido (varios usuarios
#     a la vez), donde SQLite da errores intermitentes de "disk I/O error".
#   - Si no, se cae a una base SQLite local (desarrollo / arranque local).
DB_PATH = None
_explicit = os.environ.get("DATABASE_URL")
if _explicit:
    # Render/Neon a veces entregan el esquema antiguo "postgres://";
    # SQLAlchemy 2.x necesita "postgresql://".
    if _explicit.startswith("postgres://"):
        _explicit = _explicit.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _explicit
elif os.environ.get("PORRA_DB"):
    DB_PATH = os.environ["PORRA_DB"]
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.abspath(DB_PATH).replace("\\", "/")
else:
    import tempfile
    _dbdir = os.path.join(os.path.expanduser("~"), ".porra_mundial_2026")
    try:
        os.makedirs(_dbdir, exist_ok=True)
    except Exception:  # noqa  -> nunca usar la carpeta sincronizada (OneDrive)
        _dbdir = os.path.join(tempfile.gettempdir(), "porra_mundial_2026")
        os.makedirs(_dbdir, exist_ok=True)
    DB_PATH = os.path.join(_dbdir, "porra.db")
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.abspath(DB_PATH).replace("\\", "/")

SQLALCHEMY_TRACK_MODIFICATIONS = False
# En MySQL, PythonAnywhere corta las conexiones inactivas (~5 min); reciclar y
# verificar la conexión antes de usarla evita el error "MySQL server has gone away".
if SQLALCHEMY_DATABASE_URI.startswith("mysql"):
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_recycle": 280, "pool_pre_ping": True}
elif SQLALCHEMY_DATABASE_URI.startswith("postgresql"):
    # Postgres en la nube (Neon) cierra conexiones inactivas; verificar antes de
    # usarlas evita errores de conexión perdida.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_recycle": 300, "pool_pre_ping": True}
elif SQLALCHEMY_DATABASE_URI.startswith("sqlite"):
    # En el disco de red de PythonAnywhere, SQLite puede dar "database is locked"
    # o "disk I/O error" con varios accesos a la vez. Un tiempo de espera amplio
    # (30 s) más los PRAGMA al conectar (ver app.py) lo hacen mucho más tolerante.
    SQLALCHEMY_ENGINE_OPTIONS = {"connect_args": {"timeout": 30}}

# Proveedor de resultados:
#   "espn"        -> GRATIS, sin key, casi en tiempo real (recomendado)
#   "openfootball"-> GRATIS, sin key, pero tarda unas horas
#   "apifootball" -> de pago para 2026
RESULTS_PROVIDER = os.environ.get("RESULTS_PROVIDER", "espn")

# API-Football (api-sports.io) -- Mundial 2026 => league=1, season=2026
# OJO: el plan gratuito NO da acceso a 2026; requiere plan de pago.
API_FOOTBALL_KEY = os.environ.get("API_FOOTBALL_KEY", "")
API_FOOTBALL_BASE = "https://v3.football.api-sports.io"
API_LEAGUE_ID = 1
API_SEASON = 2026

# Zona horaria de las horas mostradas: hora de Perú (UTC-5)
DISPLAY_TZ_LABEL = "hora de Perú (UTC-5)"

# Cierre de la Fase 1: 11 de junio a las 00:00 de Perú (= 05:00 UTC)
PHASE1_LOCK_ISO = "2026-06-11T05:00:00"

# Cierre GLOBAL de la Fase 2 (eliminatorias): fin del 28 de junio (domingo) en
# Perú (= 29 jun 05:00 UTC). Además, cada cruce se bloquea 30 min antes de empezar.
PHASE2_LOCK_ISO = "2026-06-29T05:00:00"

# ---------------------------------------------------------------------------
# PUNTUACIONES POR PARTIDO  (signo 1X2 / goles por lado / resultado exacto)
#   - signo      : aciertas 1-X-2
#   - goal_side  : por cada lado (local y visitante) cuyo nº de goles aciertas
#   - exact      : bonus adicional si aciertas el marcador exacto (ambos lados)
# Un acierto perfecto = signo + goal_side*2 + exact
# ---------------------------------------------------------------------------
MATCH_POINTS = {
    "GROUP": {"signo": 2,  "goal_side": 1, "exact": 2},   # Fase de grupos
    "R32":   {"signo": 4,  "goal_side": 2, "exact": 4},   # Dieciseisavos
    "R16":   {"signo": 4,  "goal_side": 2, "exact": 4},   # Octavos
    "QF":    {"signo": 4,  "goal_side": 2, "exact": 4},   # Cuartos
    "SF":    {"signo": 8,  "goal_side": 4, "exact": 8},   # Semifinales
    "3RD":   {"signo": 16, "goal_side": 8, "exact": 16},  # Tercer puesto
    "FINAL": {"signo": 16, "goal_side": 8, "exact": 16},  # Final
}

# ---------------------------------------------------------------------------
# PUNTUACIONES DE BONUS (aciertos de equipos que clasifican / campeón)
# ---------------------------------------------------------------------------
BONUS_POINTS = {
    "GROUP_WINNER": 6,   # Campeón de cada grupo (x12)            -> FASE 1
    "R32": 4,            # Equipos que pasan a dieciseisavos (x32) -> FASE 1
    "R16": 4,            # Equipos que pasan a octavos (x16)       -> FASE 2
    "QF": 6,             # Equipos que pasan a cuartos (x8)        -> FASE 2
    "SF": 8,             # Equipos que pasan a semis (x4)          -> FASE 2
    "FINAL": 10,         # Equipos que juegan la final (x2)        -> FASE 2
    "CHAMPION": 25,      # Campeón del mundial (x1)                -> FASE 2
}

# Qué etapas de partido pertenecen a cada fase
PHASE1_MATCH_STAGES = ["GROUP"]
PHASE2_MATCH_STAGES = ["R32", "R16", "QF", "SF", "3RD", "FINAL"]

# Etiquetas legibles
STAGE_LABELS = {
    "GROUP": "Fase de grupos",
    "R32": "Dieciseisavos",
    "R16": "Octavos",
    "QF": "Cuartos",
    "SF": "Semifinales",
    "3RD": "Tercer puesto",
    "FINAL": "Final",
}

# Máximos teóricos por concepto (para mostrar en la página de reglas)
MAXIMUMS = {
    "Fase 1 · Signo grupos": 144, "Fase 1 · Goles grupos": 144,
    "Fase 1 · Exacto grupos": 144, "Fase 1 · Campeones de grupo": 72,
    "Fase 1 · Pasan a dieciseisavos": 128,
    "Fase 2 · Pasan a octavos": 64, "Fase 2 · Pasan a cuartos": 48,
    "Fase 2 · Pasan a semis": 32, "Fase 2 · Finalistas": 20,
    "Fase 2 · Campeón": 25,
}

# Banderas: nombre en español -> código ISO de flagcdn.com
FLAGS = {
    "Alemania": "de", "Arabia Saudita": "sa", "Argelia": "dz", "Argentina": "ar",
    "Australia": "au", "Austria": "at", "Bélgica": "be", "Bosnia y Herzegovina": "ba",
    "Brasil": "br", "Cabo Verde": "cv", "Canadá": "ca", "Catar": "qa",
    "Colombia": "co", "Corea del Sur": "kr", "Costa de Marfil": "ci", "Croacia": "hr",
    "Curazao": "cw", "Ecuador": "ec", "Egipto": "eg", "Escocia": "gb-sct",
    "España": "es", "Estados Unidos": "us", "Francia": "fr", "Ghana": "gh",
    "Haití": "ht", "Holanda": "nl", "Inglaterra": "gb-eng", "Irak": "iq",
    "Irán": "ir", "Japón": "jp", "Jordania": "jo", "Marruecos": "ma",
    "México": "mx", "Noruega": "no", "Nueva Zelanda": "nz", "Panamá": "pa",
    "Paraguay": "py", "Portugal": "pt", "RD Congo": "cd", "República Checa": "cz",
    "Senegal": "sn", "Sudáfrica": "za", "Suecia": "se", "Suiza": "ch",
    "Túnez": "tn", "Turquía": "tr", "Uruguay": "uy", "Uzbekistán": "uz",
}

def flag_url(name_es, w="w40"):
    """URL de la bandera de un equipo (o '' si no se conoce)."""
    code = FLAGS.get(name_es)
    return f"https://flagcdn.com/{w}/{code}.png" if code else ""


# ---------------------------------------------------------------------------
# Cuadro de eliminatorias (números de partido por ronda y enlaces del bracket)
# ---------------------------------------------------------------------------
KO_ROUND_NUMS = {
    "R32": list(range(73, 89)),   # 73..88
    "R16": list(range(89, 97)),   # 89..96
    "QF": list(range(97, 101)),   # 97..100
    "SF": [101, 102],
    "3RD": [103],
    "FINAL": [104],
}
KO_NUM_STAGE = {n: st for st, nums in KO_ROUND_NUMS.items() for n in nums}

# Cada cruce (R16 en adelante) se alimenta de los GANADORES ("W") o
# PERDEDORES ("L") de dos partidos anteriores. (Cuadro oficial 2026.)
BRACKET_FEEDS = {
    89: ("W", 74, 77), 90: ("W", 73, 75), 91: ("W", 76, 78), 92: ("W", 79, 80),
    93: ("W", 83, 84), 94: ("W", 81, 82), 95: ("W", 86, 88), 96: ("W", 85, 87),
    97: ("W", 89, 90), 98: ("W", 93, 94), 99: ("W", 91, 92), 100: ("W", 95, 96),
    101: ("W", 97, 98), 102: ("W", 99, 100),
    103: ("L", 101, 102),   # tercer puesto: perdedores de semis
    104: ("W", 101, 102),   # final: ganadores de semis
}

"""
Base de datos SQLite compartida por el indexador y el bot (datos/dossieres.db).
"""
import os
import re
import sqlite3
import unicodedata
from pathlib import Path

DATA = Path(os.getenv("DATA_DIR", "datos"))
RUTA_DB = DATA / "dossieres.db"

ESQUEMA = """
CREATE TABLE IF NOT EXISTS archivos (
    id TEXT PRIMARY KEY, nombre TEXT, ruta TEXT, raiz TEXT, ext TEXT, tamano INTEGER,
    sha1 TEXT, modificado TEXT, grupo TEXT, idioma TEXT, preferido INTEGER DEFAULT 0,
    activo INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS archivos_grupo ON archivos(grupo);

CREATE TABLE IF NOT EXISTS excel (
    file_id TEXT PRIMARY KEY, cliente TEXT, campana TEXT, anio TEXT, idioma TEXT,
    vehiculo TEXT, vehiculo2 TEXT, tipo TEXT, tematicas TEXT, comercial TEXT,
    contenidos TEXT, nota TEXT
);

CREATE TABLE IF NOT EXISTS textos (
    file_id TEXT PRIMARY KEY, sha1 TEXT, origen TEXT, caracteres INTEGER, texto TEXT
);

CREATE TABLE IF NOT EXISTS fichas (
    grupo TEXT PRIMARY KEY, firma TEXT, file_id_base TEXT, modelo TEXT,
    titulo TEXT, cliente TEXT, campana TEXT, anio TEXT, idiomas TEXT, vehiculos TEXT,
    formato TEXT, tematicas TEXT, publico TEXT, resumen TEXT, palabras_clave TEXT,
    coste_usd REAL, version TEXT, actualizado TEXT
);

CREATE TABLE IF NOT EXISTS lotes (
    id TEXT PRIMARY KEY, creado TEXT, grupos INTEGER, recogido INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usuarios (
    telegram_id INTEGER PRIMARY KEY, nombre TEXT, admin INTEGER DEFAULT 0,
    alta TEXT, ultimo_saludo TEXT
);

CREATE TABLE IF NOT EXISTS registro (
    ts TEXT DEFAULT (datetime('now')), telegram_id INTEGER, accion TEXT, detalle TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS buscador USING fts5(
    grupo UNINDEXED, titulo, cliente, campana, tematicas, vehiculos, formato,
    resumen, palabras_clave, nombres, texto,
    tokenize = "unicode61 remove_diacritics 2"
);
"""


def conectar():
    DATA.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(RUTA_DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA)
    return con


def sin_tildes(texto):
    return "".join(c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn")


VACIAS = set("""a al algo algun alguna algunos ante con como contra cual de del desde donde dossier dossieres dosier
dosieres el ella en entre era es esa ese esta este esto hay la las le lo los mas me mi muy nos o para pero por que
quiero se sea si sin sobre su sus tenemos tiene todo tu un una uno unos y ya busco buscame dame hazme algun ejemplo
ejemplos campana campanas hicimos hecho tengo tenga""".split())


def consulta_fts(texto):
    """Convierte una frase libre en una consulta FTS5 tolerante: palabras sueltas con prefijo, unidas con OR."""
    palabras = re.findall(r"[0-9a-zñ]+(?:,[0-9]+)?", sin_tildes(texto).lower())
    utiles = [p for p in palabras if p not in VACIAS and (len(p) >= 3 or p.isdigit())]
    return " OR ".join(f'"{p}"*' for p in dict.fromkeys(utiles))


def buscar(con, texto, limite=8):
    q = consulta_fts(texto)
    if not q:
        return []
    filas = con.execute(
        """SELECT f.*, bm25(buscador, 0, 6, 5, 5, 3, 2, 3, 2, 3, 2, 1) AS puntos
           FROM buscador JOIN fichas f USING (grupo)
           WHERE buscador MATCH ? ORDER BY puntos LIMIT ?""", (q, limite)).fetchall()
    return [dict(f) for f in filas]


def archivos_de(con, grupo):
    return [dict(a) for a in con.execute(
        "SELECT * FROM archivos WHERE grupo = ? AND activo = 1 ORDER BY preferido DESC, tamano", (grupo,))]

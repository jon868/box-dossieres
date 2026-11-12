"""
Catálogos, magazines y presentaciones de alegria-activity (Box: 00.COMPARTIDO / 1. Material Comercial).

A diferencia de los dossieres, no se indexan solos: son pocos y se mantienen a mano en catalogos.json
(uno por catálogo, con sus ediciones y, en cada una, un archivo por idioma y formato).

Uso desde la consola:
  python catalogos.py listar       Resumen de lo que hay en el JSON
  python catalogos.py comprobar    Mira en Box que cada archivo existe y no ha cambiado de nombre
"""
import json
import os
import sys
from pathlib import Path

import db

RUTA = Path(os.getenv("CATALOGOS_JSON", Path(__file__).with_name("catalogos.json")))
FORMATO_POR_DEFECTO = "baja"
PREFIJO = "cat-"            # "grupo" de un archivo de catálogo en el bot: cat-<id de Box>

_cache = {"mtime": None, "datos": None}


def datos():
    """Lee catalogos.json (y lo relee si cambia, sin reiniciar el bot)."""
    try:
        mtime = RUTA.stat().st_mtime
    except FileNotFoundError:
        return {"catalogos": [], "formatos": {}}
    if _cache["mtime"] != mtime:
        _cache.update(mtime=mtime, datos=json.loads(RUTA.read_text(encoding="utf-8")))
    return _cache["datos"]


def _norm(t):
    return db.sin_tildes(t or "").lower()


def _orden_edicion(e):
    try:
        return int(str(e["edicion"])[:4])
    except ValueError:
        return 0


def ediciones(cat):
    """Ediciones de un catálogo, de la más nueva a la más antigua."""
    return sorted(cat["ediciones"], key=_orden_edicion, reverse=True)


def archivo(file_id):
    """(catálogo, edición, archivo) de un id de Box, o None si no está en el JSON."""
    for c in datos()["catalogos"]:
        for e in c["ediciones"]:
            for a in e["archivos"]:
                if a["id"] == file_id:
                    return c, e, a
    return None


def ficha(file_id):
    """El archivo con la forma de ficha que usa el bot para listar y poner botón de descarga."""
    r = archivo(file_id)
    if not r:
        return None
    c, e, a = r
    return {"grupo": PREFIJO + a["id"], "titulo": f"{c['titulo']} {e['edicion']}", "cliente": c["tipo"],
            "anio": str(e["edicion"]), "idiomas": a.get("idioma") or "", "formato_archivo": a["formato"],
            "nombre": a["nombre"], "mb": a["mb"]}


def buscar(consulta="", idioma=None):
    """Catálogos que encajan con la consulta (vacía = todos), resumidos para Claude.
    Por cada edición: idiomas y formatos disponibles con el id de cada archivo."""
    q = [p for p in _norm(consulta).split() if len(p) >= 3 and p not in db.VACIAS
         and p not in ("catalogo", "catalogos", "ultimo", "ultima", "version", "pasame", "manda")]
    salida = []
    for c in datos()["catalogos"]:
        texto = _norm(" ".join([c["id"], c["titulo"], c["tipo"], c.get("describe", "")] + c.get("palabras", [])))
        if q and not any(p in texto for p in q):
            continue
        eds = []
        for e in ediciones(c):
            archivos = [a for a in e["archivos"]
                        if not idioma or _norm(idioma)[:4] in _norm(a.get("idioma") or "")]
            if not archivos and idioma:
                continue
            eds.append({"edicion": e["edicion"], "nota": e.get("nota", ""),
                        "archivos": [{"id": a["id"], "idioma": a.get("idioma") or "sin confirmar",
                                      "formato": a["formato"], "mb": a["mb"]} for a in archivos]})
        salida.append({"catalogo": c["id"], "titulo": c["titulo"], "tipo": c["tipo"],
                       "describe": c.get("describe", ""), "ediciones": eds})
    return salida


def resumen_para_prompt():
    """Línea por catálogo para el system prompt: así Claude sabe qué existe sin buscar."""
    lineas = []
    for c in datos()["catalogos"]:
        eds = ediciones(c)
        idiomas = sorted({a.get("idioma") for a in eds[0]["archivos"] if a.get("idioma")}) if eds else []
        lineas.append(f"- {c['titulo']} ({c['tipo']}): última edición {eds[0]['edicion'] if eds else '-'}"
                      f" en {', '.join(idiomas) or '-'}; {len(eds)} ediciones en total.")
    formatos = "; ".join(f"{k}: {v}" for k, v in datos().get("formatos", {}).items())
    return "\n".join(lineas) + (f"\nFormatos: {formatos}." if formatos else "")


# ---------------------------------------------------------------- consola
def cmd_listar():
    for c in datos()["catalogos"]:
        print(f"\n{c['titulo']} [{c['id']}]")
        for e in ediciones(c):
            print(f"  {e['edicion']}: " + ", ".join(f"{a.get('idioma') or '?'}/{a['formato']}" for a in e["archivos"])
                  + (f"  ({e['nota']})" if e.get("nota") else ""))


def cmd_comprobar():
    import box
    errores = 0
    for c in datos()["catalogos"]:
        for e in c["ediciones"]:
            for a in e["archivos"]:
                r = box.get(f"{box.API}/files/{a['id']}", params={"fields": "name,size,item_status"})
                if r.status_code != 200 or r.json().get("item_status") != "active":
                    print(f"✗ {c['id']} {e['edicion']}: {a['nombre']} ({a['id']}) ya no está en Box")
                    errores += 1
                elif r.json()["name"] != a["nombre"]:
                    print(f"~ {c['id']} {e['edicion']}: {a['id']} ahora se llama {r.json()['name']!r}")
    print("Todo en orden." if not errores else f"{errores} archivos que revisar.")
    return errores


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    orden = sys.argv[1] if len(sys.argv) > 1 else "listar"
    if orden == "comprobar":
        sys.exit(1 if cmd_comprobar() else 0)
    cmd_listar()

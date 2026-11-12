"""
Catálogos, magazines y presentaciones de alegria-activity (Box: 00.COMPARTIDO / 1. Material Comercial).

El catálogo vive SOLO en datos/catalogos.json (en el volumen, como el índice de dossieres): el repo es público y
no debe llevar nombres de archivos ni ids de Box. Para cargarlo o cambiarlo a mano, un administrador manda
catalogos.json al bot por Telegram (/catalogos exportar devuelve el actual). Si existe un catalogos.json junto al
código (solo en local, está en .gitignore) se usa como semilla la primera vez.

Cada noche (o con /catalogos desde Telegram) actualizar():
  1. recorre Material Comercial (menos las carpetas que no son material comercial: vídeos, fotos, web...);
  2. descarta por regla lo que no puede ser un catálogo (planos, certificados, PDF pequeños, duplicados exactos);
  3. a cada PDF nuevo que quede, Claude le lee el nombre, la ruta y el principio del texto y decide si es una
     edición nueva de un catálogo, un catálogo nuevo o algo que no es catálogo, con su edición, idioma y formato;
  4. lo aplica solo y avisa a los administradores con un botón para deshacerlo;
  5. quita lo que ya no está en Box.

Consola:
  python catalogos.py listar          Resumen del catálogo
  python catalogos.py actualizar      Busca y clasifica PDF nuevos (lo mismo que hace el bot cada noche)
  python catalogos.py comprobar       Mira que todos los archivos siguen en Box
  python catalogos.py deshacer <id>   Saca un archivo del catálogo y lo deja como descartado
  python catalogos.py importar <ruta> Sustituye el catálogo por ese archivo (guarda copia del anterior)
"""
import json
import os
import re
import shutil
import sys
import threading
import time
from pathlib import Path

import db

SEMILLA = Path(__file__).with_name("catalogos.json")
RUTA = Path(os.getenv("CATALOGOS_JSON", db.DATA / "catalogos.json"))
PREFIJO = "cat-"            # "grupo" de un archivo de catálogo en el bot: cat-<id de Box>
MODELO = os.getenv("CATALOGOS_MODELO", os.getenv("MODELO", "claude-haiku-5-5"))

CARPETA_RAIZ = os.getenv("CATALOGOS_CARPETA", "6895122650")       # 1. Material Comercial
# No son material comercial (o ya los cubre el índice de dossieres): dossieres genéricos, portfolio suelto (png),
# vídeos, descripción de campañas, fotos, asistencia, imagen corporativa, otros, podcast, fichas de campañas, web
EXCLUIR = set(os.getenv("CATALOGOS_EXCLUIR", "6895115242,6895113794,194797765098,7799541645,194797980456,"
                        "6895111862,6895113554,194796956133,248159276691,322595262844,356574175182").split(","))
MIN_MB = float(os.getenv("CATALOGOS_MIN_MB", "1"))
RUIDO = re.compile(os.getenv("CATALOGOS_RUIDO", r"planos?\b|editables|pesos|certificad|boletin|esquema|presupuesto"
                                                r"|ppto|cotas|declaraci"), re.I)

_cache = {"mtime": None, "datos": None}
_cerrojo = threading.RLock()


# ---------------------------------------------------------------- lectura y escritura
def _vacio():
    return {"version": 0, "formatos": {}, "catalogos": [], "descartados": []}


def _ids(d):
    return {a["id"] for c in d["catalogos"] for e in c["ediciones"] for a in e["archivos"]}


def _fusionar_semilla(d, s):
    """Suma al catálogo vivo lo nuevo del repo (catálogos, ediciones, archivos, descartados), sin borrar nada."""
    conocidos = _ids(d) | {x["id"] for x in d.get("descartados", [])}
    por_id = {c["id"]: c for c in d["catalogos"]}
    for cs in s["catalogos"]:
        c = por_id.get(cs["id"])
        if not c:
            d["catalogos"].append(cs)
            continue
        for k in ("titulo", "tipo", "describe", "palabras", "solo_si_lo_piden"):   # textos: manda el repo
            if k in cs:
                c[k] = cs[k]
        for es in cs["ediciones"]:
            e = next((x for x in c["ediciones"] if str(x["edicion"]) == str(es["edicion"])), None)
            if not e:
                e = {**es, "archivos": []}
                c["ediciones"].append(e)
            e["archivos"] += [a for a in es["archivos"] if a["id"] not in conocidos]
    d["descartados"] = d.get("descartados", []) + [x for x in s.get("descartados", []) if x["id"] not in conocidos]
    d["formatos"] = s.get("formatos", d.get("formatos", {}))
    d["version"] = s.get("version", 0)


def datos():
    """El catálogo vivo; lo crea o lo pone al día con la semilla del repo si hace falta, y lo relee si cambia."""
    with _cerrojo:
        semilla = json.loads(SEMILLA.read_text(encoding="utf-8")) if SEMILLA.exists() else _vacio()
        if not RUTA.exists():
            RUTA.parent.mkdir(parents=True, exist_ok=True)
            if SEMILLA.exists():
                shutil.copy(SEMILLA, RUTA)
            else:
                guardar(_vacio())
        mtime = RUTA.stat().st_mtime
        if _cache["mtime"] != mtime:
            _cache.update(mtime=mtime, datos=json.loads(RUTA.read_text(encoding="utf-8")))
            _cache["datos"].setdefault("descartados", [])
        if semilla.get("version", 0) > _cache["datos"].get("version", 0):
            _fusionar_semilla(_cache["datos"], semilla)
            guardar(_cache["datos"])
        return _cache["datos"]


def importar(contenido):
    """Valida un catalogos.json (bytes) y lo pone en lugar del actual, guardando copia del anterior."""
    d = json.loads(contenido.decode("utf-8") if isinstance(contenido, bytes) else contenido)
    if not isinstance(d.get("catalogos"), list):
        raise ValueError("no tiene la lista 'catalogos'")
    for c in d["catalogos"]:
        for k in ("id", "titulo", "ediciones"):
            if k not in c:
                raise ValueError(f"a un catálogo le falta '{k}'")
        for e in c["ediciones"]:
            for a in e.get("archivos", []):
                if not str(a.get("id", "")).isdigit() or "nombre" not in a:
                    raise ValueError(f"archivo sin id o nombre en {c['id']} {e.get('edicion')}")
    d.setdefault("descartados", [])
    with _cerrojo:
        if RUTA.exists():
            shutil.copy(RUTA, RUTA.with_name(f"catalogos.{time.strftime('%Y%m%d-%H%M%S')}.json"))
        guardar(d)
    n = len(_ids(d))
    return f"✅ Catálogo importado: {len(d['catalogos'])} catálogos y {n} archivos (copia del anterior en datos/)."


def guardar(d):
    with _cerrojo:
        d["actualizado"] = time.strftime("%Y-%m-%d %H:%M")
        tmp = RUTA.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(RUTA)
        _cache.update(mtime=RUTA.stat().st_mtime, datos=d)


# ---------------------------------------------------------------- consultas (bot)
def _norm(t):
    return db.sin_tildes(t or "").lower()


def _orden_edicion(e):
    m = re.search(r"(19|20)\d{2}", str(e["edicion"]))
    return int(m.group(0)) if m else 0


def ediciones(cat):
    """Ediciones de un catálogo, de la más nueva a la más antigua (sin las que se han quedado vacías)."""
    return sorted([e for e in cat["ediciones"] if e["archivos"]], key=_orden_edicion, reverse=True)


def archivo(file_id):
    """(catálogo, edición, archivo) de un id de Box, o None si no está."""
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
    apodo = f" · {e['nombre']}" if e.get("nombre") else ""
    return {"grupo": PREFIJO + a["id"], "titulo": f"{c['titulo']} {e['edicion']}{apodo}", "cliente": c["tipo"],
            "anio": str(e["edicion"]), "idiomas": a.get("idioma") or "", "formato_archivo": a["formato"],
            "nombre": a["nombre"], "mb": a["mb"]}


def buscar(consulta="", idioma=None):
    """Catálogos que encajan con la consulta (vacía = todos), resumidos para Claude: por cada edición, de la más
    nueva a la más antigua, los archivos por idioma y formato con su id."""
    q = [p for p in _norm(consulta).split() if len(p) >= 3 and p not in db.VACIAS
         and p not in ("catalogo", "catalogos", "ultimo", "ultima", "version", "pasame", "manda")]
    salida = []
    for c in datos()["catalogos"]:
        apodos = [e.get("nombre", "") for e in c["ediciones"]]
        texto = _norm(" ".join([c["id"], c["titulo"], c["tipo"], c.get("describe", "")] + c.get("palabras", [])
                               + apodos))
        if q and not any(p in texto for p in q):
            continue
        if c.get("solo_si_lo_piden") and not any(p in _norm(" ".join(c.get("palabras", []))) for p in q):
            continue   # p. ej. el portfolio: solo si lo nombran
        eds = []
        for e in ediciones(c):
            archivos = [a for a in e["archivos"]
                        if not idioma or _norm(idioma)[:4] in _norm(a.get("idioma") or "")]
            if archivos:
                eds.append({"edicion": e["edicion"], "nombre": e.get("nombre", ""), "nota": e.get("nota", ""),
                            "archivos": [{"id": a["id"], "idioma": a.get("idioma") or "sin confirmar",
                                          "formato": a["formato"], "mb": a["mb"]} for a in archivos]})
        if eds:
            salida.append({"catalogo": c["id"], "titulo": c["titulo"], "tipo": c["tipo"],
                           "describe": c.get("describe", ""), "ediciones": eds,
                           "elegir_edicion": bool(c.get("elegir_edicion"))})
    return salida


def resumen_para_prompt():
    """Línea por catálogo para el system prompt: así Claude sabe qué existe sin buscar."""
    lineas = []
    for c in datos()["catalogos"]:
        eds = ediciones(c)
        if not eds:
            continue
        if c.get("solo_si_lo_piden"):
            lineas.append(f"- {c['titulo']} ({c['tipo']}, {eds[0]['edicion']}): antiguo; "
                          "ofrécelo SOLO si lo piden por su nombre.")
            continue
        if c.get("elegir_edicion"):
            nombres = ", ".join(f"{e['edicion']}" + (f" ({e['nombre']})" if e.get("nombre") else "") for e in eds)
            lineas.append(f"- {c['titulo']} ({c['tipo']}): NO des solo la última; enseña las ediciones por su "
                          f"nombre y que elija: {nombres}.")
            continue
        idiomas = sorted({a.get("idioma") for a in eds[0]["archivos"] if a.get("idioma")})
        lineas.append(f"- {c['titulo']} ({c['tipo']}): última edición {eds[0]['edicion']}"
                      f" en {', '.join(idiomas) or '-'}; {len(eds)} ediciones en total.")
    formatos = "; ".join(f"{k}: {v}" for k, v in datos().get("formatos", {}).items())
    return "\n".join(lineas) + (f"\nFormatos: {formatos}." if formatos else "")


# ---------------------------------------------------------------- actualización automática
INSTRUCCIONES = """Clasificas archivos PDF de la carpeta "1. Material Comercial" del Box de alegria-activity, una \
agencia de marketing experiencial con unidades móviles. Ahí hay catálogos genéricos de la agencia (por sector, el \
magazine, la presentación corporativa, Set TV...), cada uno con ediciones por año y, en cada edición, archivos por \
idioma y formato. También hay cosas que NO son catálogos: planos, fichas internas, certificados, material de un \
proyecto concreto, documentos de un cliente concreto (eso son dossieres de propuesta, no catálogos).

Te paso los catálogos que ya existen y un archivo nuevo. Decide con la herramienta clasificar:
- "anadir": es otra edición, idioma o formato de un catálogo que ya existe (usa su id).
- "nuevo_catalogo": es un catálogo genérico de un tema que aún no existe (inventa un id corto en minúsculas).
- "descartar": no es un catálogo.

Pistas de los nombres: ES/CAST = castellano, EN/ENG/ING = inglés, FR = francés, POL = polaco, PT = portugués.
_baja o sin sufijo = "baja"; _PAN = "pantalla"; _PLIEGOS = "pliegos"; _PLIEGOS_baja = "pliegos baja"; _alta/ALTA = \
"alta". La edición es el año del nombre, de la carpeta (p. ej. "Construcción 2024") o del texto; si no hay ninguno, \
el año de modificación. Ojo: a veces el año del archivo y el de la carpeta no coinciden; manda el del nombre del \
archivo. El idioma, si el nombre no lo dice, sácalo del texto. Si de verdad no sabes algo, déjalo vacío."""

HERRAMIENTA = {
    "name": "clasificar",
    "description": "Decisión sobre el archivo nuevo.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {"type": "string", "enum": ["anadir", "nuevo_catalogo", "descartar"]},
            "catalogo": {"type": "string", "description": "anadir: id del catálogo existente"},
            "nuevo": {"type": "object", "description": "nuevo_catalogo: el catálogo nuevo", "properties": {
                "id": {"type": "string"}, "titulo": {"type": "string"}, "tipo": {"type": "string"},
                "describe": {"type": "string", "description": "Una frase: qué contiene"},
                "palabras": {"type": "array", "items": {"type": "string"}}}},
            "edicion": {"type": "string", "description": "Año de la edición, p. ej. '2027'"},
            "idioma": {"type": "string", "description": "castellano, inglés, francés, polaco, portugués..."},
            "formato": {"type": "string", "enum": ["baja", "pantalla", "pliegos", "pliegos baja", "alta"]},
            "nombre": {"type": "string", "description": "Nombre o apodo de la edición si la carpeta o el "
                                                        "archivo lo tiene, p. ej. 'Lucas' en 'Magazine2020 (Lucas)'"},
            "nota": {"type": "string", "description": "Opcional: algo útil sobre la edición"},
            "motivo": {"type": "string", "description": "Por qué lo decides, en una frase"},
        },
        "required": ["decision", "motivo"],
    },
}


def _contexto(d):
    lineas = []
    for c in d["catalogos"]:
        eds = ediciones(c)
        ejemplo = eds[0]["archivos"][0]["nombre"] if eds else ""
        lineas.append(f"- id {c['id']}: {c['titulo']} ({c['tipo']}). {c.get('describe', '')} Ediciones: "
                      + ", ".join(str(e["edicion"]) + (f" ({e['nombre']})" if e.get("nombre") else "") for e in eds) + f". Ejemplo de archivo: {ejemplo}")
    return "\n".join(lineas)


def _texto(file_id, mb):
    import box
    t = box.texto_box(file_id, espera_max=45)
    if not t and mb <= 40:
        try:
            t = box.texto_local(box.descargar(file_id), "pdf")
        except Exception:   # noqa: BLE001  sin texto también se puede clasificar por nombre y ruta
            t = ""
    return re.sub(r"\s+", " ", t or "")[:3000]


def clasificar(cliente, d, a):
    mb = round(a["tamano"] / 1048576, 1)
    datos_archivo = {"nombre": a["nombre"], "ruta": a["ruta"], "mb": mb, "modificado": (a["modificado"] or "")[:10],
                     "principio_del_texto": _texto(a["id"], mb)}
    r = cliente.messages.create(
        model=MODELO, max_tokens=600, system=INSTRUCCIONES, tools=[HERRAMIENTA],
        tool_choice={"type": "tool", "name": "clasificar"},
        messages=[{"role": "user", "content": "Catálogos que ya existen:\n" + _contexto(d)
                   + "\n\nArchivo nuevo:\n" + json.dumps(datos_archivo, ensure_ascii=False)}])
    uso = next((b.input for b in r.content if b.type == "tool_use"), None)
    return uso or {"decision": "descartar", "motivo": "Claude no ha respondido"}


def _slug(t):
    return re.sub(r"[^a-z0-9]+", "-", _norm(t)).strip("-")[:30] or "catalogo"


def aplicar(d, a, dec):
    """Aplica la decisión al catálogo vivo. Devuelve el texto del aviso."""
    nombre = a["nombre"]
    if dec.get("decision") == "descartar" or (dec.get("decision") == "anadir" and not dec.get("catalogo")):
        d["descartados"].append({"id": a["id"], "nombre": nombre, "motivo": dec.get("motivo", ""), "auto": True})
        return None   # los descartes no se avisan uno a uno
    if dec["decision"] == "nuevo_catalogo":
        n = dec.get("nuevo") or {}
        cid = _slug(n.get("id") or n.get("titulo") or nombre)
        c = next((x for x in d["catalogos"] if x["id"] == cid), None)
        if not c:
            c = {"id": cid, "tipo": n.get("tipo") or "catálogo", "titulo": n.get("titulo") or nombre,
                 "describe": n.get("describe", ""), "palabras": n.get("palabras", []), "auto": True,
                 "carpeta": a.get("carpeta"), "ediciones": []}
            d["catalogos"].append(c)
        nuevo = True
    else:
        c = next((x for x in d["catalogos"] if x["id"] == dec["catalogo"]), None)
        if not c:   # id inventado: mejor descartarlo que crear algo raro
            d["descartados"].append({"id": a["id"], "nombre": nombre, "auto": True,
                                     "motivo": f"Catálogo {dec['catalogo']!r} desconocido"})
            return None
        nuevo = False
    edicion = (dec.get("edicion") or (a["modificado"] or "")[:4] or "s/f").strip()
    e = next((x for x in c["ediciones"] if str(x["edicion"]) == edicion), None)
    if not e:
        e = {"edicion": edicion, "archivos": []}
        for k in ("nombre", "nota"):
            if dec.get(k):
                e[k] = dec[k]
        c["ediciones"].append(e)
    e["archivos"].append({"id": a["id"], "nombre": nombre, "idioma": dec.get("idioma") or None,
                          "formato": dec.get("formato") or "baja", "mb": round(a["tamano"] / 1048576, 1),
                          "sha1": a.get("sha1"), "auto": True})
    ultima = ediciones(c)[0]["edicion"] == edicion
    return (("🆕 Catálogo nuevo: " if nuevo else "➕ ") + f"{c['titulo']} · edición {edicion}"
            + f" · {dec.get('idioma') or 'idioma ?'} · {dec.get('formato') or 'baja'}"
            + (" (pasa a ser la última)" if ultima and not nuevo else "") + f"\n{nombre}\n{a['ruta']}")


def deshacer(file_id, motivo="Deshecho por un administrador"):
    """Saca un archivo del catálogo (y el catálogo, si se queda vacío y lo creó el automático) y lo descarta."""
    with _cerrojo:
        d = datos()
        r = None
        for c in list(d["catalogos"]):
            for e in list(c["ediciones"]):
                for a in list(e["archivos"]):
                    if a["id"] == file_id:
                        r = (c, a)
                        e["archivos"].remove(a)
                if not e["archivos"]:
                    c["ediciones"].remove(e)
            if not c["ediciones"] and c.get("auto"):
                d["catalogos"].remove(c)
        if not r:
            return None
        d["descartados"].append({"id": file_id, "nombre": r[1]["nombre"], "motivo": motivo})
        guardar(d)
        return f"{r[1]['nombre']} ya no está en {r[0]['titulo']}."


def actualizar(cliente=None):
    """Busca PDF nuevos en Material Comercial, los clasifica y los aplica. Devuelve
    {"anadidos": [(file_id, texto)], "quitados": [texto], "descartados": n, "errores": [texto]}."""
    import box
    informe = {"anadidos": [], "quitados": [], "descartados": 0, "errores": []}
    lista = []
    box.listar(CARPETA_RAIZ, "", lista, {"pdf"}, excluir_ids=EXCLUIR)
    en_box = {a["id"] for a in lista}
    with _cerrojo:
        d = datos()
        # 1. lo que ya no está en Box (con comprobación: la carpeta pudo excluirse o moverse)
        for c in d["catalogos"]:
            for e in c["ediciones"]:
                for a in list(e["archivos"]):
                    if a["id"] in en_box:
                        continue
                    r = box.get(f"{box.API}/files/{a['id']}", params={"fields": "item_status"})
                    if r.status_code == 404 or (r.status_code == 200 and r.json().get("item_status") != "active"):
                        e["archivos"].remove(a)
                        informe["quitados"].append(f"{c['titulo']} {e['edicion']}: {a['nombre']}")
        # 2. lo nuevo
        conocidos = _ids(d) | {x["id"] for x in d["descartados"]}
        sha1s = {a.get("sha1"): a["nombre"] for c in d["catalogos"] for e in c["ediciones"]
                 for a in e["archivos"] if a.get("sha1")}
        nuevos = [a for a in lista if a["id"] not in conocidos]
        # los conocidos sin sha1 (los de la semilla) lo apuntan ahora, para detectar copias exactas
        por_id = {a["id"]: a for a in lista}
        for c in d["catalogos"]:
            for e in c["ediciones"]:
                for a in e["archivos"]:
                    if not a.get("sha1") and a["id"] in por_id:
                        a["sha1"] = por_id[a["id"]].get("sha1")
                        sha1s[a["sha1"]] = a["nombre"]
        guardar(d)
    for a in nuevos:
        if a.get("sha1") and a["sha1"] in sha1s:
            dec = {"decision": "descartar", "motivo": f"Copia exacta de {sha1s[a['sha1']]}"}
        elif a["tamano"] < MIN_MB * 1048576 or RUIDO.search(f"{a['ruta']}/{a['nombre']}"):
            dec = {"decision": "descartar", "motivo": "Plano, documento técnico o PDF pequeño"}
        else:
            try:
                if cliente is None:
                    import anthropic
                    cliente = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)
                dec = clasificar(cliente, datos(), a)
            except Exception as ex:  # noqa: BLE001  se reintenta la próxima vez
                informe["errores"].append(f"{a['nombre']}: {ex}")
                continue
        with _cerrojo:
            d = datos()
            texto = aplicar(d, a, dec)
            guardar(d)
        if texto:
            informe["anadidos"].append((a["id"], texto))
            if a.get("sha1"):
                sha1s[a["sha1"]] = a["nombre"]
        else:
            informe["descartados"] += 1
    return informe


# ---------------------------------------------------------------- consola
def cmd_listar():
    for c in datos()["catalogos"]:
        print(f"\n{c['titulo']} [{c['id']}]" + (" (automático)" if c.get("auto") else ""))
        for e in ediciones(c):
            print(f"  {e['edicion']}: " + ", ".join(f"{a.get('idioma') or '?'}/{a['formato']}" for a in e["archivos"])
                  + (f"  ({e['nota']})" if e.get("nota") else ""))
    print(f"\n{len(datos()['descartados'])} archivos descartados.")


def cmd_comprobar():
    import box
    errores = 0
    for c in datos()["catalogos"]:
        for e in c["ediciones"]:
            for a in e["archivos"]:
                r = box.get(f"{box.API}/files/{a['id']}", params={"fields": "name,item_status"})
                if r.status_code != 200 or r.json().get("item_status") != "active":
                    print(f"✗ {c['id']} {e['edicion']}: {a['nombre']} ({a['id']}) ya no está en Box")
                    errores += 1
                elif r.json()["name"] != a["nombre"]:
                    print(f"~ {c['id']} {e['edicion']}: {a['id']} ahora se llama {r.json()['name']!r}")
    print("Todo en orden." if not errores else f"{errores} archivos que revisar.")
    return errores


def texto_informe(inf):
    partes = []
    if inf["anadidos"]:
        partes.append(f"{len(inf['anadidos'])} archivos nuevos en los catálogos.")
    if inf["quitados"]:
        partes.append("Ya no están en Box y los he quitado:\n" + "\n".join("• " + q for q in inf["quitados"]))
    if inf["descartados"]:
        partes.append(f"{inf['descartados']} PDF nuevos que no son catálogos (descartados).")
    if inf["errores"]:
        partes.append("No he podido clasificar (lo reintento mañana):\n" + "\n".join("• " + x for x in inf["errores"]))
    return "\n\n".join(partes) or "Nada nuevo en los catálogos."


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    orden = sys.argv[1] if len(sys.argv) > 1 else "listar"
    if orden == "comprobar":
        sys.exit(1 if cmd_comprobar() else 0)
    elif orden == "actualizar":
        inf = actualizar()
        for _, t in inf["anadidos"]:
            print(t, "\n")
        print(texto_informe(inf))
    elif orden == "importar" and len(sys.argv) > 2:
        print(importar(Path(sys.argv[2]).read_bytes()))
    elif orden == "deshacer" and len(sys.argv) > 2:
        print(deshacer(sys.argv[2]) or "Ese archivo no está en el catálogo.")
    else:
        cmd_listar()

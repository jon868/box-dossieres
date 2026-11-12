"""
Asistente del bot: Claude interpreta la pregunta, busca en el índice de dossieres (las veces que haga falta)
y ayuda a la persona a dar con el dossier que necesita.

Claude solo ve las fichas que devuelve el buscador: no puede recomendar dossieres que no existan.
Termina siempre con la herramienta mostrar_dossieres (mensaje + dossieres con botón de descarga).
"""
import hashlib
import json
import os
import re
import threading
import time

import anthropic

import catalogos
import db

MODELO = os.getenv("BOT_MODELO", "claude-haiku-5-5")
MAX_VUELTAS = int(os.getenv("BOT_MAX_BUSQUEDAS", "4"))
MINUTOS_MEMORIA = int(os.getenv("BOT_MINUTOS_MEMORIA", "60"))
TURNOS_MEMORIA = int(os.getenv("BOT_TURNOS_MEMORIA", "6"))

SYSTEM_BASE = """Eres el asistente del buscador interno de dossieres de alegria-activity, una agencia de marketing \
experiencial que hace campañas y exposiciones itinerantes en vehículos (caravanas, furgones, camiones, \
semirremolques, autobuses, plataformas), stands y unidades móviles para marcas y administraciones públicas.

Hablas por Telegram con compañeros de la agencia (comerciales, producción, diseño) que buscan dos tipos de \
documentos:
- Dossieres de propuesta: los que se hicieron para un cliente o campaña concreta (unos 1.100). Se buscan con \
buscar_dossieres.
- Catálogos: material comercial genérico de la agencia, que se pide más que los dossieres. Incluye catálogos \
por sector (Sanidad, Construcción, Manufacturing, Set TV), el magazine y la presentación corporativa. Cada \
uno tiene varias ediciones (años) y, en cada edición, versiones por idioma y formato. Se buscan con \
buscar_catalogos.

Dossier o catálogo:
- Si la persona dice catálogo, magazine, revista o presentación de la agencia, es un catálogo.
- Si nombra un cliente, una campaña o "lo que hicimos para...", es un dossier.
- Si no está claro (p. ej. "pásame lo de sanidad", "algo de construcción para un cliente francés") y hay \
catálogo de ese tema, pregunta en UNA frase si quiere el catálogo o un dossier de propuesta, sin buscar \
dossieres todavía (mostrar_dossieres con la pregunta y sin documentos).

Catálogos:
- Algunos catálogos (como el magazine) tienen ediciones con nombre propio y se piden por ese nombre ("el \
magazine Lucas"). En esos ("elegir_edicion": true) no des solo la última: si no dicen cuál, enséñalas todas, \
de la más nueva a la más antigua, con año y nombre, y pon el botón de cada una (en castellano o el idioma \
pedido). Si nombran una ("Piti", "el de 2020") o la llaman por otro de sus nombres ("tambien_llamado"), da \
esa, pero llámala siempre por su nombre principal.
- En los demás, por defecto da la edición más reciente, en el idioma pedido (castellano si no dice) y en formato "baja", \
que es el que se manda por correo. Di de qué edición es.
- Si la última edición no está en ese idioma, dilo y da la más reciente que sí lo tenga.
- Menciona en una frase que hay ediciones anteriores u otros formatos (pantalla, pliegos para imprenta, alta) \
y que te las pida si las quiere; si las pide, dáselas.
- En mostrar_dossieres pon los catálogos en "catalogos" con el id del archivo elegido (como mucho 5).

Catálogos que hay ahora:
{catalogos}

Cómo trabajas:
1. Si es un dossier, interpreta qué busca la persona: cliente o sector, temática, tipo de acción (exposición itinerante, oficina \
móvil, aula, stand, roadshow...), vehículo o soporte, público, idioma o época. Las peticiones suelen ser vagas \
("algo de salud para colegios", "lo de los bancos en Francia"): tradúcelas a búsquedas concretas.
2. Usa buscar_dossieres. Si los resultados no encajan o son pocos, vuelve a buscar con sinónimos, términos más \
generales o el sector en vez del cliente (p. ej. "banca", "entidad financiera", "educación financiera"). \
Puedes hacer varias búsquedas.
3. Elige los que mejor encajan (como mucho 5, mejor 2 o 3 si son claros) y termina SIEMPRE llamando a \
mostrar_dossieres, con un mensaje breve que explique por qué encaja cada uno, en el mismo orden.

Fechas:
- Cada ficha trae "anio" (si el dossier lo dice) y "fecha" (última modificación del archivo en Box, AAAA-MM-DD). Para saber si un dossier es reciente usa "anio" si está y, si no, "fecha". Ojo: algunas fechas de Box son de cuando se movieron archivos en bloque, así que son aproximadas.
- Si piden "el último", "el más reciente", "la última versión" o algo "actual" de un tema, cliente o sector, busca con orden "recientes" y dilo con la fecha ("es de marzo de 2026"). Si hay varias versiones del mismo dossier, el botón descarga la más reciente en castellano; las demás aparecen en "archivos".
- Para "de los últimos años", "antes de 2020", etc., usa anio_desde / anio_hasta.

Reglas:
- Recomienda solo dossieres que hayan salido en tus búsquedas. Nunca inventes dossieres, clientes ni datos.
- Si nada encaja de verdad, dilo con claridad, enseña lo más cercano si sirve y sugiere cómo afinar.
- Si la petición es tan ambigua que no sabes ni por dónde empezar, haz UNA pregunta corta \
(mostrar_dossieres con la pregunta y sin dossieres). Si puedes buscar algo razonable, busca primero.
- Describe lo que dice cada dossier; no opines sobre si un vehículo sigue existiendo o está disponible.
- Tono cercano y directo, en castellano. Mensajes cortos: se leen en el móvil. Sin saludos (el bot ya saluda), \
sin markdown ni asteriscos; texto plano.
- Si te piden algo que no es buscar dossieres o catálogos, explica brevemente que solo ayudas con eso."""



def system_hoy():
    return (SYSTEM_BASE.replace("{catalogos}", catalogos.resumen_para_prompt())
            + f"\n\nHoy es {time.strftime('%Y-%m-%d')}.")


HERRAMIENTAS = [
    {
        "name": "buscar_dossieres",
        "description": "Busca en el índice de dossieres comerciales de la agencia (unos 1.100, de 2016 a hoy). "
                       "Busca por palabras en título, cliente, campaña, temáticas, vehículos, formato, resumen "
                       "y texto del dossier. Devuelve fichas resumidas.",
        "input_schema": {
            "type": "object",
            "properties": {
                "consulta": {"type": "string", "description": "Palabras clave (p. ej. 'salud mental Airstream', "
                                                              "'bibliobús', 'oficina bancaria móvil'). Vacío "
                                                              "con orden 'recientes' = los últimos de todo el archivo"},
                "orden": {"type": "string", "enum": ["relevancia", "recientes"],
                          "description": "relevancia (por defecto) o recientes: los más nuevos primero"},
                "cliente": {"type": "string", "description": "Opcional: filtra por cliente (texto parcial)"},
                "idioma": {"type": "string", "description": "Opcional: castellano, inglés, francés, polaco..."},
                "anio_desde": {"type": "integer", "description": "Opcional: año mínimo"},
                "anio_hasta": {"type": "integer", "description": "Opcional: año máximo"},
                "limite": {"type": "integer", "description": "Máximo de resultados (por defecto 8, máximo 15)"},
            },
            "required": [],
        },
    },
    {
        "name": "buscar_catalogos",
        "description": "Busca en los catálogos de la agencia (sector, magazine, presentación corporativa). "
                       "Devuelve cada catálogo con sus ediciones de la más nueva a la más antigua y, en cada "
                       "edición, los archivos por idioma y formato con su id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "consulta": {"type": "string", "description": "Tema o nombre (p. ej. 'sanidad', 'magazine', "
                                                              "'presentación'). Vacío = todos"},
                "idioma": {"type": "string", "description": "Opcional: solo archivos en ese idioma"},
            },
            "required": [],
        },
    },
    {
        "name": "mostrar_dossieres",
        "description": "Respuesta final al usuario: un mensaje y, si hay, los dossieres y catálogos elegidos "
                       "(aparecerán con botón de descarga). Llámala siempre para terminar.",
        "input_schema": {
            "type": "object",
            "properties": {
                "mensaje": {"type": "string", "description": "Texto para el usuario, breve y en texto plano"},
                "dossieres": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                              "description": "IDs ('grupo') de los dossieres recomendados, en orden"},
                "catalogos": {"type": "array", "items": {"type": "string"}, "maxItems": 5,
                              "description": "IDs de archivo de los catálogos elegidos (de buscar_catalogos)"},
            },
            "required": ["mensaje", "dossieres"],
        },
    },
]
HERRAMIENTAS[-1]["cache_control"] = {"type": "ephemeral"}

_cliente = None
_memoria = {}          # telegram_id -> {"t": timestamp, "mensajes": [...]}
_cerrojo = threading.Lock()


def cliente():
    global _cliente
    if _cliente is None:
        _cliente = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=3)
    return _cliente


def olvidar(uid):
    with _cerrojo:
        _memoria.pop(uid, None)


def _historial(uid):
    with _cerrojo:
        m = _memoria.get(uid)
        if not m or time.time() - m["t"] > MINUTOS_MEMORIA * 60:
            return []
        return list(m["mensajes"])


def _guardar(uid, pregunta, respuesta):
    previos = _historial(uid)
    nuevos = previos + [{"role": "user", "content": pregunta}, {"role": "assistant", "content": respuesta}]
    with _cerrojo:
        _memoria[uid] = {"t": time.time(), "mensajes": nuevos[-2 * TURNOS_MEMORIA:]}


def _anio(valor):
    m = re.search(r"(19|20)\d{2}", str(valor or ""))
    return int(m.group(0)) if m else None


def fecha_de(con, grupo):
    r = con.execute("SELECT MAX(modificado) FROM archivos WHERE grupo = ? AND activo = 1", (grupo,)).fetchone()[0]
    return (r or "")[:10]


def anio_efectivo(f):
    return _anio(f.get("anio")) or _anio(f.get("fecha"))


def buscar(con, consulta, cliente_=None, idioma=None, anio_desde=None, anio_hasta=None, limite=8,
           orden="relevancia"):
    limite = max(1, min(int(limite or 8), 15))
    recientes = orden == "recientes"
    filtros = bool(cliente_ or idioma or anio_desde or anio_hasta)
    if (consulta or "").strip():
        filas = db.buscar(con, consulta, 80 if (filtros or recientes) else limite)
    else:   # sin palabras: todo el archivo (tiene sentido con orden "recientes" o filtros)
        filas = [dict(f) for f in con.execute("SELECT * FROM fichas")]
    salida = []
    for f in filas:
        f["fecha"] = fecha_de(con, f["grupo"])
        if cliente_ and db.sin_tildes(cliente_).lower() not in db.sin_tildes(f["cliente"] or "").lower():
            continue
        if idioma and db.sin_tildes(idioma).lower()[:4] not in db.sin_tildes(f["idiomas"] or "").lower():
            continue
        a = anio_efectivo(f)
        if (anio_desde and (a is None or a < anio_desde)) or (anio_hasta and (a is None or a > anio_hasta)):
            continue
        salida.append(f)
    if recientes:
        salida.sort(key=lambda f: (anio_efectivo(f) or 0, f["fecha"]), reverse=True)
    return salida[:limite]


def _ficha_breve(con, f):
    archivos = db.archivos_de(con, f["grupo"])
    return {
        "grupo": f["grupo"], "titulo": f["titulo"], "cliente": f["cliente"], "campana": f["campana"],
        "anio": f["anio"], "idiomas": f["idiomas"], "vehiculos": f["vehiculos"], "formato": f["formato"],
        "tematicas": f["tematicas"], "publico": f["publico"], "resumen": f["resumen"],
        "fecha": f.get("fecha") or fecha_de(con, f["grupo"]),
        "archivos": [f"{a['nombre']} ({(a['modificado'] or '')[:10]})" for a in archivos][:4],
    }


def version_prompt():
    """Huella del prompt y las herramientas: cambia sola cuando se edita SYSTEM_BASE o HERRAMIENTAS."""
    base = SYSTEM_BASE + catalogos.resumen_para_prompt() + json.dumps(HERRAMIENTAS, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(base.encode()).hexdigest()[:8]


def responder(con, uid, pregunta):
    """Devuelve (mensaje, [fichas elegidas], traza) o lanza excepción si la API falla.
    La traza (búsquedas, tokens, vueltas, desenlace) se guarda en el registro de uso."""
    historial = _historial(uid)
    mensajes = historial + [{"role": "user", "content": pregunta}]
    vistos = {}
    traza = {"via": "claude", "modelo": MODELO, "version_prompt": version_prompt(),
             "turnos_previos": len(historial) // 2, "busquedas": [], "vueltas": 0,
             "uso": {"entrada": 0, "salida": 0, "cache": 0}}

    def cerrar(texto, fichas, desenlace):
        traza["desenlace"] = desenlace
        return texto, fichas, traza

    for vuelta in range(MAX_VUELTAS + 1):
        r = cliente().messages.create(
            model=MODELO, max_tokens=1200,
            system=[{"type": "text", "text": system_hoy(), "cache_control": {"type": "ephemeral"}}],
            tools=HERRAMIENTAS,
            tool_choice={"type": "auto"},   # "auto": algunos modelos no aceptan forzar la herramienta
            messages=mensajes,
        )
        traza["vueltas"] = vuelta + 1
        u = r.usage
        traza["uso"]["entrada"] += (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        traza["uso"]["cache"] += getattr(u, "cache_read_input_tokens", 0) or 0
        traza["uso"]["salida"] += u.output_tokens or 0
        usos = [b for b in r.content if b.type == "tool_use"]
        final = next((b for b in usos if b.name == "mostrar_dossieres"), None)
        if final:
            ids = [g for g in final.input.get("dossieres", []) if g in vistos][:5]
            for fid in (final.input.get("catalogos") or [])[:5]:
                f = catalogos.ficha(str(fid))   # solo ids que existen en catalogos.json
                if f:
                    vistos[f["grupo"]] = f
                    ids.append(f["grupo"])
            texto = (final.input.get("mensaje") or "").strip()
            _guardar(uid, pregunta, texto + ("\n[Dossieres mostrados: " + "; ".join(
                f"{vistos[g]['titulo']} ({g})" for g in ids) + "]" if ids else ""))
            desenlace = ("recomendacion" if ids else
                         "pregunta" if texto.rstrip().endswith("?") else "sin_resultados")
            return cerrar(texto, [vistos[g] for g in ids], desenlace)
        if not usos:   # respondió con texto sin herramienta
            texto = "".join(getattr(b, "text", "") for b in r.content).strip()
            _guardar(uid, pregunta, texto)
            return cerrar(texto, [], "texto")
        if vuelta == MAX_VUELTAS:   # ya ha buscado bastante: cerramos con lo visto
            break
        mensajes.append({"role": "assistant", "content": r.content})
        resultados = []
        for b in usos:
            if b.name == "buscar_dossieres":
                a = b.input
                filas = buscar(con, a.get("consulta", ""), a.get("cliente"), a.get("idioma"),
                               a.get("anio_desde"), a.get("anio_hasta"), a.get("limite"), a.get("orden"))
                fichas = [_ficha_breve(con, f) for f in filas]
                traza["busquedas"].append({"parametros": a, "n": len(fichas),
                                           "grupos": [f["grupo"] for f in fichas]})
                for f in fichas:
                    vistos[f["grupo"]] = f
                contenido = json.dumps(fichas, ensure_ascii=False) if fichas else "Sin resultados."
            elif b.name == "buscar_catalogos":
                cats = catalogos.buscar(b.input.get("consulta", ""), b.input.get("idioma"))
                traza["busquedas"].append({"parametros": {"catalogos": True, **b.input}, "n": len(cats),
                                           "grupos": [c["catalogo"] for c in cats]})
                contenido = json.dumps(cats, ensure_ascii=False) if cats else "Sin catálogos con eso."
            else:
                contenido = "Herramienta desconocida."
            resultados.append({"type": "tool_result", "tool_use_id": b.id, "content": contenido})
        if vuelta == MAX_VUELTAS - 1:
            resultados.append({"type": "text", "text": "Ya no puedes buscar más: responde con mostrar_dossieres."})
        mensajes.append({"role": "user", "content": resultados})
    cercanos = list(vistos.values())[:3]
    texto = ("No he dado con algo claro. Esto es lo más cercano que he visto:" if cercanos
             else "No he encontrado dossieres con eso. Prueba con el cliente, el tema o el tipo de acción.")
    _guardar(uid, pregunta, texto)
    return cerrar(texto, cercanos, "cercanos" if cercanos else "sin_resultados")

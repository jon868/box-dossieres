#!/usr/bin/env python3
"""
Indexador de dossieres de Box para el bot de Telegram · alegria-activity

Uso:
  python indexar.py listar          Recorre las carpetas de dossieres y guarda los archivos en la base de datos
  python indexar.py excel           Lee el "Listado de Dossieres y Renders (links).xlsx" y lo cruza por ID de Box
  python indexar.py textos          Saca el texto de cada dossier (Box; si falla, del propio PDF/PPTX)
  python indexar.py agrupar         Junta versiones e idiomas del mismo dossier (_baja, _PRINT, _ENG, V2...)
  python indexar.py enviar [--si]   Crea el lote para Haiku con los dossieres sin ficha o que han cambiado
         [--forzar]                 (todas las fichas de nuevo)
  python indexar.py estado          Estado de los lotes
  python indexar.py recoger         Guarda las fichas y rehace el buscador
  python indexar.py todo [--si]     Todo seguido, esperando a que termine el lote
  python indexar.py actualizar      Igual que "todo --si" (para la tarea nocturna)
  python indexar.py buscar "texto"  Prueba el buscador desde la consola
  python indexar.py resumen         Cifras del índice
  python indexar.py exportar        Rehace indice_dossieres.json (para el conector de Box)

Solo LEE de Box. No escribe, mueve ni borra nada allí.
"""
import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import anthropic
from dotenv import load_dotenv

load_dotenv()

import box  # noqa: E402
import db  # noqa: E402

VERSION = "v1"

# ---------------------------------------------------------------- configuración
# Dossieres PDF - Desde 2016 hasta 2023  y  Dossieres PDF - Desde 2024
CARPETAS = [x.strip() for x in os.getenv("CARPETAS", "6906273278,236966097161").split(",") if x.strip()]
# RECOPILATORIOS (dentro de 2016-2023): compilaciones de varios dossieres, fuera del índice
EXCLUIR_IDS = {x.strip() for x in os.getenv("EXCLUIR_IDS", "164858983963").split(",") if x.strip()}
EXCLUIR_PATRON = re.compile(os.getenv("EXCLUIR_PATRON", r"presupuesto|ppto"), re.I)
EXTENSIONES = {x.strip().lower() for x in os.getenv("EXTENSIONES", "pdf,pptx").split(",") if x.strip()}
EXCEL_ID = os.getenv("EXCEL_ID", "302492593457")   # Listado de Dossieres y Renders (links).xlsx
MODELO = os.getenv("MODELO", "claude-haiku-5-5")
MAX_CARACTERES = int(os.getenv("MAX_CARACTERES", "40000"))   # texto que se manda por dossier
MAX_MB_DESCARGA = int(os.getenv("MAX_MB_DESCARGA", "150"))   # para el plan B de extracción de texto
HILOS = int(os.getenv("HILOS", "4"))
MAX_PETICIONES_LOTE = int(os.getenv("MAX_PETICIONES_LOTE", "5000"))
MAX_MB_LOTE = int(os.getenv("MAX_MB_LOTE", "180"))

# Precios Batch en USD por millón de tokens (entrada, salida). Orientativos: revísalos en la Console.
PRECIOS_BATCH = {
    "claude-haiku-5-5": (0.05, 0.25),
    "claude-sonnet-5-5": (1.00, 5.00),
}

SYSTEM = """Eres documentalista de alegria-activity, una agencia de marketing experiencial que hace campañas y \
exposiciones itinerantes en vehículos (caravanas, furgones, camiones, semirremolques, autobuses, plataformas), \
stands y unidades móviles para marcas y administraciones públicas.

Recibes el texto extraído de un dossier comercial de la agencia (a veces desordenado, porque viene de un PDF \
maquetado) y, si existen, los datos que el equipo apuntó en su listado de dossieres. Tu trabajo es rellenar la \
ficha del dossier con la herramienta registrar_dossier para que el equipo lo encuentre después con un buscador.

Reglas:
- Describe solo lo que dice el documento. No inventes datos ni opines: si algo no aparece, deja el campo vacío.
- Los vehículos y soportes, tal como aparecen en el dossier (p. ej. "autobús de 15 metros", "Airstream", \
"semirremolque desplegable"). No valores si siguen existiendo ni si están disponibles.
- El cliente es la marca o institución para la que se hizo el dossier. Si es genérico (sin cliente), pon "Genérico".
- Escribe en castellano aunque el dossier esté en otro idioma, salvo los nombres propios y el lema de la campaña.
- El resumen, de 2 a 4 frases: qué se propone, para quién, en qué soporte y con qué actividades."""

HERRAMIENTA = {
    "name": "registrar_dossier",
    "description": "Guarda la ficha del dossier para el buscador interno.",
    "input_schema": {
        "type": "object",
        "properties": {
            "titulo": {"type": "string", "description": "Nombre de la campaña o del dossier"},
            "cliente": {"type": "string", "description": "Marca o institución; 'Genérico' si no hay"},
            "campana": {"type": "string", "description": "Lema o nombre de la campaña, tal cual"},
            "anio": {"type": "string", "description": "Año si aparece en el dossier o en el listado; si no, vacío"},
            "idioma": {"type": "string", "description": "Idioma del dossier (castellano, inglés, francés...)"},
            "vehiculos": {"type": "array", "items": {"type": "string"},
                          "description": "Vehículos o soportes tal como los nombra el dossier"},
            "formato": {"type": "array", "items": {"type": "string"},
                        "description": "Tipo de acción: exposición itinerante, roadshow, oficina móvil, aula móvil, "
                                       "unidad sanitaria, bibliobús, stand, pop-up store, evento, showcooking..."},
            "tematicas": {"type": "array", "items": {"type": "string"},
                          "description": "Temas: salud, igualdad, medio ambiente, banca, alimentación, deporte..."},
            "publico": {"type": "string", "description": "A quién se dirige (escolares, mayores, profesionales...)"},
            "resumen": {"type": "string", "description": "2 a 4 frases"},
            "palabras_clave": {"type": "array", "items": {"type": "string"},
                               "description": "De 5 a 15 términos con los que alguien buscaría este dossier"},
        },
        "required": ["titulo", "cliente", "campana", "anio", "idioma", "vehiculos", "formato", "tematicas",
                     "publico", "resumen", "palabras_clave"],
    },
}


# ---------------------------------------------------------------- utilidades
def ahora():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def confirmar(args, pregunta):
    if getattr(args, "si", False):
        return
    try:
        respuesta = input(f"\n{pregunta} [s/N] ")
    except (EOFError, OSError):   # sin teclado (nohup, tarea programada)
        sys.exit("\nNo hay consola para confirmar. Lanza el mismo comando con --si para que no pregunte.")
    if respuesta.strip().lower() not in ("s", "si", "sí"):
        sys.exit("Cancelado.")


# ---------------------------------------------------------------- 1. listar
def cmd_listar(_args):
    con = db.conectar()
    vistos = []
    for carpeta in CARPETAS:
        r = box.get(f"{box.API}/folders/{carpeta}", params={"fields": "name"})
        r.raise_for_status()
        nombre = r.json()["name"]
        antes = len(vistos)
        box.listar(carpeta, nombre, vistos, EXTENSIONES, EXCLUIR_IDS, EXCLUIR_PATRON)
        print(f"  {len(vistos) - antes:5d}  {nombre}")
    nuevos = 0
    for a in vistos:
        previo = con.execute("SELECT sha1 FROM archivos WHERE id = ?", (a["id"],)).fetchone()
        nuevos += previo is None or previo["sha1"] != a["sha1"]
        con.execute("""INSERT INTO archivos (id, nombre, ruta, raiz, ext, tamano, sha1, modificado, activo)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                       ON CONFLICT(id) DO UPDATE SET nombre=excluded.nombre, ruta=excluded.ruta,
                         ext=excluded.ext, tamano=excluded.tamano, sha1=excluded.sha1,
                         modificado=excluded.modificado, activo=1""",
                    (a["id"], a["nombre"], a["ruta"], a["ruta"].split("/")[0], a["ext"], a["tamano"],
                     a["sha1"], a["modificado"]))
    ids = {a["id"] for a in vistos}
    retirados = [r["id"] for r in con.execute("SELECT id FROM archivos WHERE activo = 1") if r["id"] not in ids]
    con.executemany("UPDATE archivos SET activo = 0 WHERE id = ?", [(i,) for i in retirados])
    con.commit()
    print(f"{len(vistos)} archivos ({nuevos} nuevos o cambiados, {len(retirados)} ya no están en Box).")


# ---------------------------------------------------------------- 2. excel
def cmd_excel(_args):
    from openpyxl import load_workbook

    con = db.conectar()
    libro = load_workbook(io.BytesIO(box.descargar(EXCEL_ID)), read_only=True, data_only=True)
    hoja = libro["Dossieres"] if "Dossieres" in libro.sheetnames else libro.worksheets[0]
    filas = list(hoja.iter_rows(values_only=True))
    cab_i = next((i for i, f in enumerate(filas) if any(str(c).strip() == "Cliente" for c in f if c)), None)
    if cab_i is None:
        sys.exit("No encuentro la fila de cabecera (con 'Cliente') en el Excel.")
    cab = [str(c).strip() if c else "" for c in filas[cab_i]]

    def col(nombre):
        return cab.index(nombre) if nombre in cab else None

    c = {k: col(v) for k, v in {"cliente": "Cliente", "campana": "Nombre de la campaña", "anio": "Año",
                                 "idioma": "Idioma", "vehiculo": "UM", "vehiculo2": "Tipo / UM 2", "tipo": "Tipo",
                                 "box": "Box", "comercial": "Comercial", "contenidos": "Contenidos",
                                 "nota": "Nota"}.items()}
    tematicas = [i for i, n in enumerate(cab) if n.startswith("Temática")]

    def val(f, k):
        i = c.get(k)
        v = f[i] if i is not None and i < len(f) else None
        return str(v).strip() if v not in (None, "") else ""

    n = sin_id = 0
    for f in filas[cab_i + 1:]:
        if not any(f):
            continue
        file_id = box.id_de_enlace(val(f, "box"))
        if not file_id:
            sin_id += bool(val(f, "cliente"))
            continue
        tem = ", ".join(str(f[i]).strip() for i in tematicas if i < len(f) and f[i])
        con.execute("""INSERT OR REPLACE INTO excel VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (file_id, val(f, "cliente"), val(f, "campana"), val(f, "anio"), val(f, "idioma"),
                     val(f, "vehiculo"), val(f, "vehiculo2"), val(f, "tipo"), tem, val(f, "comercial"),
                     val(f, "contenidos"), val(f, "nota")))
        n += 1
    con.commit()
    cruzados = con.execute("SELECT COUNT(*) FROM excel e JOIN archivos a ON a.id = e.file_id "
                           "WHERE a.activo = 1").fetchone()[0]
    print(f"{n} filas del Excel con enlace a Box ({sin_id} sin enlace). {cruzados} coinciden con archivos indexados.")


# ---------------------------------------------------------------- 3. textos
def extraer(a):
    texto, origen = None, "box"
    try:
        texto = box.texto_box(a["id"])
    except Exception as e:  # noqa: BLE001
        print(f"  Box no da texto de {a['nombre']}: {e}")
    if not (texto and texto.strip()) and a["tamano"] <= MAX_MB_DESCARGA * 1024 * 1024:
        origen = "local"
        try:
            texto = box.texto_local(box.descargar(a["id"]), a["ext"])
        except Exception as e:  # noqa: BLE001
            print(f"  No se pudo leer {a['nombre']}: {e}")
            texto = ""
    texto = re.sub(r"[ \t]+", " ", texto or "")
    texto = re.sub(r"\n\s*\n+", "\n\n", texto).strip()
    return a, origen if texto else "vacio", texto


def cmd_textos(_args):
    con = db.conectar()
    pendientes = [dict(a) for a in con.execute(
        """SELECT a.* FROM archivos a LEFT JOIN textos t ON t.file_id = a.id
           WHERE a.activo = 1 AND (t.file_id IS NULL OR t.sha1 IS NOT a.sha1)""")]
    print(f"{len(pendientes)} archivos sin texto o cambiados.")
    hechos = vacios = 0
    with ThreadPoolExecutor(HILOS) as ex:
        for a, origen, texto in ex.map(extraer, pendientes):
            con.execute("INSERT OR REPLACE INTO textos VALUES (?,?,?,?,?)",
                        (a["id"], a["sha1"], origen, len(texto), texto))
            hechos += 1
            vacios += not texto
            if hechos % 50 == 0:
                con.commit()
                print(f"  {hechos}/{len(pendientes)}")
    con.commit()
    print(f"Hecho: {hechos} archivos ({vacios} sin texto legible; se indexan solo por nombre y Excel).")


# ---------------------------------------------------------------- 4. agrupar
IDIOMAS = {"es": "castellano", "esp": "castellano", "en": "inglés", "eng": "inglés", "ing": "inglés",
           "fr": "francés", "pol": "polaco", "po": "polaco", "pl": "polaco", "it": "italiano",
           "gal": "gallego", "eu": "euskera", "pt": "portugués", "ruso": "ruso"}
RUIDO = re.compile(r"(baja\d*|alta|print|impresion|imprimir|impre|pantalla|enviar|def|definitivo|final|copia|"
                   r"v\d+|\d{3})")
AMBIGUOS = {"es", "en", "it", "eu", "po"}   # también son palabras en castellano/euskera: solo al final del nombre


def clave_y_idioma(nombre):
    """Clave para juntar versiones del mismo dossier y el idioma que indica el nombre (si lo indica)."""
    base = db.sin_tildes(re.sub(r"\.[a-z0-9]{2,4}$", "", nombre, flags=re.I)).lower()
    base = re.sub(r"\(\d+\)", " ", base)
    palabras = [p for p in re.split(r"[\s_\-.,]+", base) if p and not RUIDO.fullmatch(p)]
    idioma = ""
    for i in range(len(palabras) - 1, max(len(palabras) - 4, 0) - 1, -1):
        p = palabras[i]
        if p in IDIOMAS and (p not in AMBIGUOS or i == len(palabras) - 1):
            idioma = IDIOMAS[p]
            del palabras[i]
            break
    if len(palabras) > 1 and palabras[-1].isdigit() and len(palabras[-1]) == 1:
        palabras.pop()   # "_2", "_5": número de versión
    clave = "".join(palabras)   # sin espacios: "Banco Santander" = "BancoSantander"
    return clave or nombre.lower(), idioma


def cmd_agrupar(_args):
    con = db.conectar()
    archivos = [dict(a) for a in con.execute("SELECT * FROM archivos WHERE activo = 1")]
    # unión de archivos con la misma clave de nombre o el mismo contenido (sha1)
    padre = {a["id"]: a["id"] for a in archivos}

    def raiz(x):
        while padre[x] != x:
            padre[x] = padre[padre[x]]
            x = padre[x]
        return x

    def unir(x, y):
        padre[raiz(x)] = raiz(y)

    por_clave, por_sha = {}, {}
    for a in archivos:
        a["clave"], a["idioma"] = clave_y_idioma(a["nombre"])
        for d, k in ((por_clave, a["clave"]), (por_sha, a["sha1"])):
            if k and k in d:
                unir(a["id"], d[k])
            elif k:
                d[k] = a["id"]
    grupos = defaultdict(list)
    for a in archivos:
        grupos[raiz(a["id"])].append(a)

    for miembros in grupos.values():
        clave = min(m["clave"] for m in miembros)
        grupo = "g" + hashlib.sha1(clave.encode()).hexdigest()[:12]
        # descarga por defecto: castellano (o sin idioma marcado), la versión más reciente (por día),
        # y entre las del mismo día la "baja" o la más ligera
        orden = sorted(miembros, key=lambda m: (m["idioma"] not in ("", "castellano"),
                                                 "".join(chr(255 - ord(c)) for c in (m["modificado"] or "")[:10]),
                                                 "baja" not in m["nombre"].lower(), m["tamano"]))
        for m in miembros:
            con.execute("UPDATE archivos SET grupo = ?, idioma = ?, preferido = ? WHERE id = ?",
                        (grupo, m["idioma"], int(m is orden[0]), m["id"]))
    con.commit()
    varios = sum(len(m) > 1 for m in grupos.values())
    print(f"{len(archivos)} archivos en {len(grupos)} dossieres ({varios} con varias versiones o idiomas).")
    if con.execute("SELECT COUNT(*) FROM fichas").fetchone()[0]:
        exportar_indice(con)


# ---------------------------------------------------------------- 5. enviar
def grupos_para_ficha(con, forzar):
    grupos = defaultdict(list)
    for a in con.execute("""SELECT a.*, t.texto, t.caracteres FROM archivos a
                            LEFT JOIN textos t ON t.file_id = a.id
                            WHERE a.activo = 1 AND a.grupo IS NOT NULL"""):
        grupos[a["grupo"]].append(dict(a))
    hechas = {f["grupo"]: f["firma"] for f in con.execute("SELECT grupo, firma FROM fichas")}
    salida = []
    for g, miembros in grupos.items():
        firma = hashlib.sha1("|".join(sorted(f"{m['id']}:{m['sha1']}" for m in miembros)).encode()).hexdigest()
        if forzar or hechas.get(g) != firma:
            salida.append((g, firma, miembros))
    return salida


def peticion(con, grupo, miembros):
    # versión base para leer: castellano con más texto; si no, la de más texto
    base = max(miembros, key=lambda m: (m["idioma"] in ("", "castellano"), m["caracteres"] or 0))
    texto = (base["texto"] or "")[:MAX_CARACTERES]
    excel = None
    for m in miembros:
        excel = excel or con.execute("SELECT * FROM excel WHERE file_id = ?", (m["id"],)).fetchone()
    partes = [f"Archivo: {base['nombre']}", f"Carpeta: {base['ruta']}"]
    if len(miembros) > 1:
        partes.append("Otras versiones: " + "; ".join(m["nombre"] for m in miembros if m is not base))
    if excel:
        datos = {k: excel[k] for k in excel.keys() if k != "file_id" and excel[k]}
        partes.append("Datos del listado de dossieres: " + json.dumps(datos, ensure_ascii=False))
    partes.append("TEXTO DEL DOSSIER:\n" + (texto or "(no se ha podido extraer texto; usa el nombre y el listado)"))
    return base, {
        "custom_id": grupo,
        "params": {
            "model": MODELO,
            "max_tokens": 3000,
            "system": [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
            "tools": [HERRAMIENTA],
            # "auto" como en box-etiquetar: algunos modelos no aceptan forzar la herramienta.
            "tool_choice": {"type": "auto"},
            "messages": [{"role": "user", "content": "\n\n".join(partes)}],
        },
    }


def crear_lote(cliente, peticiones, intentos=40):
    for i in range(intentos):
        try:
            return cliente.messages.batches.create(requests=peticiones)
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            espera = min(600, 60 * (i + 1))
            print(f"  API ocupada ({type(e).__name__}); reintento en {espera // 60} min...")
            time.sleep(espera)
    raise RuntimeError("No se pudo crear el lote tras muchos reintentos. Relanza más tarde.")


def cmd_enviar(args):
    con = db.conectar()
    pendientes = grupos_para_ficha(con, args.forzar)
    if not pendientes:
        print("Todas las fichas están al día. No hay nada que enviar.")
        return False
    peticiones = [peticion(con, g, m)[1] for g, _, m in pendientes]
    caracteres = sum(len(p["params"]["messages"][0]["content"]) for p in peticiones)
    pin, pout = PRECIOS_BATCH.get(MODELO, (1.0, 5.0))
    coste = ((caracteres / 3.5 + len(peticiones) * 700) * pin + len(peticiones) * 600 * pout) / 1e6
    print(f"{len(peticiones)} dossieres para {MODELO} · {caracteres / 1e6:.1f} M caracteres · ≈ {coste:.2f} $")
    confirmar(args, "¿Enviar el lote?")
    cliente = anthropic.Anthropic(api_key=box.entorno("ANTHROPIC_API_KEY"))
    trozo, mb = [], 0.0
    lotes = []
    for p in peticiones + [None]:
        tam = len(json.dumps(p, ensure_ascii=False).encode()) / 1e6 if p else 0
        if trozo and (p is None or len(trozo) >= MAX_PETICIONES_LOTE or mb + tam > MAX_MB_LOTE):
            lote = crear_lote(cliente, trozo)
            con.execute("INSERT INTO lotes (id, creado, grupos) VALUES (?, ?, ?)", (lote.id, ahora(), len(trozo)))
            con.commit()
            lotes.append(lote.id)
            print(f"Lote enviado: {lote.id} ({len(trozo)} dossieres)")
            trozo, mb = [], 0.0
        if p:
            trozo.append(p)
            mb += tam
    # guardamos la firma solo al recoger; mientras, apuntamos qué lote lleva cada grupo
    escribir_firmas(con, {g: f for g, f, _ in pendientes})
    return bool(lotes)


def escribir_firmas(con, firmas):
    ruta = db.DATA / "firmas_enviadas.json"
    previo = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    previo.update(firmas)
    ruta.write_text(json.dumps(previo), encoding="utf-8")


# ---------------------------------------------------------------- 6. estado / recoger
def lotes_abiertos(con):
    return [r["id"] for r in con.execute("SELECT id FROM lotes WHERE recogido = 0")]


def cmd_estado(_args):
    con = db.conectar()
    abiertos = lotes_abiertos(con)
    if not abiertos:
        print("No hay lotes pendientes de recoger.")
        return True
    cliente = anthropic.Anthropic(api_key=box.entorno("ANTHROPIC_API_KEY"))
    terminado = True
    for lid in abiertos:
        b = cliente.messages.batches.retrieve(lid)
        c = b.request_counts
        print(f"  {lid}  {b.processing_status:12s} ok={c.succeeded} error={c.errored} en curso={c.processing}")
        terminado &= b.processing_status == "ended"
    return terminado


def lista(v):
    return ", ".join(str(x).strip() for x in v if str(x).strip()) if isinstance(v, list) else str(v or "")


def cmd_recoger(_args):
    con = db.conectar()
    abiertos = lotes_abiertos(con)
    if not abiertos:
        print("No hay lotes pendientes de recoger.")
        reconstruir_buscador(con)
        return
    if not cmd_estado(None):
        sys.exit("Todavía hay lotes en curso. Vuelve a probar más tarde.")
    cliente = anthropic.Anthropic(api_key=box.entorno("ANTHROPIC_API_KEY"))
    ruta_firmas = db.DATA / "firmas_enviadas.json"
    firmas = json.loads(ruta_firmas.read_text(encoding="utf-8")) if ruta_firmas.exists() else {}
    pin, pout = PRECIOS_BATCH.get(MODELO, (1.0, 5.0))
    ok = err = 0
    coste_total = 0.0
    motivos = Counter()
    for lid in abiertos:
        for r in cliente.messages.batches.results(lid):
            grupo = r.custom_id
            if r.result.type != "succeeded":
                err += 1
                e = getattr(r.result, "error", None)
                interno = getattr(e, "error", e)
                motivo = f"{r.result.type}: {getattr(interno, 'message', interno) or ''}"
                motivos[motivo[:120]] += 1
                print(f"  {grupo}: {motivo[:200]}")
                continue
            msg = r.result.message
            ficha = next((b.input for b in msg.content if b.type == "tool_use"), None)
            if ficha is None:  # sin herramienta: intentar leer un JSON del texto
                texto = "".join(getattr(b, "text", "") for b in msg.content)
                m = re.search(r"\{.*\}", texto, re.S)
                try:
                    ficha = json.loads(m.group(0)) if m else None
                except ValueError:
                    ficha = None
            if not ficha:
                err += 1
                motivo = f"sin ficha (stop_reason={getattr(msg, 'stop_reason', '?')})"
                motivos[motivo] += 1
                print(f"  {grupo}: {motivo}")
                continue
            u = msg.usage
            tin = u.input_tokens + (u.cache_creation_input_tokens or 0) * 1.25 + (u.cache_read_input_tokens or 0) * 0.1
            coste = (tin * pin + u.output_tokens * pout) / 1e6
            coste_total += coste
            idiomas = sorted({a["idioma"] or "castellano" for a in db.archivos_de(con, grupo)})
            base = db.archivos_de(con, grupo)
            con.execute("""INSERT OR REPLACE INTO fichas VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (grupo, firmas.get(grupo), base[0]["id"] if base else None, MODELO,
                         ficha.get("titulo", ""), ficha.get("cliente", ""), ficha.get("campana", ""),
                         str(ficha.get("anio", "") or ""), ", ".join(idiomas), lista(ficha.get("vehiculos")),
                         lista(ficha.get("formato")), lista(ficha.get("tematicas")), ficha.get("publico", ""),
                         ficha.get("resumen", ""), lista(ficha.get("palabras_clave")), round(coste, 5),
                         VERSION, ahora()))
            ok += 1
        con.execute("UPDATE lotes SET recogido = 1 WHERE id = ?", (lid,))
        con.commit()
    print(f"{ok} fichas guardadas, {err} con error · coste ≈ {coste_total:.2f} $")
    for motivo, n in motivos.most_common():
        print(f"  {n:4d} × {motivo}")
    if err:
        print("Los que han fallado se reintentan con: python indexar.py enviar --si")
    reconstruir_buscador(con)


def reconstruir_buscador(con):
    con.execute("DELETE FROM buscador")
    for f in con.execute("SELECT * FROM fichas").fetchall():
        archivos = db.archivos_de(con, f["grupo"])
        if not archivos:
            continue
        nombres = " | ".join(a["nombre"] for a in archivos)
        textos = [t["texto"] for t in con.execute(
            f"SELECT texto FROM textos WHERE file_id IN ({','.join('?' * len(archivos))})",
            [a["id"] for a in archivos]) if t["texto"]]
        excel = con.execute(
            f"SELECT cliente, campana, tematicas FROM excel WHERE file_id IN ({','.join('?' * len(archivos))})",
            [a["id"] for a in archivos]).fetchall()
        extra = " ".join(" ".join(x for x in e if x) for e in excel)
        con.execute("INSERT INTO buscador VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (f["grupo"], f["titulo"], f["cliente"], f["campana"], f"{f['tematicas']} {extra}",
                     f["vehiculos"], f["formato"], f["resumen"], f["palabras_clave"], nombres,
                     max(textos, key=len)[:200000] if textos else ""))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM buscador").fetchone()[0]
    print(f"Buscador rehecho: {n} dossieres.")
    exportar_indice(con)


def exportar_indice(con):
    """indice_dossieres.json para la herramienta buscar_dossieres del conector de Box (Claude Team).
    Solo fichas y archivos (sin el texto completo de los PDF)."""
    dossieres = []
    for f in con.execute("SELECT * FROM fichas ORDER BY grupo").fetchall():
        archivos = db.archivos_de(con, f["grupo"])
        if not archivos:
            continue
        fechas = [a["modificado"] for a in archivos if a["modificado"]]
        dossieres.append({
            "grupo": f["grupo"], "titulo": f["titulo"], "cliente": f["cliente"], "campana": f["campana"],
            "anio": f["anio"], "fecha": max(fechas)[:10] if fechas else "", "idiomas": f["idiomas"],
            "vehiculos": f["vehiculos"], "formato": f["formato"], "tematicas": f["tematicas"],
            "publico": f["publico"], "resumen": f["resumen"], "palabras_clave": f["palabras_clave"],
            "archivos": [{"id": a["id"], "nombre": a["nombre"], "idioma": a["idioma"] or "castellano",
                          "mb": round((a["tamano"] or 0) / 1048576, 1), "modificado": (a["modificado"] or "")[:10],
                          "por_defecto": bool(a["preferido"]), "carpeta": a["ruta"]} for a in archivos],
        })
    ruta = db.DATA / "indice_dossieres.json"
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps({"generado": ahora(), "version": VERSION, "dossieres": dossieres},
                                   ensure_ascii=False), encoding="utf-8")
    temporal.replace(ruta)   # se sustituye de golpe: nunca se sirve un archivo a medias
    print(f"Índice para el conector: {len(dossieres)} dossieres en {ruta}.")


def cmd_exportar(_args):
    exportar_indice(db.conectar())


# ---------------------------------------------------------------- todo / actualizar / buscar
def cmd_todo(args):
    cmd_listar(args)
    cmd_excel(args)
    cmd_textos(args)
    cmd_agrupar(args)
    if cmd_enviar(args) or lotes_abiertos(db.conectar()):
        print("Esperando a que termine el lote (se consulta cada 2 min)...")
        while not cmd_estado(None):
            time.sleep(120)
    cmd_recoger(args)


def cmd_actualizar(args):
    args.si = True
    cmd_todo(args)


def cmd_buscar(args):
    con = db.conectar()
    for i, f in enumerate(db.buscar(con, args.texto, 10), 1):
        print(f"{i:2d}. {f['titulo']} · {f['cliente']} · {f['anio'] or 's/f'} · {f['vehiculos']}")
        print(f"    {f['resumen'][:180]}")


def cmd_resumen(_args):
    con = db.conectar()
    q = lambda s: con.execute(s).fetchone()[0]  # noqa: E731
    print(f"Archivos activos: {q('SELECT COUNT(*) FROM archivos WHERE activo = 1')}")
    print(f"Con texto:        {q('SELECT COUNT(*) FROM textos WHERE caracteres > 0')}")
    print(f"Dossieres:        {q('SELECT COUNT(DISTINCT grupo) FROM archivos WHERE activo = 1')}")
    print(f"Fichas:           {q('SELECT COUNT(*) FROM fichas')}")
    print(f"En el buscador:   {q('SELECT COUNT(*) FROM buscador')}")
    print(f"Coste fichas:     {q('SELECT COALESCE(SUM(coste_usd), 0) FROM fichas'):.2f} $")


def main():
    p = argparse.ArgumentParser(description="Indexador de dossieres de Box")
    p.add_argument("comando", choices=["listar", "excel", "textos", "agrupar", "enviar", "estado", "recoger",
                                          "todo", "actualizar", "buscar", "resumen", "exportar"])
    p.add_argument("texto", nargs="?", default="", help="con buscar: lo que quieres encontrar")
    p.add_argument("--si", action="store_true", help="no pedir confirmación")
    p.add_argument("--forzar", action="store_true", help="con enviar/todo: rehacer todas las fichas")
    args = p.parse_args()
    {"listar": cmd_listar, "excel": cmd_excel, "textos": cmd_textos, "agrupar": cmd_agrupar,
     "enviar": cmd_enviar, "estado": cmd_estado, "recoger": cmd_recoger, "todo": cmd_todo,
     "actualizar": cmd_actualizar, "buscar": cmd_buscar, "resumen": cmd_resumen,
     "exportar": cmd_exportar}[args.comando](args)


if __name__ == "__main__":
    main()

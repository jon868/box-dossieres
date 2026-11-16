"""
Fotos de campañas para el bot: busca en el índice de fotos etiquetadas (indice.json, el que genera box-etiquetar
y usa el conector de Box con buscar_fotos) y saca las vistas previas de Box para mandarlas por Telegram.

La búsqueda es la misma que buscar_fotos de server.js (repo box): mismos filtros, misma puntuación por campos.
Si se cambia allí, conviene cambiarla aquí también.

Siempre quita fotos con posibles menores y fotos no aptas para enseñar a un cliente.
Sin filtro de plano, mezcla exteriores e interiores (ext, int, ext, int, otro...).
"""
import os
import re
import threading
import time
import unicodedata

import requests

import box

URL = os.getenv("FOTOS_INDICE_URL", "")
USUARIO = os.getenv("FOTOS_INDICE_USUARIO", "")
PASSWORD = os.getenv("FOTOS_INDICE_PASSWORD", "")
REFRESCO_MIN = int(os.getenv("FOTOS_REFRESCO_MIN", "60"))
POR_TANDA = int(os.getenv("FOTOS_POR_TANDA", "4"))
TAMANO_PREVIA = os.getenv("FOTOS_TAMANO", "1024")   # Box da 320 o 1024 en jpg

FLOTA = ["13,80", "Airstream", "CajaMar", "Camión rígido", "Cotazero", "Furgón", "Plataformas", "Semirremolque",
         "Set TV"]
PLANOS = ["exterior del vehículo", "interior del vehículo", "evento con público", "montaje o producción", "detalle",
          "retrato o equipo", "otro"]
LINEAS = ["Roadshow", "Acciones pedagógicas y sensibilización", "Marca y patrocinio", "Promoción y producto",
          "Manufacturing", "Agencia"]
EXT, INT = "exterior del vehículo", "interior del vehículo"
PARADAS = set("de la el los las del y en un una con para por al a o u que se sin sobre entre tipo foto fotos "
              "imagen imagenes".split())

_indice = {"fotos": [], "por_id": {}, "generado": None, "t": 0, "error": None}
_cerrojo = threading.Lock()


def activo():
    return bool(URL)


def norm(t):
    t = unicodedata.normalize("NFD", str(t or ""))
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower()


def palabras(t):
    return [w for w in re.split(r"[^a-z0-9ñ]+", norm(t)) if len(w) > 1 and w not in PARADAS]


def _lista(v):
    return v if isinstance(v, list) else ([v] if v else [])


def _cargar():
    auth = (USUARIO, PASSWORD) if USUARIO else None
    r = requests.get(URL, auth=auth, timeout=60)
    r.raise_for_status()
    data = r.json()
    fotos = []
    for f in data.get("fotos") or []:
        f = dict(f)
        f["_alto"] = set(palabras(" ".join(str(x or "") for x in (
            f.get("marca"), f.get("cliente"), " ".join(_lista(f.get("palabras"))), f.get("vehiculo"),
            f.get("vehiculo_campana")))))
        f["_medio"] = set(palabras(" ".join(str(x or "") for x in (
            f.get("descripcion"), f.get("campana"), " ".join(_lista(f.get("lineas"))), f.get("plano"),
            f.get("tipo_vehiculo")))))
        f["_bajo"] = set(palabras(f"{f.get('nombre') or ''} {f.get('carpetas') or ''}"))
        fotos.append(f)
    _indice.update(fotos=fotos, por_id={str(f["id"]): f for f in fotos}, generado=data.get("generado"),
                   t=time.time(), error=None)
    print(f"Índice de fotos cargado: {len(fotos)} fotos (generado {_indice['generado']})", flush=True)


def indice():
    """Fotos del índice; lo recarga cada FOTOS_REFRESCO_MIN minutos. Si falla, sigue con el anterior."""
    if not activo():
        return []
    with _cerrojo:
        if not _indice["fotos"] or time.time() - _indice["t"] > REFRESCO_MIN * 60:
            try:
                _cargar()
            except Exception as ex:  # noqa: BLE001
                _indice["error"] = str(ex)
                _indice["t"] = time.time()   # no reintentar en cada mensaje
                print(f"[fotos] no se pudo cargar el índice: {ex}", flush=True)
        return _indice["fotos"]


def foto(fid):
    indice()
    return _indice["por_id"].get(str(fid))


def _puntuar(f, terminos):
    total = encontrados = 0
    for t in terminos:
        p = 0
        for conj, peso in ((f["_alto"], 3), (f["_medio"], 2), (f["_bajo"], 1)):
            if t in conj:
                p = max(p, peso)
            elif len(t) >= 4:
                for w in conj:
                    if len(w) >= 4 and (w.startswith(t) or t.startswith(w)):
                        p = max(p, peso - 0.5)
                        break
        if p:
            encontrados += 1
        total += p
    return total, encontrados


def _mezclar(lista):
    """Intercala exteriores e interiores conservando el orden de relevancia dentro de cada grupo:
    ext, int, ext, int, otro, ext, int, ext, int, otro... Si se acaba un grupo, sigue con los demás."""
    colas = {"ext": [f for f in lista if f.get("plano") == EXT],
             "int": [f for f in lista if f.get("plano") == INT],
             "otro": [f for f in lista if f.get("plano") not in (EXT, INT)]}
    patron = ["ext", "int", "ext", "int", "otro"]
    salida, i = [], 0
    while any(colas.values()):
        cola = colas[patron[i % len(patron)]]
        if cola:
            salida.append(cola.pop(0))
        i += 1
    return salida


def buscar(texto="", vehiculo=None, plano=None, linea=None, cliente=None, marca=None, anio_desde=None,
           anio_hasta=None, offset=0, limite=None, excluir=()):
    """Devuelve (página de fotos, total disponible). Misma lógica que buscar_fotos del conector, siempre sin
    posibles menores y solo aptas; sin plano, mezcla exteriores e interiores. `excluir`: ids ya enseñados."""
    limite = max(1, min(int(limite or POR_TANDA), 10))
    offset = max(0, int(offset or 0))
    nc, nm = norm(cliente) if cliente else None, norm(marca) if marca else None
    base = []
    for f in indice():
        if vehiculo and f.get("vehiculo") != vehiculo and vehiculo not in (f.get("vehiculo_campana") or "").split(", "):
            continue
        if plano and f.get("plano") != plano:
            continue
        if linea and linea not in _lista(f.get("lineas")):
            continue
        if nc and nc not in norm(f"{f.get('cliente') or ''} {f.get('campana') or ''} {f.get('carpetas') or ''}"):
            continue
        if nm and nm not in norm(f.get("marca")):
            continue
        anio = f.get("anio") or 0
        if (anio_desde and anio < anio_desde) or (anio_hasta and (not anio or anio > anio_hasta)):
            continue
        if f.get("menores") in ("sí", "dudoso") or not f.get("apta") or str(f["id"]) in excluir:
            continue
        base.append(f)
    terminos = palabras(texto)
    if terminos:
        minimo = max(1, -(-len(terminos) // 2))
        puntuadas = [(f, *_puntuar(f, terminos)) for f in base]
        puntuadas = [x for x in puntuadas if x[2] >= minimo]
        puntuadas.sort(key=lambda x: (x[2], x[1], x[0].get("anio") or 0), reverse=True)
        lista = [x[0] for x in puntuadas]
    else:
        lista = sorted(base, key=lambda f: f.get("anio") or 0, reverse=True)
    if not plano:
        lista = _mezclar(lista)
    return lista[offset:offset + limite], len(lista)


def ficha(f):
    """Lo que ve Claude de cada foto (sin los campos internos)."""
    return {k: f.get(k) for k in ("id", "campana", "anio", "vehiculo", "plano", "marca", "descripcion")
            if f.get(k)}


def pie(f, n=None):
    """Texto bajo cada foto en Telegram."""
    partes = [x for x in (f.get("campana"), str(f.get("anio") or ""), f.get("plano")) if x]
    return (f"{n}. " if n else "") + " · ".join(partes)


def previa(fid):
    """JPG de vista previa (FOTOS_TAMANO px) que genera Box; None si no la da."""
    hint = f"[jpg?dimensions={TAMANO_PREVIA}x{TAMANO_PREVIA}]"
    r = box.get(f"{box.API}/files/{fid}", params={"fields": "representations"}, headers={"x-rep-hints": hint})
    if r.status_code != 200:
        return None
    reps = (r.json().get("representations") or {}).get("entries") or []
    if not reps:
        return None
    rep, limite = reps[0], time.time() + 25
    while rep["status"]["state"] in ("none", "pending") and time.time() < limite:
        info = box.get(rep["info"]["url"])
        if info.status_code != 200:
            return None
        rep = info.json()
        if rep["status"]["state"] != "success":
            time.sleep(1.5)
    if rep["status"]["state"] != "success":
        return None
    c = box.get(rep["content"]["url_template"].replace("{+asset_path}", ""))
    return c.content if c.status_code == 200 else None

"""
Acceso a Box con la app de cuenta de servicio "claudebox" (Client Credentials Grant).
Mismas variables que box-etiquetar y el conector. Solo LEE de Box.
"""
import io
import os
import re
import sys
import time

import requests

API = "https://api.box.com/2.0"
_token = {"v": None, "exp": 0}


def entorno(nombre):
    valor = os.getenv(nombre)
    if not valor:
        sys.exit(f"Falta la variable de entorno {nombre} (mira .env.example).")
    return valor


def token():
    if _token["v"] and time.time() < _token["exp"] - 60:
        return _token["v"]
    r = requests.post("https://api.box.com/oauth2/token", data={
        "grant_type": "client_credentials",
        "client_id": entorno("BOX_CLIENT_ID"),
        "client_secret": entorno("BOX_CLIENT_SECRET"),
        "box_subject_type": os.getenv("BOX_SUBJECT_TYPE", "enterprise"),
        "box_subject_id": entorno("BOX_SUBJECT_ID"),
    }, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Box no ha dado token ({r.status_code}): {r.text[:300]}")
    j = r.json()
    _token.update(v=j["access_token"], exp=time.time() + j.get("expires_in", 3600))
    return _token["v"]


def get(url, params=None, headers=None, allow_redirects=True, timeout=120):
    r = None
    for intento in range(6):
        h = {"Authorization": f"Bearer {token()}"}
        if headers:
            h.update(headers)
        r = requests.get(url, params=params, headers=h, timeout=timeout, allow_redirects=allow_redirects)
        if r.status_code == 401:
            _token["v"] = None
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(int(r.headers.get("Retry-After", 2 ** intento)))
            continue
        return r
    return r


def listar(folder_id, ruta, salida, extensiones, excluir_ids=(), excluir_patron=None):
    """Recorre la carpeta y sus subcarpetas y añade a `salida` los archivos con esas extensiones."""
    offset = 0
    while True:
        r = get(f"{API}/folders/{folder_id}/items",
                params={"fields": "id,type,name,size,extension,sha1,modified_at", "limit": 1000, "offset": offset})
        r.raise_for_status()
        j = r.json()
        for it in j["entries"]:
            if it["id"] in excluir_ids or (excluir_patron and excluir_patron.search(it["name"])):
                continue
            if it["type"] == "folder":
                listar(it["id"], f"{ruta}/{it['name']}" if ruta else it["name"], salida,
                       extensiones, excluir_ids, excluir_patron)
            elif (it["type"] == "file" and (it.get("extension") or "").lower() in extensiones
                  and not it["name"].startswith("._")):
                salida.append({"id": it["id"], "nombre": it["name"], "ruta": ruta,
                               "ext": it["extension"].lower(), "tamano": it.get("size", 0),
                               "sha1": it.get("sha1"), "modificado": it.get("modified_at")})
        offset += len(j["entries"])
        if not j["entries"] or offset >= j.get("total_count", 0):
            break


def descargar(file_id):
    r = get(f"{API}/files/{file_id}/content", timeout=600)
    r.raise_for_status()
    return r.content


def enlace_temporal(file_id):
    """URL de descarga directa que Box genera para este archivo; caduca en pocos minutos."""
    r = get(f"{API}/files/{file_id}/content", allow_redirects=False)
    if r.status_code in (301, 302) and r.headers.get("Location"):
        return r.headers["Location"]
    r.raise_for_status()
    return None


def texto_box(file_id, espera_max=60):
    """Texto que Box extrae del archivo (representación extracted_text). None si no hay."""
    r = get(f"{API}/files/{file_id}", params={"fields": "representations"},
            headers={"x-rep-hints": "[extracted_text]"})
    if r.status_code != 200:
        return None
    reps = (r.json().get("representations") or {}).get("entries") or []
    rep = next((x for x in reps if x.get("representation") == "extracted_text"), None)
    if not rep:
        return None
    limite = time.time() + espera_max
    while rep["status"]["state"] in ("none", "pending") and time.time() < limite:
        time.sleep(3)
        info = get(rep["info"]["url"])
        if info.status_code != 200:
            return None
        rep = info.json()
    if rep["status"]["state"] != "success":
        return None
    url = rep["content"]["url_template"].replace("{+asset_path}", "")
    c = get(url)
    if c.status_code != 200:
        return None
    c.encoding = c.encoding or "utf-8"
    return c.text


def texto_local(datos, ext):
    """Plan B si Box no da el texto: lo extraemos nosotros del archivo descargado."""
    if ext == "pdf":
        from pypdf import PdfReader
        lector = PdfReader(io.BytesIO(datos))
        return "\n\n".join((p.extract_text() or "") for p in lector.pages)
    if ext == "pptx":
        from pptx import Presentation
        prs = Presentation(io.BytesIO(datos))
        partes = []
        for i, diapo in enumerate(prs.slides, 1):
            textos = [s.text_frame.text for s in diapo.shapes if s.has_text_frame and s.text_frame.text.strip()]
            partes.append(f"[{i}]\n" + "\n".join(textos))
        return "\n\n".join(partes)
    return ""


def id_de_enlace(enlace):
    m = re.search(r"/file/(\d+)", str(enlace or ""))
    return m.group(1) if m else None

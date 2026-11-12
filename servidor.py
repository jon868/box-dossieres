"""
Mini servidor HTTP que publica para el conector de Box indice_dossieres.json (herramienta buscar_dossieres) e
indice_catalogos.json (herramienta buscar_catalogos: el mismo catálogo que usa el bot, que se mantiene solo).
Protegido con usuario y contraseña (INDICE_USUARIO / INDICE_PASSWORD). No sirve nada más: ni la base de
datos, ni el registro de uso, ni los PDF.

Lo arranca bot.py en un hilo si INDICE_PASSWORD está definida.
"""
import base64
import hmac
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import json

import catalogos
import db

USUARIO = os.getenv("INDICE_USUARIO", "alegria")
PASSWORD = os.getenv("INDICE_PASSWORD", "")
PUERTO = int(os.getenv("PORT", "8080"))
RUTA = "/indice_dossieres.json"
RUTA_CATALOGOS = "/indice_catalogos.json"


def indice_catalogos():
    """El catálogo del bot sin lo interno (descartados): catálogos, ediciones, archivos y formatos."""
    d = catalogos.datos()
    return json.dumps({"version": d.get("version"), "generado": time.strftime("%Y-%m-%dT%H:%M:%S"),
                       "formatos": d.get("formatos", {}), "catalogos": d.get("catalogos", [])},
                      ensure_ascii=False).encode("utf-8")


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *_):   # sin una línea de log por petición
        pass

    def _responder(self, codigo, cuerpo=b"", tipo="text/plain; charset=utf-8", extra=None):
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(cuerpo)))
        self.send_header("X-Robots-Tag", "noindex, nofollow")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(cuerpo)

    def _autorizado(self):
        esperado = "Basic " + base64.b64encode(f"{USUARIO}:{PASSWORD}".encode()).decode()
        return hmac.compare_digest(self.headers.get("Authorization", ""), esperado)

    def do_GET(self):
        if self.path == "/salud":
            return self._responder(200, b"ok")
        if not self._autorizado():
            return self._responder(401, b"Necesita usuario y contrasena",
                                   extra={"WWW-Authenticate": 'Basic realm="box-dossieres"'})
        ruta = self.path.split("?")[0]
        if ruta == RUTA_CATALOGOS:
            return self._responder(200, indice_catalogos(), "application/json; charset=utf-8")
        if ruta != RUTA:
            return self._responder(404, b"No encontrado")
        archivo = db.DATA / "indice_dossieres.json"
        if not archivo.exists():
            return self._responder(503, b"Todavia no hay indice: ejecuta python indexar.py exportar")
        return self._responder(200, archivo.read_bytes(), "application/json; charset=utf-8")

    do_HEAD = do_GET


def arrancar():
    print(f"Índices para el conector en el puerto {PUERTO}: {RUTA} y {RUTA_CATALOGOS}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PUERTO), Manejador).serve_forever()

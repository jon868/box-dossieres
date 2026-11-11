#!/usr/bin/env python3
"""
Bot de Telegram para consultar y descargar dossieres de alegria-activity.

- Solo chats privados y solo usuarios de la lista de permitidos (por ID numérico de Telegram).
- Saluda por el nombre que el administrador pone al dar de alta (si no, el nombre de Telegram).
- Busca en el índice que genera indexar.py y envía el PDF; si pesa más de 50 MB, un enlace temporal de Box.
- Opcional: relanza el indexador cada noche (AUTO_INDEXAR=true, INDEXAR_HORA=3).

Comandos de administración: /permitir <id> <nombre>, /quitar <id>, /usuarios, /informe [días]
"""
import html
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

import asistente  # noqa: E402
import box  # noqa: E402
import db  # noqa: E402
import uso  # noqa: E402

TOKEN = box.entorno("TELEGRAM_TOKEN")
TG = f"https://api.telegram.org/bot{TOKEN}"
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
LIMITE_TG = 49 * 1024 * 1024          # los bots no pueden enviar archivos de más de 50 MB
HORAS_SALUDO = int(os.getenv("HORAS_SALUDO", "6"))
RESULTADOS = int(os.getenv("RESULTADOS", "5"))
AUTO_INDEXAR = os.getenv("AUTO_INDEXAR", "false").lower() == "true"
INDEXAR_HORA = int(os.getenv("INDEXAR_HORA", "3"))
USAR_CLAUDE = os.getenv("USAR_CLAUDE", "true").lower() == "true"   # false: solo búsqueda por palabras
HILOS = int(os.getenv("HILOS_BOT", "4"))


# ---------------------------------------------------------------- Telegram
def tg(metodo, **datos):
    archivos = datos.pop("files", None)
    r = requests.post(f"{TG}/{metodo}", data=datos if archivos else None, json=None if archivos else datos,
                      files=archivos, timeout=300 if archivos else 70)
    j = r.json()
    if not j.get("ok"):
        print(f"Telegram {metodo}: {j.get('description')}", flush=True)
    return j.get("result")


def enviar(chat, texto, botones=None):
    datos = {"chat_id": chat, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True}
    if botones:
        datos["reply_markup"] = {"inline_keyboard": botones}
    return tg("sendMessage", **datos)


def e(t):
    return html.escape(str(t or ""))


# ---------------------------------------------------------------- usuarios
def usuario(con, uid):
    return con.execute("SELECT * FROM usuarios WHERE telegram_id = ?", (uid,)).fetchone()


def registrar(con, uid, accion, detalle=""):
    con.execute("INSERT INTO registro (telegram_id, accion, detalle) VALUES (?, ?, ?)", (uid, accion, detalle))
    con.commit()


def saludo(con, u, de):
    """'Hola Juncal 👋' la primera vez y tras unas horas sin hablar; si no, nada."""
    nombre = u["nombre"] or de.get("first_name") or ""
    ultimo = u["ultimo_saludo"]
    ahora = datetime.now(timezone.utc)
    if ultimo and ahora - datetime.fromisoformat(ultimo) < timedelta(hours=HORAS_SALUDO):
        return ""
    con.execute("UPDATE usuarios SET ultimo_saludo = ? WHERE telegram_id = ?", (ahora.isoformat(), u["telegram_id"]))
    con.commit()
    return f"Hola {e(nombre)} 👋\n"


def asegurar_admins(con):
    for uid in ADMINS:
        con.execute("""INSERT INTO usuarios (telegram_id, admin, alta) VALUES (?, 1, datetime('now'))
                       ON CONFLICT(telegram_id) DO UPDATE SET admin = 1""", (uid,))
    con.commit()


def avisar_admins(texto):
    for uid in ADMINS:
        enviar(uid, texto)


# ---------------------------------------------------------------- comandos
def cmd_admin(con, chat, u, texto):
    partes = texto.split(maxsplit=2)
    orden = partes[0].split("@")[0].lower()
    if orden == "/permitir":
        if len(partes) < 3 or not partes[1].lstrip("-").isdigit():
            return enviar(chat, "Uso: <code>/permitir 123456789 Nombre</code>") or True
        nuevo, nombre = int(partes[1]), partes[2].strip()
        con.execute("""INSERT INTO usuarios (telegram_id, nombre, alta) VALUES (?, ?, datetime('now'))
                       ON CONFLICT(telegram_id) DO UPDATE SET nombre = excluded.nombre""", (nuevo, nombre))
        con.commit()
        registrar(con, u["telegram_id"], "permitir", f"{nuevo} {nombre}")
        enviar(chat, f"✅ {e(nombre)} ({nuevo}) ya puede usar el bot.")
        enviar(nuevo, f"Hola {e(nombre)} 👋 Ya tienes acceso al buscador de dossieres. "
                      "Escríbeme qué buscas, por ejemplo: <i>salud mental en Airstream</i>.\n"
                      "Tus búsquedas se guardan para ir mejorando el buscador.")
        return True
    if orden == "/quitar":
        if len(partes) < 2 or not partes[1].lstrip("-").isdigit():
            return enviar(chat, "Uso: <code>/quitar 123456789</code>") or True
        quitado = int(partes[1])
        if quitado in ADMINS:
            return enviar(chat, "Ese usuario es administrador (ADMIN_IDS); quítalo de ahí.") or True
        con.execute("DELETE FROM usuarios WHERE telegram_id = ?", (quitado,))
        con.commit()
        registrar(con, u["telegram_id"], "quitar", str(quitado))
        return enviar(chat, f"Hecho: {quitado} ya no tiene acceso.") or True
    if orden == "/informe":
        dias = int(partes[1]) if len(partes) > 1 and partes[1].isdigit() else 7
        return enviar(chat, "<pre>" + e(uso.resumen(con, dias)) + "</pre>") or True
    if orden == "/usuarios":
        filas = con.execute("SELECT * FROM usuarios ORDER BY admin DESC, nombre").fetchall()
        lineas = [f"{'⭐ ' if f['admin'] else '• '}{e(f['nombre'] or '(sin nombre)')} · <code>{f['telegram_id']}</code>"
                  for f in filas]
        return enviar(chat, "<b>Usuarios con acceso</b>\n" + "\n".join(lineas)) or True
    return False


def lista_con_botones(inicio, mensaje, fichas, iid=None):
    lineas, botones = [inicio + e(mensaje)], []
    for i, f in enumerate(fichas, 1):
        fecha = f.get("anio") or (f.get("fecha") or "")[:7]
        datos = " · ".join(x for x in (f["cliente"], fecha, f["idiomas"]) if x)
        lineas.append(f"\n<b>{i}. {e(f['titulo'])}</b>\n<i>{e(datos)}</i>")
        # el id de la respuesta va en el botón para saber qué descarga viene de qué respuesta
        datos_boton = f"d:{f['grupo']}:{iid}" if iid else f"d:{f['grupo']}"
        botones.append([{"text": f"📥 {i}. {f['titulo'][:40]}", "callback_data": datos_boton}])
    return "\n".join(lineas)[:4000], botones


def responder_busqueda(con, chat, u, de, texto):
    uid = u["telegram_id"]
    inicio = saludo(con, u, de)
    registrar(con, uid, "buscar", texto[:300])
    uso.nueva_pregunta(con, uid)
    tg("sendChatAction", chat_id=chat, action="typing")
    t0 = time.time()
    traza = {"via": "directa"}
    if USAR_CLAUDE:
        try:
            respuesta = asistente.responder(con, uid, texto)
        except Exception as ex:  # noqa: BLE001  si Claude falla, búsqueda directa
            print(f"Asistente no disponible ({ex}); búsqueda directa.", flush=True)
            respuesta, traza = None, {"via": "directa", "error": str(ex)[:500]}
        if respuesta:
            mensaje, fichas, traza = respuesta
            traza["ms"] = round((time.time() - t0) * 1000)
            iid = uso.guardar(con, uid, texto, traza, mensaje, fichas)
            return enviar(chat, *lista_con_botones(inicio, mensaje, fichas, iid))
    resultados = db.buscar(con, texto, RESULTADOS)
    traza.update(ms=round((time.time() - t0) * 1000),
                 desenlace="recomendacion" if resultados else "sin_resultados",
                 busquedas=[{"parametros": {"consulta": texto}, "n": len(resultados)}])
    if not resultados:
        mensaje = ("No he encontrado dossieres con eso. Prueba con otras palabras "
                   "(cliente, tema, vehículo o tipo de acción).")
        uso.guardar(con, uid, texto, traza, mensaje, [])
        return enviar(chat, inicio + mensaje)
    mensaje = f"He encontrado {len(resultados)} dossier{'es' if len(resultados) > 1 else ''}:"
    iid = uso.guardar(con, uid, texto, traza, mensaje, resultados)
    return enviar(chat, *lista_con_botones(inicio, mensaje, resultados, iid))


def responder_descarga(con, consulta, u):
    chat = consulta["message"]["chat"]["id"]
    tg("answerCallbackQuery", callback_query_id=consulta["id"], text="Preparando el dossier...")
    partes = consulta["data"].split(":")
    grupo = partes[1]
    if len(partes) > 2 and partes[2].isdigit():
        uso.descarga(con, u["telegram_id"], int(partes[2]), grupo)
    archivos = db.archivos_de(con, grupo)
    if not archivos:
        return enviar(chat, "Ese dossier ya no está en Box.")
    a = archivos[0]
    registrar(con, u["telegram_id"], "descargar", f"{a['id']} {a['nombre']}")
    otros = [x for x in archivos[1:] if x["idioma"] and x["idioma"] != a["idioma"]]
    nota = ("\nTambién hay versión en " + ", ".join(sorted({x["idioma"] for x in otros}))) if otros else ""
    if a["tamano"] <= LIMITE_TG:
        tg("sendChatAction", chat_id=chat, action="upload_document")
        datos = box.descargar(a["id"])
        return tg("sendDocument", chat_id=chat, caption=(a["nombre"] + nota)[:1000],
                  files={"document": (a["nombre"], datos)})
    enlace = box.enlace_temporal(a["id"])
    return enviar(chat, f"<b>{e(a['nombre'])}</b> pesa {a['tamano'] / 1048576:.0f} MB y Telegram no deja "
                        f"enviarlo. Descárgalo aquí (el enlace caduca en unos minutos):\n{e(enlace)}{e(nota)}")


def atender(con, upd):
    if "callback_query" in upd:
        q = upd["callback_query"]
        u = usuario(con, q["from"]["id"])
        if not u or q["message"]["chat"]["type"] != "private":
            return tg("answerCallbackQuery", callback_query_id=q["id"], text="Sin acceso")
        try:
            return responder_descarga(con, q, u)
        except Exception as ex:  # noqa: BLE001
            print(f"Error en descarga: {ex}", flush=True)
            return enviar(q["message"]["chat"]["id"], "No he podido traer el archivo de Box. Prueba en un rato.")
    msg = upd.get("message")
    if not msg or msg["chat"]["type"] != "private" or "text" not in msg:
        return None
    de, chat, texto = msg["from"], msg["chat"]["id"], msg["text"].strip()
    u = usuario(con, de["id"])
    if not u:
        registrar(con, de["id"], "sin_acceso", texto[:200])
        enviar(chat, f"No tienes acceso a este bot. Tu ID es <code>{de['id']}</code>: pásaselo a quien lo administra.")
        quien = " ".join(x for x in (de.get("first_name"), de.get("last_name")) if x)
        alias = f" (@{de['username']})" if de.get("username") else ""
        return avisar_admins(f"🔔 Solicitud de acceso: {e(quien)}{e(alias)}\n"
                             f"Para darle acceso: <code>/permitir {de['id']} {e(de.get('first_name', ''))}</code>")
    if not u["nombre"] and de.get("first_name"):   # administradores: nombre de Telegram hasta que se cambie
        con.execute("UPDATE usuarios SET nombre = ? WHERE telegram_id = ?", (de["first_name"], de["id"]))
        con.commit()
        u = usuario(con, de["id"])
    if texto.startswith("/") and u["admin"] and cmd_admin(con, chat, u, texto):
        return None
    if texto.split("@")[0].lower() in ("/start", "/ayuda", "/help"):
        asistente.olvidar(de["id"])
        con.execute("UPDATE usuarios SET ultimo_saludo = NULL WHERE telegram_id = ?", (de["id"],))
        u = usuario(con, de["id"])
        return enviar(chat, saludo(con, u, de) + "Soy el buscador de dossieres de alegria-activity. Dime qué "
                                                 "buscas (cliente, tema, vehículo, tipo de acción) y te enseño los "
                                                 "dossieres y te los envío. Puedes preguntarme como a un compañero.\nEjemplos: "
                                                 "<i>bibliobús en inglés</i> · <i>algo de salud para colegios en "
                                                 "Airstream</i>\nCon /nuevo empiezo una búsqueda desde cero.")
    if texto.split("@")[0].lower() == "/nuevo":
        uso.reinicio(con, de["id"])
        asistente.olvidar(de["id"])
        return enviar(chat, "Hecho, empezamos de cero. ¿Qué dossier buscas?")
    if texto.startswith("/"):
        return enviar(chat, "No conozco ese comando. Escríbeme directamente lo que buscas.")
    return responder_busqueda(con, chat, u, de, texto)


# ---------------------------------------------------------------- indexado nocturno
def indexado_nocturno():
    hecho = None
    while True:
        ahora = datetime.now()
        if ahora.hour == INDEXAR_HORA and hecho != ahora.date():
            hecho = ahora.date()
            print("Lanzando el indexado nocturno...", flush=True)
            r = subprocess.run([sys.executable, "indexar.py", "actualizar"], capture_output=True, text=True)
            (db.DATA / "indexado.log").write_text(r.stdout + r.stderr, encoding="utf-8")
            if r.returncode:
                avisar_admins("⚠️ El indexado nocturno de dossieres ha fallado. Mira datos/indexado.log.")
        time.sleep(300)


def purga_diaria():
    while True:
        try:
            con = db.conectar()
            n = uso.purgar(con)
            con.close()
            if n:
                print(f"Registro: {n} respuestas antiguas borradas.", flush=True)
        except Exception as ex:  # noqa: BLE001
            print(f"Error purgando el registro: {ex}", flush=True)
        time.sleep(24 * 3600)


def main():
    if not ADMINS:
        sys.exit("Define ADMIN_IDS con tu ID de Telegram.")
    con = db.conectar()
    asegurar_admins(con)
    tg("setMyCommands", commands=[{"command": "start", "description": "Qué puedo hacer"},
                                  {"command": "nuevo", "description": "Empezar una búsqueda desde cero"}])
    if AUTO_INDEXAR:
        threading.Thread(target=indexado_nocturno, daemon=True).start()
    threading.Thread(target=purga_diaria, daemon=True).start()
    print("Bot de dossieres en marcha.", flush=True)
    hilos = ThreadPoolExecutor(HILOS)
    offset = None
    while True:
        try:
            updates = tg("getUpdates", timeout=50, offset=offset,
                         allowed_updates=["message", "callback_query"]) or []
        except requests.RequestException as ex:
            print(f"Sin conexión con Telegram: {ex}", flush=True)
            time.sleep(10)
            continue
        for upd in updates:
            offset = upd["update_id"] + 1
            hilos.submit(atender_seguro, upd)


def atender_seguro(upd):
    """Cada mensaje en su hilo y con su conexión: una respuesta lenta de Claude no bloquea a los demás."""
    con = db.conectar()
    try:
        atender(con, upd)
    except Exception as ex:  # noqa: BLE001
        print(f"Error atendiendo {upd.get('update_id')}: {ex}", flush=True)
    finally:
        con.close()


if __name__ == "__main__":
    main()

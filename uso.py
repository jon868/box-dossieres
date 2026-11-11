#!/usr/bin/env python3
"""
Registro de uso del bot y señales implícitas de calidad.

Cada respuesta del bot es una "interacción" (pregunta, búsquedas que hizo Claude, dossieres mostrados,
versión del prompt, tokens, tiempo). Después se le van sumando señales sin preguntar nada al usuario:
  - descarga:   descarga un dossier de esa respuesta (y en qué posición estaba)  → buena señal
  - repregunta: vuelve a preguntar a los pocos minutos sin haber descargado nada  → probable fallo
                (no cuenta si el bot le había hecho una pregunta; un "¿y en inglés?" sí cuenta: es una señal
                 débil, se revisa a mano)
  - reinicio:   usa /nuevo poco después de una respuesta sin descarga             → probable fallo
Una respuesta con dossieres y sin ninguna de las tres se cuenta como "sin reacción".

Uso desde la consola:
  python uso.py informe [--dias 7]    Cifras del periodo y casos a revisar
  python uso.py casos [--dias 7]      Detalle de las respuestas con señales de fallo (para afinar el prompt)
  python uso.py purgar                Borra el registro más antiguo que REGISTRO_DIAS
"""
import argparse
import json
import os
from datetime import datetime, timedelta, timezone

import db

MIN_REPREGUNTA = int(os.getenv("MIN_REPREGUNTA", "5"))     # minutos para considerar que vuelve a preguntar
MIN_REINICIO = int(os.getenv("MIN_REINICIO", "10"))
REGISTRO_DIAS = int(os.getenv("REGISTRO_DIAS", "365"))


def _ahora():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ts(texto):
    return datetime.fromisoformat(texto) if texto else None


def _ultima(con, uid):
    return con.execute("SELECT * FROM interacciones WHERE telegram_id = ? ORDER BY id DESC LIMIT 1",
                       (uid,)).fetchone()


def _senal(con, iid, uid, tipo, valor=None, detalle=""):
    con.execute("INSERT INTO senales (interaccion_id, telegram_id, tipo, valor, detalle) VALUES (?,?,?,?,?)",
                (iid, uid, tipo, valor, detalle))


def nueva_pregunta(con, uid):
    """Antes de responder: si la respuesta anterior no tuvo descarga y es reciente, cuenta como repregunta."""
    prev = _ultima(con, uid)
    # si el bot había hecho una pregunta, que el usuario conteste no es un fallo
    if prev and not prev["descargas"] and not prev["repregunta"] and prev["desenlace"] != "pregunta":
        minutos = (_ahora() - _ts(prev["ts"])).total_seconds() / 60
        if minutos <= MIN_REPREGUNTA:
            con.execute("UPDATE interacciones SET repregunta = 1 WHERE id = ?", (prev["id"],))
            _senal(con, prev["id"], uid, "repregunta", round(minutos * 60))
            con.commit()


def reinicio(con, uid):
    prev = _ultima(con, uid)
    if prev and not prev["descargas"] and (_ahora() - _ts(prev["ts"])).total_seconds() <= MIN_REINICIO * 60:
        con.execute("UPDATE interacciones SET reinicio = 1 WHERE id = ?", (prev["id"],))
        _senal(con, prev["id"], uid, "reinicio")
        con.commit()


def guardar(con, uid, pregunta, traza, mensaje, fichas):
    """Guarda la respuesta y devuelve su id (va en los botones de descarga)."""
    u = traza.get("uso", {})
    cur = con.execute(
        """INSERT INTO interacciones (telegram_id, pregunta, turnos_previos, via, desenlace, mensaje, busquedas,
               mostrados, version_prompt, modelo, vueltas, tokens_entrada, tokens_salida, tokens_cache, ms, error)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (uid, pregunta[:2000], traza.get("turnos_previos", 0), traza.get("via"), traza.get("desenlace"),
         (mensaje or "")[:4000], json.dumps(traza.get("busquedas", []), ensure_ascii=False),
         json.dumps([f["grupo"] for f in fichas]), traza.get("version_prompt"), traza.get("modelo"),
         traza.get("vueltas"), u.get("entrada"), u.get("salida"), u.get("cache"), traza.get("ms"),
         traza.get("error")))
    con.commit()
    return cur.lastrowid


def descarga(con, uid, iid, grupo):
    fila = con.execute("SELECT * FROM interacciones WHERE id = ? AND telegram_id = ?", (iid, uid)).fetchone()
    if not fila:
        return
    mostrados = json.loads(fila["mostrados"] or "[]")
    pos = mostrados.index(grupo) + 1 if grupo in mostrados else None
    seg = round((_ahora() - _ts(fila["ts"])).total_seconds())
    _senal(con, iid, uid, "descarga", pos, grupo)
    if not fila["descargas"]:
        con.execute("UPDATE interacciones SET descargas = 1, pos_primera_descarga = ?, seg_hasta_descarga = ? "
                    "WHERE id = ?", (pos, seg, iid))
    else:
        con.execute("UPDATE interacciones SET descargas = descargas + 1 WHERE id = ?", (iid,))
    con.commit()


def purgar(con, dias=REGISTRO_DIAS):
    limite = (_ahora() - timedelta(days=dias)).isoformat(sep=" ", timespec="seconds")
    n = con.execute("DELETE FROM interacciones WHERE ts < ?", (limite,)).rowcount
    con.execute("DELETE FROM senales WHERE ts < ?", (limite,))
    con.execute("DELETE FROM registro WHERE ts < ?", (limite,))
    con.commit()
    return n


# ---------------------------------------------------------------- informes
def _periodo(con, dias):
    desde = (_ahora() - timedelta(days=dias)).isoformat(sep=" ", timespec="seconds")
    return [dict(f) for f in con.execute("SELECT * FROM interacciones WHERE ts >= ? ORDER BY id", (desde,))]


def fallida(f):
    return bool(f["repregunta"] or f["reinicio"] or f["desenlace"] in ("sin_resultados", "cercanos")
                or f["via"] != "claude")


def resumen(con, dias=7):
    filas = _periodo(con, dias)
    if not filas:
        return f"Sin preguntas en los últimos {dias} días."
    n = len(filas)
    con_dossieres = [f for f in filas if json.loads(f["mostrados"] or "[]")]
    descargadas = [f for f in con_dossieres if f["descargas"]]
    posiciones = [f["pos_primera_descarga"] for f in descargadas if f["pos_primera_descarga"]]
    sin_reaccion = [f for f in con_dossieres if not (f["descargas"] or f["repregunta"] or f["reinicio"])]
    usuarios = len({f["telegram_id"] for f in filas})
    pct = lambda a, b: f"{100 * a / b:.0f} %" if b else "—"  # noqa: E731
    lineas = [
        f"Últimos {dias} días · {n} preguntas de {usuarios} personas",
        f"Con dossieres propuestos: {len(con_dossieres)}",
        f"  · con descarga: {len(descargadas)} ({pct(len(descargadas), len(con_dossieres))})"
        + (f", posición media {sum(posiciones) / len(posiciones):.1f}" if posiciones else ""),
        f"  · sin reacción: {len(sin_reaccion)}",
        f"Repreguntas: {sum(f['repregunta'] for f in filas)} · reinicios: {sum(f['reinicio'] for f in filas)}",
        f"Sin resultados: {sum(f['desenlace'] == 'sin_resultados' for f in filas)} · "
        f"solo cercanos: {sum(f['desenlace'] == 'cercanos' for f in filas)} · "
        f"preguntas de vuelta: {sum(f['desenlace'] == 'pregunta' for f in filas)}",
        f"Sin Claude (API caída): {sum(f['via'] != 'claude' for f in filas)}",
    ]
    ms = [f["ms"] for f in filas if f["ms"]]
    if ms:
        lineas.append(f"Tiempo medio de respuesta: {sum(ms) / len(ms) / 1000:.1f} s")
    tok = sum((f["tokens_entrada"] or 0) + (f["tokens_salida"] or 0) for f in filas)
    lineas.append(f"Tokens: {tok / 1000:.0f} k · versiones de prompt: "
                  + (", ".join(sorted({f['version_prompt'] for f in filas if f['version_prompt']})) or "—"))
    malas = [f for f in filas if fallida(f)][-5:]
    if malas:
        lineas.append("\nÚltimas a revisar:")
        lineas += [f"  #{f['id']} «{f['pregunta'][:70]}» → {motivo(f)}" for f in malas]
    return "\n".join(lineas)


def motivo(f):
    m = []
    if f["via"] != "claude":
        m.append("sin Claude")
    if f["desenlace"] in ("sin_resultados", "cercanos"):
        m.append(f["desenlace"].replace("_", " "))
    if f["repregunta"]:
        m.append("repregunta")
    if f["reinicio"]:
        m.append("reinicio")
    return ", ".join(m) or "—"


def casos(con, dias=7):
    filas = [f for f in _periodo(con, dias) if fallida(f)]
    for f in filas:
        print(f"\n#{f['id']} · {f['ts']} · usuario {f['telegram_id']} · {motivo(f)} · prompt {f['version_prompt']}")
        print(f"  Pregunta: {f['pregunta']}")
        for b in json.loads(f["busquedas"] or "[]"):
            print(f"  Buscó: {json.dumps(b.get('parametros'), ensure_ascii=False)} → {b.get('n')} resultados")
        print(f"  Respondió: {(f['mensaje'] or '')[:300]}")
        print(f"  Mostró: {f['mostrados']}")
    print(f"\n{len(filas)} casos con señales de fallo.")


def main():
    p = argparse.ArgumentParser(description="Uso y calidad del bot de dossieres")
    p.add_argument("comando", choices=["informe", "casos", "purgar"])
    p.add_argument("--dias", type=int, default=7)
    a = p.parse_args()
    con = db.conectar()
    if a.comando == "informe":
        print(resumen(con, a.dias))
    elif a.comando == "casos":
        casos(con, a.dias)
    else:
        print(f"{purgar(con)} respuestas anteriores a {REGISTRO_DIAS} días borradas.")


if __name__ == "__main__":
    main()

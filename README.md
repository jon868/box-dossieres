# Buscador de dossieres (bot de Telegram)

Bot de Telegram interno de alegria-activity para **consultar y descargar** los dossieres comerciales de Box.
Solo responde en chats privados a quien esté en la lista de permitidos, y saluda a cada uno por su nombre.

**Solo lee de Box**: no escribe, mueve ni borra nada.

## Cómo funciona
1. `indexar.py` recorre en Box:
   - `00.COMPARTIDO / 2. Dossieres y 3D / Dossieres PDF - Desde 2016 hasta 2023` (sin `RECOPILATORIOS`)
   - `00.COMPARTIDO / 2. Dossieres y 3D / Dossieres PDF - Desde 2024`

   Ignora los archivos de presupuesto. Cruza cada archivo con el *Listado de Dossieres y Renders (links).xlsx*,
   saca su texto, junta versiones e idiomas del mismo dossier (`_baja`, `_PRINT`, `_ENG`, `V2`…) y crea con
   Haiku (Batch API) una ficha por dossier: cliente, campaña, vehículos, formato, temáticas, público y resumen.
   Todo queda en `datos/dossieres.db` (SQLite), con un buscador de texto completo.
2. `bot.py` atiende Telegram: busca en ese índice, enseña los dossieres con un botón **📥** y envía el archivo.
   Si pesa más de 50 MB (límite de Telegram para bots) manda un enlace de descarga de Box que caduca en minutos.

## 1. Antes de empezar
- **Bot**: en Telegram, habla con **@BotFather** → `/newbot` → guarda el token.
- **Tu ID de Telegram**: escríbele a **@userinfobot** y te lo dice (es un número).
- **API key** de Anthropic propia (p. ej. `dossieres`) con límite de gasto de 10 $.
- Credenciales de la app de Box **claudebox** (las mismas que el conector).

## 2. Servicio en Easypanel
1. **+ Service → App**, nombre `box-dossieres`. **Source → GitHub**: este repo, rama `main`. **Build**: Dockerfile.
2. **Environment**: copia `.env.example` y rellena los valores (nunca los subas al repo).
3. **Mounts**: volumen `datos` en `/app/datos`, para que el índice y la lista de usuarios sobrevivan a los redespliegues.
4. No hace falta dominio: el bot habla con Telegram por *long polling*.
5. **Deploy**. Mientras no haya índice, el bot responde pero no encuentra nada.

## 3. Primer indexado (consola del servicio)
```bash
python indexar.py listar      # cuenta archivos por carpeta
python indexar.py excel       # cruza con el listado de dossieres
python indexar.py textos      # saca el texto (tarda: unos 1.100 archivos)
python indexar.py agrupar     # junta versiones e idiomas
python indexar.py enviar      # enseña el coste y pide confirmación
python indexar.py estado      # hasta que el lote esté "ended"
python indexar.py recoger     # guarda las fichas y rehace el buscador
python indexar.py resumen
python indexar.py buscar "salud mental airstream"
```
O todo seguido y en segundo plano:
```bash
nohup python indexar.py todo --si > /app/datos/indexado.log 2>&1 &   # con nohup, siempre --si
tail -f /app/datos/indexado.log
```
Si se corta, relánzalo: reanuda (no repite textos ni fichas ya hechas).

## 4. Mantenerlo al día
- Con `AUTO_INDEXAR=true` el bot lanza `indexar.py actualizar` cada noche a las `INDEXAR_HORA` (3:00 por defecto):
  solo procesa dossieres nuevos o modificados y te avisa por Telegram si falla.
- `python indexar.py enviar --forzar` rehace todas las fichas (p. ej. tras cambiar las instrucciones).

## 5. Gestionar el acceso (desde Telegram, como administrador)
- Si alguien sin permiso escribe al bot, le responde con su ID y **te avisa** con el comando ya preparado.
- `/permitir 123456789 Juncal` · da acceso; el bot la saludará como "Juncal".
- `/quitar 123456789` · quita el acceso.
- `/usuarios` · lista quién tiene acceso.

Los administradores son los IDs de `ADMIN_IDS`. Cada búsqueda y descarga queda en la tabla `registro`.

## Archivos
| Archivo | Qué hace |
|---|---|
| `box.py` | Autenticación con Box (cuenta de servicio), listado, descarga y texto extraído |
| `db.py` | Esquema SQLite, buscador FTS5 y consultas comunes |
| `indexar.py` | Indexador por pasos (listar, excel, textos, agrupar, enviar, recoger…) |
| `bot.py` | Bot de Telegram: permisos, saludo, búsqueda, descarga e indexado nocturno |

## Pendiente (siguiente fase)
- Que Claude interprete la pregunta y ordene los resultados ("algo parecido a lo de Iberdrola pero para colegios").
- Filtros por año, idioma o vehículo desde el propio chat.

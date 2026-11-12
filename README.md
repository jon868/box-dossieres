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
2. `bot.py` atiende Telegram. Con `asistente.py`, Claude interpreta la pregunta ("algo de salud para colegios
   en Airstream"), busca en el índice las veces que haga falta y propone los dossieres que encajan, explicando
   por qué, con un botón **📥** para descargarlos. Recuerda la conversación una hora ("¿y en inglés?",
   "el segundo"); `/nuevo` empieza de cero. Si la API de Claude falla, el bot hace una búsqueda directa.
   Si pesa más de 50 MB (límite de Telegram para bots) manda un enlace de descarga de Box que caduca en minutos.

## Catálogos, magazine y presentación corporativa
Además de los dossieres de propuesta, el bot ofrece el material comercial genérico de
`00.COMPARTIDO / 1. Material Comercial`: catálogos por sector, el magazine, la presentación corporativa, Set TV,
fichas de vehículos y el portfolio (este último solo si lo piden por su nombre).
- Si no está claro si piden un dossier o un catálogo, el bot lo pregunta.
- Por defecto da la última edición en el idioma pedido y en `baja` (para correo), y ofrece las anteriores y los
  otros formatos (`pantalla`, `pliegos` para imprenta, `alta`).

**Se mantiene solo.** El catálogo vive solo en `datos/catalogos.json` (el volumen), nunca en el repo: es público y no
debe llevar nombres de archivos ni ids de Box. Cada noche (a `INDEXAR_HORA`, con `AUTO_CATALOGOS=true`) el bot
recorre Material Comercial:
1. descarta por regla planos, certificados, PDF de menos de 1 MB y copias exactas de archivos que ya tiene;
2. a cada PDF nuevo que quede, Claude (Haiku) le lee nombre, ruta y principio del texto y decide si es una edición,
   idioma o formato nuevo de un catálogo, un catálogo nuevo o algo que no es catálogo;
3. lo añade y avisa a los administradores por Telegram, uno a uno, con un botón **↩️ Deshacer**;
4. quita lo que se haya borrado de Box.

Comandos (administradores): `/catalogos` lanza la revisión en el momento, `/catalogos lista` enseña el resumen y
`/catalogos exportar` manda el `catalogos.json` actual. Para cambiarlo a mano, edítalo y **mándaselo al bot** como
archivo llamado `catalogos.json`: lo valida, guarda copia del anterior y lo sustituye.
En la consola: `python catalogos.py listar | actualizar | comprobar | deshacer <id> | importar <ruta>`.

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
- `/informe [días]` · uso y calidad de las respuestas.

Los administradores son los IDs de `ADMIN_IDS`. Cada búsqueda y descarga queda en la tabla `registro`.

## Archivos
| Archivo | Qué hace |
|---|---|
| `box.py` | Autenticación con Box (cuenta de servicio), listado, descarga y texto extraído |
| `db.py` | Esquema SQLite, buscador FTS5 y consultas comunes |
| `indexar.py` | Indexador por pasos (listar, excel, textos, agrupar, enviar, recoger…) |
| `bot.py` | Bot de Telegram: permisos, saludo, búsqueda, descarga e indexado nocturno |
| `asistente.py` | Claude en el bot: system prompt, herramientas de búsqueda y memoria de la conversación |
| `uso.py` | Registro de uso, señales implícitas e informes |
| `catalogos.py` | Catálogos, magazine y presentación: búsqueda, revisión automática e importación (datos en `datos/catalogos.json`) |
| `servidor.py` | Publica `indice_dossieres.json` (con contraseña) para el conector de Box |

## Buscar dossieres desde Claude Team (conector Box_alegria)
Además del bot, el índice se puede usar desde Claude con la herramienta `buscar_dossieres` del conector de Box
y la skill `buscar-dossieres`.
1. Al recoger las fichas (y al agrupar) se genera `datos/indice_dossieres.json` (fichas y archivos, sin el texto de
   los PDF). Para rehacerlo a mano: `python indexar.py exportar`.
2. Con `INDICE_PASSWORD` definida, el servicio lo publica en el puerto 8080:
   `https://<dominio>/indice_dossieres.json`, con usuario y contraseña (`INDICE_USUARIO` / `INDICE_PASSWORD`).
   En Easypanel: **Domains** → asigna un dominio con HTTPS al puerto `8080`.
3. En el servicio del conector (`box-mcp`) añade:
   ```
   INDICE_DOSSIERES_URL=https://<dominio>/indice_dossieres.json
   INDICE_DOSSIERES_USUARIO=alegria
   INDICE_DOSSIERES_PASSWORD=<la misma contraseña>
   ```
   El conector lo recarga cada `INDICE_REFRESCO_MIN` minutos (60 por defecto), como el índice de fotos.
   Comprobación: `/health/box` del conector muestra `indice_dossieres` con el número de dossieres.

## Registro de uso y calidad
Cada respuesta del bot queda registrada con su contexto (pregunta, búsquedas que hizo Claude, dossieres
mostrados, versión del prompt, tokens y tiempo) y se le suman **señales implícitas**, sin preguntar nada:
- **descarga** de un dossier de esa respuesta (y en qué posición estaba) → buena señal;
- **repregunta** a los pocos minutos sin haber descargado → probable fallo (no cuenta si el bot había preguntado);
- **/nuevo** poco después de una respuesta sin descarga → probable fallo.

Para verlo:
- En Telegram (administradores): `/informe` o `/informe 30` (días).
- En la consola: `python uso.py informe --dias 30` y `python uso.py casos --dias 30` (detalle de las respuestas
  con señales de fallo: qué buscó Claude, qué encontró y qué respondió; es lo que sirve para afinar el prompt).

La versión del prompt se calcula sola a partir de `SYSTEM_BASE` y las herramientas, así que al cambiarlo se puede
comparar antes y después. El registro se borra pasados `REGISTRO_DIAS` (365 por defecto). Al dar acceso, el bot
avisa de que las búsquedas se guardan.

## Ajustar el comportamiento del asistente
El system prompt está en `asistente.py` (constante `SYSTEM`); las herramientas que puede usar, justo debajo.
Cada pregunta cuesta muy poco con Haiku (el gasto real se ve en la Console); se puede cambiar de modelo con `BOT_MODELO`.

FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY box.py db.py indexar.py bot.py asistente.py uso.py servidor.py catalogos.py ./

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/datos \
    PORT=8080

# Solo si INDICE_PASSWORD está definida: índice para el conector de Box
EXPOSE 8080

# El contenedor deja el bot en marcha. El indexado se lanza a mano desde la consola
# del servicio en Easypanel o cada noche si AUTO_INDEXAR=true.
CMD ["python", "bot.py"]

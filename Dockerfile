FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY box.py db.py indexar.py bot.py ./

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/datos

# El contenedor deja el bot en marcha. El indexado se lanza a mano desde la consola
# del servicio en Easypanel o cada noche si AUTO_INDEXAR=true.
CMD ["python", "bot.py"]

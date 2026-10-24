# Pont Opal Box — transcription locale + voix Edge, parle à l'API d'Opal.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPAL_MODELS_DIR=/models

RUN useradd --create-home --uid 1000 opal && mkdir -p /models && chown opal /models

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY *.py ./

USER opal
EXPOSE 8766
CMD ["python", "opal_bridge.py"]

# Pont Opal Box — transcription locale, voix Edge, caméra (OpenCV), vision Claude Code.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPAL_MODELS_DIR=/models

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 1000 opal && mkdir -p /models && chown opal /models

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
# Détecteur de visage YuNet (opencv_zoo), empreinte vérifiée
RUN curl -fsSL -o face_detection_yunet.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
 && echo "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4  face_detection_yunet.onnx" \
      | sha256sum -c -

USER opal
# Claude Code décrit les photos avec l'abonnement Claude (CLAUDE_CODE_OAUTH_TOKEN).
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/home/opal/.local/bin:$PATH \
    DISABLE_AUTOUPDATER=1

COPY *.py ./
EXPOSE 8766 8767
CMD ["python", "opal_bridge.py"]

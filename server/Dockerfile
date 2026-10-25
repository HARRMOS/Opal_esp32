# Pont Opal Box — transcription locale, voix Edge, caméra (OpenCV), vision Claude Code.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    OPAL_MODELS_DIR=/models

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates fonts-dejavu-core \
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
# Reconnaissance des visages SFace (opencv_zoo), empreinte vérifiée
RUN curl -fsSL -o face_recognition_sface.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx \
 && echo "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79  face_recognition_sface.onnx" \
      | sha256sum -c -
# Gestes : détection de la paume + points de la main (MediaPipe, opencv_zoo), empreintes vérifiées
RUN curl -fsSL -o palm_detection_mediapipe.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/palm_detection_mediapipe/palm_detection_mediapipe_2023feb.onnx \
 && curl -fsSL -o handpose_estimation_mediapipe.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/handpose_estimation_mediapipe/handpose_estimation_mediapipe_2023feb.onnx \
 && printf "%s  %s\n%s  %s\n" \
      78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c palm_detection_mediapipe.onnx \
      db0898ae717b76b075d9bf563af315b29562e11f8df5027a1ef07b02bef6d81c handpose_estimation_mediapipe.onnx \
      | sha256sum -c -
# Bras croisés : détection de la personne + points du corps (MediaPipe, opencv_zoo), empreintes vérifiées
RUN curl -fsSL -o person_detection_mediapipe.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/person_detection_mediapipe/person_detection_mediapipe_2023mar.onnx \
 && curl -fsSL -o pose_estimation_mediapipe.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/pose_estimation_mediapipe/pose_estimation_mediapipe_2023mar.onnx \
 && printf "%s  %s\n%s  %s\n" \
      47fd5599d6fa17608f03e0eb0ae230baa6e597d7e8a2c8199fe00abea55a701f person_detection_mediapipe.onnx \
      9d89c599319a18fb7d2e28451a883476164543182bafca5f09eb2cf767ed2f3f pose_estimation_mediapipe.onnx \
      | sha256sum -c -
# Expression du visage (MobileFaceNet, opencv_zoo), empreinte vérifiée
RUN curl -fsSL -o facial_expression_recognition.onnx \
      https://github.com/opencv/opencv_zoo/raw/main/models/facial_expression_recognition/facial_expression_recognition_mobilefacenet_2022july.onnx \
 && echo "4f61307602fc089ce20488a31d4e4614e3c9753a7d6c41578c854858b183e1a9  facial_expression_recognition.onnx" \
      | sha256sum -c -

USER opal
# Claude Code décrit les photos avec l'abonnement Claude (CLAUDE_CODE_OAUTH_TOKEN).
RUN curl -fsSL https://claude.ai/install.sh | bash
ENV PATH=/home/opal/.local/bin:$PATH \
    DISABLE_AUTOUPDATER=1

COPY *.py ./
COPY vendor ./vendor
EXPOSE 8766 8767
CMD ["python", "opal_bridge.py"]

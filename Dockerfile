# EdgeMind: one image for every role (sync gateway, hub/dashboard, edge node).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    EDGEMIND_DATA=/data EDGEMIND_MODELS=/models

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# Bake the AI models into the image (multilingual text + CLIP photo models, ~700 MB):
# an edge node must work with no internet from its very first boot.
COPY app/__init__.py app/config.py app/embeddings.py app/
ARG PRELOAD_MODELS=1
RUN if [ "$PRELOAD_MODELS" = "1" ]; then python -c "\
from app import embeddings, config; \
embeddings.get().embed_dense('warm'); \
v = embeddings.vision(); v.embed_text('warm'); v._load('_img', 'ImageEmbedding', config.CLIP_VISION); \
print('models ready:', embeddings.get().name)"; fi

COPY app app
COPY static static
COPY launch.py .

VOLUME /data
EXPOSE 8000 8100 8001
CMD ["python", "launch.py", "--no-browser"]

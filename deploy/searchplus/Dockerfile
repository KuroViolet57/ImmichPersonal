# Search+ model server: PyTorch + OpenCLIP, running immich_organizer/embed_service.py (mounted at /app).
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN pip install torch==2.14.1 torchvision --index-url https://download.pytorch.org/whl/cu130 \
 && pip install open_clip_torch==3.3.0 timm==1.0.30 safetensors huggingface_hub pillow numpy
WORKDIR /app
ENTRYPOINT ["python", "/app/embed_service.py"]

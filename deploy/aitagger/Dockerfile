# AI Tagger model server: WD EVA02-Large Tagger v3 (ONNX Runtime, CUDA) + PixAI Tagger v1.0 (PyTorch, bf16),
# running immich_organizer/tagger_service.py (mounted at /app).
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

# CUDA 13 wheels, like the Search+ image.
RUN pip install torch==2.14.1 torchvision==0.29.1 --index-url https://download.pytorch.org/whl/cu130

# onnxruntime-gpu's "cuda"/"cudnn" extras pull the CUDA 13 runtime libraries it needs (they are the same nvidia-*
# wheels torch uses, so nothing is installed twice). onnx + onnxconverter-common convert the WD model to fp16 on
# the first start.
RUN pip install "onnxruntime-gpu[cuda,cudnn]==1.30.0" onnx==1.23.1 onnxconverter-common==1.16.0 \
    pillow numpy

# PixAI Tagger v1.0 ships its own model code (tagger_pipeline.py in the Hugging Face repo, loaded with
# trust_remote_code=True from the one pinned revision in tagger_service.py). That code imports torch, torchvision,
# timm and transformers; it was exported with transformers 4.57.6, which needs huggingface_hub < 1.0.
RUN pip install transformers==4.57.6 huggingface_hub==0.36.2 timm==1.0.30

ENV HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app
ENTRYPOINT ["python", "/app/tagger_service.py"]

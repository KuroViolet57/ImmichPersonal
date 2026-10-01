# AI Tagger model server: WD EVA02-Large Tagger v3 (ONNX Runtime, CUDA) + RAM++ (PyTorch),
# running immich_organizer/tagger_service.py (mounted at /app).
FROM python:3.12-slim
ENV PIP_NO_CACHE_DIR=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1

# CUDA 13 wheels, like the Search+ image.
RUN pip install torch==2.14.1 torchvision --index-url https://download.pytorch.org/whl/cu130

# onnxruntime-gpu's "cuda"/"cudnn" extras pull the CUDA 13 runtime libraries it needs (they are the same nvidia-*
# wheels torch uses, so nothing is installed twice). onnx + onnxconverter-common convert the WD model to fp16 on
# the first start. scipy and timm are only imported by the RAM++ Swin backbone code fetched below.
RUN pip install "onnxruntime-gpu[cuda,cudnn]==1.30.0" onnx==1.23.1 onnxconverter-common==1.16.0 timm==1.0.30 \
    scipy huggingface_hub pillow numpy

# RAM++ (Recognize Anything, Apache-2.0; the Swin backbone inside it is MIT). The official `ram` package pins
# timm==0.4.12, fairscale==0.4.4 and an old transformers that no longer imports, so it is NOT installed. Instead
# the build fetches one pinned commit and keeps only what inference needs: the Swin-L backbone source, the tag list
# with the per-class thresholds, and the licence texts. tagger_service.py implements the (small) tagging head itself.
ARG RAM_COMMIT=7cb804a8609e9f4b1a50b7f31436d2df40bb9481
RUN python - <<'PY'
import io, os, pathlib, tarfile, urllib.request
commit = os.environ["RAM_COMMIT"]
url = f"https://github.com/xinyu1205/recognize-anything/archive/{commit}.tar.gz"
want = {
    "ram/models/swin_transformer.py": "swin_transformer.py",
    "ram/data/ram_tag_list.txt": "ram_tag_list.txt",
    "ram/data/ram_tag_list_threshold.txt": "ram_tag_list_threshold.txt",
    "LICENSE": "LICENSE",
    "NOTICE.txt": "NOTICE.txt",
}
out = pathlib.Path("/opt/ram")
out.mkdir(parents=True)
with urllib.request.urlopen(url) as resp:
    tar = tarfile.open(fileobj=io.BytesIO(resp.read()), mode="r:gz")
for m in tar.getmembers():
    rel = m.name.split("/", 1)[-1]
    if rel in want and m.isfile():
        (out / want[rel]).write_bytes(tar.extractfile(m).read())
missing = [v for v in want.values() if not (out / v).exists()]
assert not missing, f"missing from the RAM++ source archive: {missing}"
(out / "COMMIT").write_text(commit + "\n")
PY

ENV RAM_CODE=/opt/ram HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app
ENTRYPOINT ["python", "/app/tagger_service.py"]

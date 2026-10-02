# AI Tagger model server: WD EVA02-Large Tagger v3 (ONNX Runtime, CUDA) + PixAI Tagger v1.0 and RAM++ (PyTorch, fp16),
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
# scipy is only imported at the top of the RAM++ Swin backbone source fetched below (the one function that uses it
# is not on the inference path).
RUN pip install transformers==4.57.6 huggingface_hub==0.36.2 timm==1.0.30 scipy==1.18.1

# RAM++ (Recognize Anything, Apache-2.0; the Swin backbone inside it is MIT). The official `ram` package pins
# timm==0.4.12, fairscale==0.4.4 and an old transformers that no longer imports, so it is NOT installed. Instead the
# build fetches one pinned commit and keeps only what inference needs, each file checked against its SHA-256: the
# Swin-L backbone source, the tag list with the per-tag thresholds, and the licence texts. tagger_service.py
# implements the (small) tagging head itself and checks the weights against it when it loads them.
ARG RAM_COMMIT=7cb804a8609e9f4b1a50b7f31436d2df40bb9481
RUN python - <<'PY'
import hashlib, io, os, pathlib, tarfile, urllib.request
commit = os.environ["RAM_COMMIT"]
url = f"https://github.com/xinyu1205/recognize-anything/archive/{commit}.tar.gz"
want = {   # path in the repository -> (name here, sha256)
    "ram/models/swin_transformer.py": ("swin_transformer.py", "6ad728f9eb9276b292cbdabe6f3f77bfea3c8603b8b672ec02331c9d467c386c"),
    "ram/data/ram_tag_list.txt": ("ram_tag_list.txt", "5d96d2f7225527480ad773aaac76b6e5c08a18adf6eae9ca7d2ab923d5e7bd8b"),
    "ram/data/ram_tag_list_threshold.txt": ("ram_tag_list_threshold.txt", "b6f81d0de1bc7f9c251af512c413eae976c77dbfbadf700d4e3d90510f4c6447"),
    "LICENSE": ("LICENSE", "d38f4edcfb09332fa497fd3ce52bd6c13dd73fa5acb8418a47aece5a2d311f26"),
    "NOTICE.txt": ("NOTICE.txt", "ddb1bf4cb85cefefacc718b470a7b6cef644a45aba338219b03ee0bbe0c60537"),
}
out = pathlib.Path("/opt/ram")
out.mkdir(parents=True)
with urllib.request.urlopen(url) as resp:
    tar = tarfile.open(fileobj=io.BytesIO(resp.read()), mode="r:gz")
for m in tar.getmembers():
    rel = m.name.split("/", 1)[-1]
    if rel in want and m.isfile():
        name, digest = want[rel]
        data = tar.extractfile(m).read()
        assert hashlib.sha256(data).hexdigest() == digest, f"{rel} at {commit} does not match its SHA-256"
        (out / name).write_bytes(data)
missing = [v[0] for v in want.values() if not (out / v[0]).exists()]
assert not missing, f"missing from the RAM++ source archive: {missing}"
(out / "COMMIT").write_text(commit + "\n")
PY

ENV RAM_CODE=/opt/ram HF_HUB_DISABLE_TELEMETRY=1
WORKDIR /app
ENTRYPOINT ["python", "/app/tagger_service.py"]

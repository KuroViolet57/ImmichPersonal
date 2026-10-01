"""AI Tagger model server: tags pictures with two image taggers on the GPU.

Runs inside its own GPU container (see deploy/aitagger). The panel sends it pictures (and video frames) while it
indexes the library; everything else (settings, aggregation, the language model, writing descriptions) happens in
the panel.

    GET  /health   -> {"status": "loading" | "ok" | "error", "error": null, "device": "cuda", "vramCapGb": 5,
                       "batch": 16, "models": [{"name", "kind", "tags", "precision", "loadedIn"}, ...],
                       "idleExitMinutes": 20, "idleSeconds": 12, "busy": 0}
    POST /tag      {"images": [<base64 jpeg/png>, ...], "floor": 0.05}      (at most 64 images)
                   -> {"results": [{"wd": {"general": {tag: score}, "character": {tag: score},
                                           "rating": {"general": p, "sensitive": p, "questionable": p, "explicit": p}},
                                    "ram": {tag: score}} | null, ...],
                       "errors": [null | "why", ...], "tookMs": 412}

The two models:
  * WD EVA02-Large Tagger v3 (SmilingWolf, Apache-2.0): 10,861 Danbooru tags, run with onnxruntime. Input is
    448x448, NHWC, BGR, float32 0-255, the picture padded to a white square (the layout of the reference
    implementation at huggingface.co/spaces/SmilingWolf/wd-tagger).
  * RAM++ (Recognize Anything Plus, xinyu1205, Apache-2.0): 4,585 plain-English tags, run with PyTorch fp16.
    The tagging code below is adapted from https://github.com/xinyu1205/recognize-anything (ram/models/ram_plus.py,
    Apache-2.0, (c) the authors). Only the Swin-L backbone source is taken from that repository, at build time
    (see deploy/aitagger/Dockerfile); the official package is not installed because its pins no longer resolve.

Scores are calibrated so that 0.5 is each model's own recommended threshold for that tag:
s' = sigmoid(logit(s) - logit(t)), with t = 0.35 for WD general tags, 0.75 for WD character tags and RAM++'s
shipped per-class threshold for RAM++ tags. Only tags with s' >= floor are returned. The WD rating is the model's
raw probability for each rating. Tag names are model-native (WD keeps its underscores).

Optional extra request field: "models": ["wd"] or ["ram"] runs only that model (default: both).

The server answers /health at once while the models load in the background (503 on /tag until they are ready).
One GPU thread runs everything, fed by a queue, so concurrent requests never fight over the GPU. A picture that
cannot be read, or that fails on the GPU, fails only its own slot. A CUDA out-of-memory error splits the batch
and retries (and lowers the micro-batch for the rest of the session); a picture that still does not fit comes back
as an error containing "out of memory".

Environment: AITAGGER_VRAM_GB (memory cap for this process, default 5), AITAGGER_BATCH (GPU micro-batch,
default 16), IDLE_EXIT_MINUTES (default 20; the server exits after that long without a request, never while one is
in flight, which stops the container and frees the GPU), WD_MODEL / RAM_MODEL (Hugging Face repos or local
directories), AITAGGER_CACHE (model files, default /cache), AITAGGER_DEVICE (default cuda; cpu is for tests).
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import math
import os
import queue
import re
import signal
import sys
import threading
import time
import traceback
import warnings
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile, ImageOps

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

CACHE = Path(os.environ.get("AITAGGER_CACHE", "/cache"))
DEVICE = os.environ.get("AITAGGER_DEVICE", "cuda")
VRAM_GB = float(os.environ.get("AITAGGER_VRAM_GB", "5"))
MAX_BATCH = max(1, int(os.environ.get("AITAGGER_BATCH", "16")))
IDLE_EXIT_MINUTES = float(os.environ.get("IDLE_EXIT_MINUTES", "20"))
WD_MODEL = os.environ.get("WD_MODEL", "SmilingWolf/wd-eva02-large-tagger-v3")
RAM_MODEL = os.environ.get("RAM_MODEL", "xinyu1205/recognize-anything-plus-model")
RAM_FILE = os.environ.get("RAM_FILE", "ram_plus_swin_large_14m.pth")
RAM_CODE = Path(os.environ.get("RAM_CODE", "/opt/ram"))      # swin_transformer.py + tag list, fetched at image build
WD_PRECISION = os.environ.get("WD_PRECISION", "fp16")        # fp16: the ONNX model is converted once (cached)
CONTEXT_GB = 0.5                 # CUDA context and library workspaces: not counted by either allocator
ORT_SHARE = 0.5                  # share of the rest of the VRAM cap given to onnxruntime (WD); the rest is torch's
WD_BATCH = 8                     # onnxruntime gains nothing from bigger WD batches (measured on an RTX 4090)

MAX_IMAGES = 64
MAX_BODY = 512 * 1024 * 1024
WD_GENERAL_T, WD_CHARACTER_T = 0.35, 0.75    # the models' recommended thresholds (wd-tagger reference)
WD_CATEGORY_GENERAL, WD_CATEGORY_CHARACTER, WD_CATEGORY_RATING = 0, 4, 9
RAM_SIZE = 384
RAM_CLASSES, RAM_DESCRIPTIONS, RAM_WIDTH = 4585, 51, 512
RAM_MEAN, RAM_STD = (0.485, 0.456, 0.406), (0.229, 0.224, 0.225)


def _cuda() -> bool:
    return DEVICE.startswith("cuda")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _logit(p):
    p = np.clip(np.asarray(p, dtype=np.float64), 1e-6, 1 - 1e-6)
    return np.log(p / (1 - p))


def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30.0, 30.0)))


def _is_oom(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(s in text for s in ("out of memory", "failed to allocate", "smaller than requested",
                                    "memory allocation", "bad_alloc", "cuda failure 2"))


def _fetch(source: str, filename: str) -> Path:
    """A model file from a local directory, else from the Hugging Face cache under CACHE (downloaded once)."""
    local = Path(source)
    if local.is_dir():
        return local / filename
    from huggingface_hub import hf_hub_download
    cache_dir = str(CACHE / "hub")
    try:
        return Path(hf_hub_download(source, filename, cache_dir=cache_dir, local_files_only=True))
    except Exception:  # noqa: BLE001 - not downloaded yet
        _log(f"downloading {source}/{filename} (first start only)")
        return Path(hf_hub_download(source, filename, cache_dir=cache_dir))


def _wd_file(fp32: Path):
    """(model path, precision). On the GPU the ONNX model is converted to fp16 once and kept in the cache:
    twice as fast and half the memory, with tag scores within ~0.01 of fp32 (checked on real pictures). If the
    conversion is unavailable or fails, the original fp32 model is used."""
    if WD_PRECISION != "fp16" or not _cuda():
        return fp32, "fp32"
    out = CACHE / f"{Path(WD_MODEL).name}-fp16.onnx"
    if out.exists():
        return out, "fp16"
    try:
        import onnx
        from onnxconverter_common import float16
        _log("converting the WD model to fp16 (first start only)")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")             # "float32 number 1e-8 will be truncated" for tiny constants
            model = float16.convert_float_to_float16(onnx.load(str(fp32)), keep_io_types=True)
        tmp = out.with_suffix(".tmp")
        onnx.save(model, str(tmp))
        tmp.replace(out)
        return out, "fp16"
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        _log("fp16 conversion failed; using the fp32 WD model")
        return fp32, "fp32"


# --------------------------------------------------------------------------- RAM++ (adapted from the official repo)
_RAM_CLASS = None


def _ram_class():
    """The RAM++ inference network, built on first use (torch is imported lazily so this file imports without it)."""
    global _RAM_CLASS
    if _RAM_CLASS is not None:
        return _RAM_CLASS
    import importlib.util

    import torch
    import torch.nn.functional as F
    from torch import nn

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # timm.models.layers is a deprecated alias in timm 1.x
        spec = importlib.util.spec_from_file_location("ram_swin_transformer", RAM_CODE / "swin_transformer.py")
        swin = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = swin
        spec.loader.exec_module(swin)

    class CrossLayer(nn.Module):
        """One layer of RAM++'s tagging head: cross-attention (tags attend to the picture) + feed-forward.
        The BERT self-attention is removed in the released model ("tagging" mode), so only these weights exist."""

        def __init__(self, hidden=768, enc=RAM_WIDTH, heads=4, inner=3072):
            super().__init__()
            self.heads = heads
            self.q, self.k, self.v = nn.Linear(hidden, hidden), nn.Linear(enc, hidden), nn.Linear(enc, hidden)
            self.o, self.ln1 = nn.Linear(hidden, hidden), nn.LayerNorm(hidden, eps=1e-12)
            self.fc1, self.fc2 = nn.Linear(hidden, inner), nn.Linear(inner, hidden)
            self.ln2 = nn.LayerNorm(hidden, eps=1e-12)

        def forward(self, x, picture):
            b, n, _ = x.shape
            m = picture.shape[1]
            h = self.heads
            q = self.q(x).view(b, n, h, -1).transpose(1, 2)
            k = self.k(picture).view(b, m, h, -1).transpose(1, 2)
            v = self.v(picture).view(b, m, h, -1).transpose(1, 2)
            a = F.scaled_dot_product_attention(q, k, v).transpose(1, 2).reshape(b, n, -1)
            x = self.ln1(self.o(a) + x)
            return self.ln2(self.fc2(F.gelu(self.fc1(x))) + x)

    class RamPlus(nn.Module):
        def __init__(self):
            super().__init__()
            self.visual_encoder = swin.SwinTransformer(
                img_size=RAM_SIZE, patch_size=4, in_chans=3, embed_dim=192, depths=[2, 2, 18, 2],
                num_heads=[6, 12, 24, 48], window_size=12, mlp_ratio=4.0, qkv_bias=True, drop_rate=0.0,
                drop_path_rate=0.0, ape=False, patch_norm=True, use_checkpoint=False)
            self.image_proj = nn.Linear(1536, RAM_WIDTH)
            self.label_embed = nn.Parameter(torch.zeros(RAM_CLASSES * RAM_DESCRIPTIONS, RAM_WIDTH))
            self.reweight_scale = nn.Parameter(torch.ones(()) * math.log(1 / 0.07))   # not in the checkpoint
            self.wordvec_proj = nn.Linear(RAM_WIDTH, 768)
            self.layers = nn.ModuleList([CrossLayer(), CrossLayer()])
            self.fc = nn.Linear(768, 1)

        def forward(self, x):
            """x: normalised (B, 3, 384, 384). Returns the raw tag logits (B, 4585) as float32."""
            feats = self.image_proj(self.visual_encoder(x))                      # (B, 145, 512): class token + 144
            cls = F.normalize(feats[:, 0, :].float(), dim=-1)
            sim = (cls.to(self.label_embed.dtype) @ self.label_embed.t()).float() * self.reweight_scale.float().exp()
            weights = F.softmax(sim.view(x.shape[0], RAM_CLASSES, RAM_DESCRIPTIONS), dim=2)
            labels = self.label_embed.view(RAM_CLASSES, RAM_DESCRIPTIONS, RAM_WIDTH)
            labels = torch.einsum("bcd,cdk->bck", weights.to(labels.dtype), labels)   # per-picture tag queries
            h = F.relu(self.wordvec_proj(labels))
            for layer in self.layers:
                h = layer(h, feats)
            return self.fc(h).squeeze(-1).float()

    _RAM_CLASS = RamPlus
    return RamPlus


_QKV = {"query": "q", "key": "k", "value": "v"}
_RAM_KEY_MAP = [
    (re.compile(r"^tagging_head\.encoder\.layer\.(\d+)\.crossattention\.self\.(query|key|value)\.(weight|bias)$"),
     lambda m: f"layers.{m[1]}.{_QKV[m[2]]}.{m[3]}"),
    (re.compile(r"^tagging_head\.encoder\.layer\.(\d+)\.crossattention\.output\.dense\.(weight|bias)$"),
     lambda m: f"layers.{m[1]}.o.{m[2]}"),
    (re.compile(r"^tagging_head\.encoder\.layer\.(\d+)\.crossattention\.output\.LayerNorm\.(weight|bias)$"),
     lambda m: f"layers.{m[1]}.ln1.{m[2]}"),
    (re.compile(r"^tagging_head\.encoder\.layer\.(\d+)\.intermediate\.dense\.(weight|bias)$"),
     lambda m: f"layers.{m[1]}.fc1.{m[2]}"),
    (re.compile(r"^tagging_head\.encoder\.layer\.(\d+)\.output\.dense\.(weight|bias)$"),
     lambda m: f"layers.{m[1]}.fc2.{m[2]}"),
    (re.compile(r"^tagging_head\.encoder\.layer\.(\d+)\.output\.LayerNorm\.(weight|bias)$"),
     lambda m: f"layers.{m[1]}.ln2.{m[2]}"),
]


def _ram_state(checkpoint: dict) -> dict:
    """Map the official checkpoint's names onto RamPlus. Self-attention, embeddings and the Swin buffers that are
    rebuilt at construction time (relative_position_index, attn_mask) are dropped, as the official loader does."""
    state = checkpoint.get("model", checkpoint)
    out = {}
    for key, value in state.items():
        key = key.replace("vision_multi", "tagging_head")
        if "relative_position_index" in key or "attn_mask" in key:
            continue
        if key.startswith("tagging_head."):
            for pattern, to in _RAM_KEY_MAP:
                m = pattern.match(key)
                if m:
                    out[to(m)] = value
                    break
            continue                                    # tagging_head.embeddings / .attention are unused
        if key.startswith(("visual_encoder.", "image_proj.", "wordvec_proj.", "fc.")) or key in ("label_embed",
                                                                                                    "reweight_scale"):
            out[key] = value
    return out


# --------------------------------------------------------------------------- the engine
class Job:
    __slots__ = ("inputs", "errors", "models", "floor")

    def __init__(self, inputs, errors, models, floor):
        self.inputs, self.errors, self.models, self.floor = inputs, errors, models, floor


class Tagger:
    """Both models, loaded in the background; every GPU call runs on one thread."""

    def __init__(self):
        self.status, self.error = "loading", ""
        self.models: list[dict] = []
        self.queue: queue.Queue = queue.Queue()
        self.last_used = time.monotonic()
        self.busy = 0
        self._lock = threading.Lock()
        self.batch = {"wd": min(MAX_BATCH, WD_BATCH), "ram": MAX_BATCH}     # lowered by CUDA out-of-memory answers
        self.wd = self.ram = None
        threading.Thread(target=self._load_then_serve, daemon=True).start()

    # ---- bookkeeping
    def touch(self) -> None:
        self.last_used = time.monotonic()

    def enter(self) -> None:
        with self._lock:
            self.busy += 1
        self.touch()

    def leave(self) -> None:
        with self._lock:
            self.busy -= 1
        self.touch()

    # ---- loading
    def _vram_split(self):
        """(onnxruntime arena bytes, torch fraction of the whole GPU) from the AITAGGER_VRAM_GB cap."""
        import torch
        total = torch.cuda.get_device_properties(0).total_memory
        usable = max(1.0, VRAM_GB - CONTEXT_GB) * 1024 ** 3
        return int(usable * ORT_SHARE), min(1.0, usable * (1 - ORT_SHARE) / total)

    def _load_wd(self, ort_limit: int) -> dict:
        started = time.monotonic()
        if _cuda():
            import torch  # noqa: F401 - loads the CUDA 13 libraries that onnxruntime then finds
        import onnxruntime as ort
        if _cuda() and hasattr(ort, "preload_dlls"):
            try:
                ort.preload_dlls()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
        model_path, precision = _wd_file(_fetch(WD_MODEL, "model.onnx"))
        with open(_fetch(WD_MODEL, "selected_tags.csv"), newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        names = [r["name"] for r in rows]
        cats = np.array([int(r["category"]) for r in rows])
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        opts.enable_mem_pattern = False             # batch sizes vary; the memory-pattern planner would re-plan
        if _cuda():
            providers = [("CUDAExecutionProvider", {"device_id": 0, "gpu_mem_limit": ort_limit,
                                                    "arena_extend_strategy": "kSameAsRequested",
                                                    "cudnn_conv_algo_search": "HEURISTIC"}),
                         "CPUExecutionProvider"]
        else:
            providers = ["CPUExecutionProvider"]
        sess = ort.InferenceSession(str(model_path), opts, providers=providers)
        if _cuda() and "CUDAExecutionProvider" not in sess.get_providers():
            raise RuntimeError(f"onnxruntime could not start its CUDA provider (using {sess.get_providers()})")
        inp, out = sess.get_inputs()[0], sess.get_outputs()[0]
        shape = inp.shape
        if len(shape) != 4 or shape[3] != 3 or not isinstance(shape[1], int):
            raise RuntimeError(f"unexpected WD input {inp.name} {shape}; expected NHWC with 3 channels")
        thresholds = np.full(len(names), 0.5)
        thresholds[cats == WD_CATEGORY_GENERAL] = WD_GENERAL_T
        thresholds[cats == WD_CATEGORY_CHARACTER] = WD_CHARACTER_T
        self.wd = {
            "sess": sess, "input": inp.name, "output": out.name, "size": int(shape[1]),
            "names": np.array(names, dtype=object), "general": np.flatnonzero(cats == WD_CATEGORY_GENERAL),
            "character": np.flatnonzero(cats == WD_CATEGORY_CHARACTER),
            "rating": np.flatnonzero(cats == WD_CATEGORY_RATING), "logit_t": _logit(thresholds),
        }
        self._forward_wd([np.full((self.wd["size"], self.wd["size"], 3), 255, np.uint8)], 0.5)    # warm-up
        return {"name": Path(WD_MODEL).name, "kind": "wd", "tags": len(names), "precision": precision,
                "loadedIn": round(time.monotonic() - started, 1)}

    def _load_ram(self, torch_fraction: float) -> dict:
        import torch

        started = time.monotonic()
        tags = (RAM_CODE / "ram_tag_list.txt").read_text(encoding="utf-8").splitlines()
        thresholds = np.array([float(s) for s in (RAM_CODE / "ram_tag_list_threshold.txt").read_text().split()])
        if not (len(tags) == len(thresholds) == RAM_CLASSES):
            raise RuntimeError(f"RAM++ tag list has {len(tags)} tags and {len(thresholds)} thresholds, "
                               f"expected {RAM_CLASSES}")
        path = _fetch(RAM_MODEL, RAM_FILE)
        try:
            checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        except Exception:  # noqa: BLE001 - an older pickle layout; the file is the official release
            checkpoint = torch.load(path, map_location="cpu", weights_only=False)
        state = _ram_state(checkpoint)
        del checkpoint
        net = _ram_class()()
        result = net.load_state_dict(state, strict=False)
        missing = [k for k in result.missing_keys
                   if k != "reweight_scale" and "relative_position_index" not in k and "attn_mask" not in k]
        if missing or result.unexpected_keys:
            raise RuntimeError(f"RAM++ checkpoint does not fit the network: missing {missing[:5]}, "
                               f"unexpected {result.unexpected_keys[:5]}")
        del state
        net.eval()
        if _cuda():
            net = net.half().to(DEVICE)
            torch.cuda.set_per_process_memory_fraction(torch_fraction, 0)
        self.ram = {
            "net": net, "dtype": next(net.parameters()).dtype, "names": np.array(tags, dtype=object),
            "logit_t": _logit(np.clip(thresholds, 1e-4, 1 - 1e-4)),      # a threshold of 1.0 means "never"
            "mean": torch.tensor(RAM_MEAN, device=DEVICE).view(1, 3, 1, 1),
            "std": torch.tensor(RAM_STD, device=DEVICE).view(1, 3, 1, 1),
        }
        self._forward_ram([np.full((RAM_SIZE, RAM_SIZE, 3), 255, np.uint8)], 0.5)                # warm-up
        return {"name": Path(RAM_FILE).stem, "kind": "ram", "tags": len(tags),
                "precision": "fp16" if _cuda() else "fp32", "loadedIn": round(time.monotonic() - started, 1)}

    def _load_then_serve(self):
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            ort_limit, fraction = 0, 1.0
            if _cuda():
                ort_limit, fraction = self._vram_split()
            self.models.append(self._load_wd(ort_limit))
            _log(f"ready: {self.models[-1]}")
            self.models.append(self._load_ram(fraction))
            _log(f"ready: {self.models[-1]}")
            self.status = "ok"
            _log(f"serving on {DEVICE}, cap {VRAM_GB:g} GB, micro-batch {MAX_BATCH}")
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
            return
        self.touch()
        while True:
            job, done = self.queue.get()
            self.touch()
            try:
                done.set_result(self._process(job))
            except Exception as exc:  # noqa: BLE001
                done.set_exception(exc)
            finally:
                self.touch()

    # ---- GPU work (the worker thread only)
    def _forward_wd(self, arrays, floor):
        wd = self.wd
        x = np.stack(arrays)[..., ::-1].astype(np.float32)           # RGB uint8 -> BGR float32 0-255, NHWC
        probs = wd["sess"].run([wd["output"]], {wd["input"]: x})[0].astype(np.float64)
        cal = _sigmoid(_logit(probs) - wd["logit_t"])
        names, out = wd["names"], []
        for b in range(len(arrays)):
            row = {}
            for key in ("general", "character"):
                idx = wd[key]
                scores = cal[b, idx]
                keep = np.flatnonzero(scores >= floor)
                keep = keep[np.argsort(-scores[keep], kind="stable")]
                row[key] = {str(names[idx[i]]): round(float(scores[i]), 4) for i in keep}
            row["rating"] = {str(names[i]): round(float(probs[b, i]), 4) for i in wd["rating"]}
            out.append(row)
        return out

    def _forward_ram(self, arrays, floor):
        import torch

        ram = self.ram
        with torch.inference_mode():
            x = torch.from_numpy(np.stack(arrays)).to(DEVICE).permute(0, 3, 1, 2).float().div_(255.0)
            x = ((x - ram["mean"]) / ram["std"]).to(ram["dtype"])
            logits = ram["net"](x).cpu().numpy().astype(np.float64)
        cal = _sigmoid(logits - ram["logit_t"])
        out = []
        for b in range(len(arrays)):
            keep = np.flatnonzero(cal[b] >= floor)
            keep = keep[np.argsort(-cal[b, keep], kind="stable")]
            out.append({str(ram["names"][i]): round(float(cal[b, i]), 4) for i in keep})
        return out

    def _attempt(self, name, job, chunk, out, errs):
        """Run one micro-batch; on any failure split it so that only the bad picture(s) fail."""
        forward = self._forward_wd if name == "wd" else self._forward_ram
        try:
            result = forward([job.inputs[k][name] for k in chunk], job.floor)
        except Exception as exc:  # noqa: BLE001
            oom = _is_oom(exc)
            if oom and _cuda():
                try:
                    import torch
                    torch.cuda.empty_cache()
                except Exception:  # noqa: BLE001
                    pass
            if len(chunk) == 1:
                errs[chunk[0]] = (f"{name}: CUDA out of memory on a single picture "
                                  f"(raise AITAGGER_VRAM_GB or lower the picture size)" if oom
                                  else f"{name}: {type(exc).__name__}: {exc}")
                _log(f"{name}: picture failed: {errs[chunk[0]]}")
                return
            if oom:
                self.batch[name] = max(1, len(chunk) // 2)
                _log(f"{name}: out of memory at {len(chunk)} pictures - micro-batch is now {self.batch[name]}")
            else:
                _log(f"{name}: batch of {len(chunk)} failed ({type(exc).__name__}: {exc}); retrying in halves")
            half = len(chunk) // 2
            self._attempt(name, job, chunk[:half], out, errs)
            self._attempt(name, job, chunk[half:], out, errs)
            return
        for k, row in zip(chunk, result):
            out[k][name] = row

    def _process(self, job: Job):
        n = len(job.inputs)
        out: list[dict] = [{} for _ in range(n)]
        errs = list(job.errors)
        for name in job.models:
            todo = [i for i in range(n) if errs[i] is None]
            pos = 0
            while pos < len(todo):
                chunk = todo[pos:pos + self.batch[name]]
                pos += len(chunk)
                self._attempt(name, job, chunk, out, errs)
                self.touch()                         # a long job must never look idle
        return [None if errs[i] else out[i] for i in range(n)], errs

    def submit(self, job: Job):
        self.touch()
        done: Future = Future()
        self.queue.put((job, done))
        result = done.result()
        self.touch()
        return result


# --------------------------------------------------------------------------- pictures (request threads)
_POOL = ThreadPoolExecutor(max_workers=max(2, min(8, os.cpu_count() or 2)), thread_name_prefix="prep")


def _decode(data: bytes, wanted: int) -> Image.Image:
    with Image.open(io.BytesIO(data)) as im:
        if im.format == "JPEG":
            im.draft("RGB", (wanted * 2, wanted * 2))   # huge JPEGs decode at a reduced scale (still >= 2x oversampled)
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
            im = im.convert("RGBA")
            canvas = Image.new("RGBA", im.size, (255, 255, 255, 255))
            canvas.alpha_composite(im)
            im = canvas
        return im.convert("RGB")


def _prepare(b64: str, models, wd_size: int):
    """One picture -> {"wd": uint8 (S, S, 3) white-padded square, "ram": uint8 (384, 384, 3)} (RGB)."""
    im = _decode(base64.b64decode(b64, validate=False), max(wd_size, RAM_SIZE))
    out = {}
    if "wd" in models:
        w, h = im.size
        side = max(w, h)
        sq = im
        if w != h:
            sq = Image.new("RGB", (side, side), (255, 255, 255))
            sq.paste(im, ((side - w) // 2, (side - h) // 2))
        if side != wd_size:
            sq = sq.resize((wd_size, wd_size), Image.Resampling.BICUBIC)
        out["wd"] = np.asarray(sq, dtype=np.uint8)
    if "ram" in models:
        out["ram"] = np.asarray(im.resize((RAM_SIZE, RAM_SIZE), Image.Resampling.BILINEAR), dtype=np.uint8)
    return out


TAGGER: Tagger | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # quiet
        pass

    def _json(self, code: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/") != "/health":
            return self._json(404, {"error": "not found"})
        t = TAGGER
        self._json(200, {"status": t.status, "error": t.error or None, "device": DEVICE, "vramCapGb": VRAM_GB,
                         "batch": MAX_BATCH, "effectiveBatch": dict(t.batch), "models": list(t.models),
                         "idleExitMinutes": IDLE_EXIT_MINUTES,
                         "idleSeconds": round(time.monotonic() - t.last_used), "busy": t.busy})

    def do_POST(self):  # noqa: N802
        if self.path.split("?")[0].rstrip("/") != "/tag":
            self.close_connection = True             # the unread body would corrupt a kept-alive connection
            return self._json(404, {"error": "not found"})
        t = TAGGER
        if t.status != "ok":
            self.close_connection = True
            return self._json(503, {"error": t.error or "the models are still loading", "status": t.status})
        t.enter()                                    # from here on the server must not idle-exit
        try:
            started = time.monotonic()
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self.close_connection = True
                return self._json(413, {"error": "request too large"})
            try:
                req = json.loads(self.rfile.read(length))
                if not isinstance(req, dict):
                    raise ValueError("expected an object")
            except ValueError as exc:
                return self._json(400, {"error": f"bad JSON: {exc}"})
            images = req.get("images")
            if not isinstance(images, list) or not images:
                return self._json(400, {"error": "no images"})
            if len(images) > MAX_IMAGES:
                return self._json(400, {"error": f"too many images ({len(images)}); at most {MAX_IMAGES}"})
            try:
                floor = min(1.0, max(0.001, float(req.get("floor", 0.05))))
            except (TypeError, ValueError):
                return self._json(400, {"error": "floor must be a number"})
            wanted = req.get("models") or ["wd", "ram"]
            if not isinstance(wanted, list) or not wanted or any(m not in ("wd", "ram") for m in wanted):
                return self._json(400, {"error": 'models must be a list of "wd" and/or "ram"'})
            models = [m for m in ("wd", "ram") if m in wanted]

            def prep(item):
                try:
                    return _prepare(item, models, t.wd["size"]), None
                except binascii.Error:
                    return None, "invalid base64"
                except Image.UnidentifiedImageError:
                    return None, "cannot identify image file"
                except Exception as exc:  # noqa: BLE001 - one unreadable picture must not fail the batch
                    return None, f"{type(exc).__name__}: {exc}"

            prepared = list(_POOL.map(prep, images))
            t.touch()
            inputs = [p[0] for p in prepared]
            errors = [p[1] for p in prepared]
            if all(e is not None for e in errors):
                results = [None] * len(images)
            else:
                results, errors = t.submit(Job(inputs, errors, models, floor))
            return self._json(200, {"results": results, "errors": errors,
                                    "tookMs": round((time.monotonic() - started) * 1000)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})
        finally:
            t.leave()


def _idle_watch() -> None:
    """Exit after a quiet spell so the container stops and the GPU memory is free. Never while a request is
    being served, queued or processed (the worker and the request threads keep last_used fresh as well)."""
    if IDLE_EXIT_MINUTES <= 0:
        return
    while True:
        time.sleep(min(15.0, max(1.0, IDLE_EXIT_MINUTES * 60 / 4)))
        t = TAGGER
        if t.status == "loading" or t.busy > 0 or not t.queue.empty():
            continue
        if time.monotonic() - t.last_used > IDLE_EXIT_MINUTES * 60:
            _log(f"idle for {IDLE_EXIT_MINUTES:g} min - exiting to free the GPU")
            os._exit(0)


def main() -> int:
    global TAGGER
    TAGGER = Tagger()
    threading.Thread(target=_idle_watch, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):      # PID 1 ignores signals without a handler: stop at once
        signal.signal(sig, lambda *_: os._exit(0))
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    server.daemon_threads = True
    _log(f"listening; loading {WD_MODEL} and {RAM_MODEL}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

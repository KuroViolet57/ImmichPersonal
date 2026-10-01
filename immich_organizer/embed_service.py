"""Search+ model server: turns photos and text into vectors with a CLIP-style model.

Runs inside its own GPU container (see deploy/searchplus). The panel sends it
photos while it builds the Search+ index, and the words you type when you
search; everything else (the index, scoring, filters) happens in the panel.

    GET  /health          -> {"status": "loading" | "ok" | "error", "model": ..., "dim": 1280, ...}
    POST /embed/text      {"texts": ["a dog on a beach", ...]}
                          -> {"vectors": [<base64 float16 x dim>, ...], "dim": 1280}
    POST /embed/images    {"images": [<base64 jpeg/png>, ...]}
                          -> {"vectors": [<base64 float16> | null, ...], "errors": [null | "why", ...]}

Vectors are L2-normalised, so a dot product is the cosine similarity.

The model (Meta's Perception Encoder PE-Core G/14 at 448 px by default) is
downloaded once into /cache; after the first start a ready-to-load copy is
kept there too, so later starts take seconds. When nobody has asked
anything for IDLE_EXIT_MINUTES the server exits, which stops the container
and frees the graphics memory; the panel starts it again when needed.
"""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import sys
import threading
import time
import traceback
from concurrent.futures import Future
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
from PIL import Image, ImageFile

ImageFile.LOAD_TRUNCATED_IMAGES = True
Image.MAX_IMAGE_PIXELS = None

MODEL = os.environ.get("SEARCHPLUS_MODEL", "PE-Core-bigG-14-448")
PRETRAINED = os.environ.get("SEARCHPLUS_PRETRAINED", "meta")         # "" = random weights (tests)
CACHE = Path(os.environ.get("SEARCHPLUS_CACHE", "/cache"))
DEVICE = os.environ.get("SEARCHPLUS_DEVICE", "cuda")
PRECISION = os.environ.get("SEARCHPLUS_PRECISION", "bf16" if DEVICE == "cuda" else "fp32")
MAX_BATCH = int(os.environ.get("SEARCHPLUS_BATCH", "16"))
IDLE_EXIT_MINUTES = float(os.environ.get("IDLE_EXIT_MINUTES", "20"))


def _b64(vec: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(vec, dtype="<f2").tobytes()).decode()


class Encoder:
    """The model, loaded in the background; every GPU call runs on one thread."""

    def __init__(self):
        self.status, self.error, self.dim, self.loaded_in = "loading", "", 0, 0.0
        self.model = self.tokenizer = None
        self.size, self.mean, self.std, self.squash = 224, (0.5,) * 3, (0.5,) * 3, True
        self.queue: queue.Queue = queue.Queue()
        self.last_used = time.monotonic()
        threading.Thread(target=self._load_then_serve, daemon=True).start()

    # ---- loading
    def _fast_path(self) -> Path:
        return CACHE / f"{MODEL}-{PRETRAINED or 'random'}-{PRECISION}.pt"

    def _load(self):
        import open_clip
        import torch

        fast = self._fast_path()
        model = None
        if PRETRAINED and fast.exists():
            try:
                model = torch.load(fast, map_location=DEVICE, weights_only=False, mmap=True)
            except Exception:  # noqa: BLE001 - stale or broken copy: rebuild it
                traceback.print_exc()
                model = None
        if model is None:
            print(f"building {MODEL} ({PRETRAINED or 'random weights'}) - the first time this downloads the model",
                  flush=True)
            model = open_clip.create_model(MODEL, pretrained=PRETRAINED or None, precision=PRECISION, device=DEVICE,
                                           cache_dir=str(CACHE / "hf"))
            if PRETRAINED:
                tmp = fast.with_suffix(".tmp")
                torch.save(model, tmp)
                tmp.replace(fast)
        model.eval()
        cfg = open_clip.get_model_config(MODEL) or {}
        pre = getattr(model.visual, "preprocess_cfg", {}) or {}
        self.size = int(cfg.get("vision_cfg", {}).get("image_size") or pre.get("size") or 224)
        self.mean = tuple(pre.get("mean") or (0.5, 0.5, 0.5))
        self.std = tuple(pre.get("std") or (0.5, 0.5, 0.5))
        self.squash = (pre.get("resize_mode") or "squash") == "squash"
        self.tokenizer = open_clip.get_tokenizer(MODEL)
        self.model = model
        self.dtype = next(model.visual.parameters()).dtype
        with torch.inference_mode():
            probe = self.model.encode_text(self.tokenizer(["a photo"]).to(DEVICE), normalize=True)
        self.dim = int(probe.shape[-1])
        if DEVICE == "cuda":
            torch.cuda.empty_cache()

    def _load_then_serve(self):
        started = time.monotonic()
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            self._load()
            self.loaded_in = round(time.monotonic() - started, 1)
            self.status = "ok"
            print(f"ready: {MODEL} dim={self.dim} size={self.size} on {DEVICE}/{PRECISION} in {self.loaded_in}s",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self.status, self.error = "error", f"{type(exc).__name__}: {exc}"
            return
        self.last_used = time.monotonic()
        while True:
            kind, payload, done = self.queue.get()
            try:
                done.set_result(self._run(kind, payload))
            except Exception as exc:  # noqa: BLE001
                done.set_exception(exc)

    # ---- GPU work (this thread only)
    def _run(self, kind: str, payload) -> np.ndarray:
        import torch

        out = []
        with torch.inference_mode():
            for i in range(0, len(payload), MAX_BATCH):
                chunk = payload[i:i + MAX_BATCH]
                if kind == "text":
                    vec = self.model.encode_text(self.tokenizer(chunk).to(DEVICE), normalize=True)
                else:
                    batch = torch.from_numpy(np.stack(chunk)).to(DEVICE, dtype=self.dtype)
                    vec = self.model.encode_image(batch, normalize=True)
                vec = vec.float()
                vec = vec / vec.norm(dim=-1, keepdim=True).clamp_min(1e-6)
                out.append(vec.cpu().numpy())
        return np.concatenate(out) if out else np.zeros((0, self.dim), np.float32)

    def submit(self, kind: str, payload) -> np.ndarray:
        self.last_used = time.monotonic()
        done: Future = Future()
        self.queue.put((kind, payload, done))
        result = done.result()
        self.last_used = time.monotonic()
        return result

    # ---- request threads
    def prepare(self, data: bytes) -> np.ndarray:
        with Image.open(io.BytesIO(data)) as im:
            s = self.size
            if im.format == "JPEG":
                im.draft("RGB", (s, s))            # decode big JPEGs at a reduced scale (much faster)
            if getattr(im, "is_animated", False):
                im.seek(0)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
                bg.alpha_composite(im)
                im = bg
            im = im.convert("RGB")
            if self.squash:
                im = im.resize((s, s), Image.Resampling.BILINEAR)
            else:                                   # shortest side to s, centre crop
                w, h = im.size
                k = s / min(w, h)
                im = im.resize((max(s, round(w * k)), max(s, round(h * k))), Image.Resampling.BICUBIC)
                w, h = im.size
                left, top = (w - s) // 2, (h - s) // 2
                im = im.crop((left, top, left + s, top + s))
            arr = np.asarray(im, dtype=np.float32) / 255.0
        arr = (arr - np.array(self.mean, np.float32)) / np.array(self.std, np.float32)
        return np.transpose(arr, (2, 0, 1))


ENCODER: Encoder | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):  # quiet
        pass

    def _json(self, code: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") != "/health":
            return self._json(404, {"error": "not found"})
        enc = ENCODER
        self._json(200, {"status": enc.status, "error": enc.error, "model": MODEL, "pretrained": PRETRAINED,
                         "dim": enc.dim, "size": enc.size, "device": DEVICE, "precision": PRECISION,
                         "loadedIn": enc.loaded_in, "idleExitMinutes": IDLE_EXIT_MINUTES,
                         "idleSeconds": round(time.monotonic() - enc.last_used)})

    def do_POST(self):  # noqa: N802
        route = self.path.rstrip("/")
        if route not in ("/embed/text", "/embed/images"):
            return self._json(404, {"error": "not found"})
        enc = ENCODER
        if enc.status != "ok":
            return self._json(503, {"error": enc.error or "the model is still loading", "status": enc.status})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        except ValueError as exc:
            return self._json(400, {"error": f"bad JSON: {exc}"})
        try:
            if route == "/embed/text":
                texts = [str(t)[:2000] for t in req.get("texts") or []]
                if not texts:
                    return self._json(400, {"error": "no texts"})
                vecs = enc.submit("text", texts)
                return self._json(200, {"vectors": [_b64(v) for v in vecs], "dim": enc.dim})
            images = req.get("images") or []
            if not images:
                return self._json(400, {"error": "no images"})
            prepared, errors = [], []
            for b in images:
                try:
                    prepared.append(enc.prepare(base64.b64decode(b)))
                    errors.append(None)
                except Exception as exc:  # noqa: BLE001 - one unreadable image must not fail the batch
                    prepared.append(None)
                    errors.append(f"{type(exc).__name__}: {exc}")
            good = [p for p in prepared if p is not None]
            vecs = iter(enc.submit("image", good)) if good else iter(())
            out = [None if p is None else _b64(next(vecs)) for p in prepared]
            return self._json(200, {"vectors": out, "errors": errors, "dim": enc.dim})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": f"{type(exc).__name__}: {exc}"})


def _idle_watch() -> None:
    """Exit after a quiet spell so the container stops and the GPU memory is free."""
    if IDLE_EXIT_MINUTES <= 0:
        return
    while True:
        time.sleep(15)
        enc = ENCODER
        if enc.status == "loading":
            continue
        if enc.queue.empty() and time.monotonic() - enc.last_used > IDLE_EXIT_MINUTES * 60:
            print(f"idle for {IDLE_EXIT_MINUTES:g} min - exiting to free the GPU", flush=True)
            os._exit(0)


def main() -> int:
    global ENCODER
    ENCODER = Encoder()
    threading.Thread(target=_idle_watch, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), Handler)
    server.daemon_threads = True
    print(f"listening; loading {MODEL}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())

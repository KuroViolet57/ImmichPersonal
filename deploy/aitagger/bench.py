#!/usr/bin/env python3
"""Speed and GPU memory of a running AI Tagger container, measured through its HTTP API (decoding and transfer
included), the way the panel uses it.

    python deploy/aitagger/bench.py --dir /path/to/previews [--url http://127.0.0.1:11440] [--count 240]
        [--per-request 8] [--inflight 2] [--models wd,pixai,ram,e621] [--shrink 1024]

It shrinks the pictures (JPEG files of any size; Immich's preview JPEGs are fine) to --shrink pixels on the long side
like the panel's captures, waits for the service to be ready, sends --count pictures (the folder repeated if it holds
fewer) in requests of --per-request pictures with --inflight requests at a time, and prints one line of JSON:
pictures per second, the service's micro-batches (/health "effectiveBatch") and the GPU memory (nvidia-smi, if found)
before, after loading and at the peak. "growthMiB" is what the whole GPU's used memory grew by since the script
started, so the container should be started after the baseline is taken; other GPU users must be idle for it to mean
this service alone.
"""
from __future__ import annotations

import argparse
import base64
import glob
import io
import json
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor


def used_mib():
    try:
        out = subprocess.check_output(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                      text=True, timeout=10)
        return int(out.split()[0])
    except Exception:  # noqa: BLE001 - no nvidia-smi on this machine
        return None


def health(url):
    with urllib.request.urlopen(url + "/health", timeout=10) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--url", default="http://127.0.0.1:11440")
    ap.add_argument("--dir", required=True, help="folder with .jpg / .jpeg pictures")
    ap.add_argument("--count", type=int, default=240)
    ap.add_argument("--per-request", type=int, default=8)
    ap.add_argument("--inflight", type=int, default=2)
    ap.add_argument("--models", default="", help="comma separated subset of wd,pixai,ram,e621 (default: all)")
    ap.add_argument("--shrink", type=int, default=1024)
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for the service to be ready")
    args = ap.parse_args()

    from PIL import Image
    files = sorted(glob.glob(args.dir + "/*.jpg") + glob.glob(args.dir + "/*.jpeg"))
    if not files:
        print("no pictures in", args.dir, file=sys.stderr)
        return 2
    pictures = []
    for path in files[:args.count]:
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((args.shrink, args.shrink))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
        pictures.append(base64.b64encode(buf.getvalue()).decode())
    pictures = (pictures * (args.count // len(pictures) + 1))[:args.count]
    models = [m for m in args.models.split(",") if m] or None

    baseline = used_mib()
    deadline = time.time() + args.wait
    while True:
        try:
            state = health(args.url)
            if state["status"] == "ok":
                break
            if state["status"] == "error":
                print("service error:", state["error"], file=sys.stderr)
                return 1
        except OSError:
            pass
        if time.time() > deadline:
            print("the service did not become ready", file=sys.stderr)
            return 1
        time.sleep(1)
    loaded = used_mib()

    def send(batch):
        body = {"images": batch, "floor": 0.05}
        if models:
            body["models"] = models
        req = urllib.request.Request(args.url + "/tag", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=600) as resp:
            out = json.load(resp)
        return sum(1 for e in out["errors"] if e)

    peak = [loaded or 0]
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            now = used_mib()
            if now:
                peak[0] = max(peak[0], now)
            time.sleep(0.1)

    if loaded is not None:
        threading.Thread(target=sample, daemon=True).start()
    send(pictures[:args.per_request])                              # warm-up request, not timed
    batches = [pictures[i:i + args.per_request] for i in range(0, len(pictures), args.per_request)]
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.inflight) as pool:
        failed = sum(pool.map(send, batches))
    elapsed = time.time() - started
    stop.set()
    state = health(args.url)
    result = {"pictures": len(pictures), "perRequest": args.per_request, "inflight": args.inflight,
              "models": models or ["wd", "pixai", "ram", "e621"], "seconds": round(elapsed, 1),
              "picturesPerSecond": round(len(pictures) / elapsed, 1), "failedPictures": failed,
              "vramCapGb": state["vramCapGb"], "effectiveBatch": state["effectiveBatch"]}
    if baseline is not None:
        result.update({"baselineMiB": baseline, "loadedMiB": loaded, "peakMiB": peak[0],
                       "growthMiB": peak[0] - baseline, "growthGb": round((peak[0] - baseline) / 1024, 2)})
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
TOON vs JSON benchmark API.

Loads a fixed, bundled 10,000-record dataset (data/dataset_flat.json,
data/dataset_nested.json) once at startup -- no random regeneration on
cold start, so results are reproducible across deploys and across
UptimeRobot-triggered wake-ups.

Endpoints:
    GET /health              -> for UptimeRobot
    GET /cases                -> list of the 21 available experiment cases
    GET /data                 -> format x compression, no caching (cases 1-18)
    GET /cache/data            -> the 3 caching cases (19-21)

Run locally:
    pip install -r requirements.txt
    uvicorn main:app --port 8000

Deploy on Render as a Web Service:
    Build command: pip install -r requirements.txt
    Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
"""
import gzip
import json
import time
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

try:
    import brotli
    HAVE_BROTLI = True
except ImportError:
    HAVE_BROTLI = False

app = FastAPI(title="TOON vs JSON Benchmark API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path(__file__).parent / "data"
DATASET = {
    "flat": json.loads((DATA_DIR / "dataset_flat.json").read_text()),
    "nested": json.loads((DATA_DIR / "dataset_nested.json").read_text()),
}


# ---------- native encoders (no JSON<->TOON conversion; both built from the same Python objects) ----------

def encode_json(rows) -> str:
    return json.dumps(rows)


def _esc(v):
    s = str(v)
    return f'"{s}"' if ("," in s or "\n" in s) else s


def encode_toon(rows, structure: str) -> str:
    if structure == "flat":
        header = f"items[{len(rows)}]{{id,name,age,city}}:"
        lines = [f"{r['id']},{_esc(r['name'])},{r['age']},{_esc(r['city'])}" for r in rows]
        return "\n".join([header] + lines)
    blocks = []
    for r in rows:
        b = f"item:\n  id: {r['id']}\n  name: {_esc(r['name'])}\n  address:\n    city: {_esc(r['address']['city'])}\n    zip: {r['address']['zip']}"
        if "tags" in r:
            b += f"\n  tags: [{','.join(r['tags'])}]"
        blocks.append(b)
    return "\n".join(blocks)


def decode_toon_flat(text: str):
    """Only supports the flat/tabular case -- used solely for the canonical-cache
    conversion path. Nested TOON decoding is out of scope for this demo."""
    lines = text.strip().split("\n")
    header = lines[0]
    n = int(header[header.index("[") + 1:header.index("]")])
    fields = header[header.index("{") + 1:header.index("}")].split(",")
    out = []
    for row in lines[1:1 + n]:
        vals = row.split(",")
        rec = {}
        for k, v in zip(fields, vals):
            rec[k] = int(v) if v.isdigit() else v
        out.append(rec)
    return out


def compress(body: bytes, encoding: str) -> bytes:
    if encoding == "gzip":
        buf = BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as f:
            f.write(body)
        return buf.getvalue()
    if encoding == "br" and HAVE_BROTLI:
        return brotli.compress(body, quality=11)
    return body


@app.head("/health")
def health():
    return {"status": "ok"}


@app.get("/cases")
def cases():
    base = []
    for structure in ["flat", "nested"]:
        for n in [10, 1000, 10000]:
            for encoding in ["identity", "gzip", "brotli"]:
                base.append({"type": "plain", "structure": structure, "n": n, "encoding": encoding})
    for mode in ["json_cache", "toon_cache", "canonical_cache"]:
        base.append({"type": "cache", "mode": mode, "structure": "flat", "n": 10000, "encoding": "identity"})
    return {"count": len(base), "cases": base}


@app.get("/data")
def get_data(format: str = Query("json"), encoding: str = Query("identity"),
             n: int = Query(10), structure: str = Query("flat")):
    rows = DATASET[structure][:n]

    t0 = time.perf_counter()
    body_str = encode_toon(rows, structure) if format == "toon" else encode_json(rows)
    encode_ms = (time.perf_counter() - t0) * 1000

    body = body_str.encode("utf-8")
    raw_len = len(body)

    t1 = time.perf_counter()
    body = compress(body, encoding)
    compress_ms = (time.perf_counter() - t1) * 1000

    headers = {
        "X-Encode-Time-Ms": f"{encode_ms:.4f}",
        "X-Compress-Time-Ms": f"{compress_ms:.4f}",
        "X-Raw-Bytes": str(raw_len),
        "X-Compressed-Bytes": str(len(body)),
    }
    if encoding != "identity":
        headers["Content-Encoding"] = encoding
    media = "text/toon" if format == "toon" else "application/json"
    return Response(content=body, media_type=media, headers=headers)


# ---------- Cache layer: 3 modes ----------
# json_cache        -> cache stores pre-serialized JSON bytes
# toon_cache        -> cache stores pre-serialized TOON bytes
# canonical_cache   -> cache stores ONLY TOON bytes; JSON requests are converted
#                      from the cached TOON on read (flat structure only)

_CACHE: dict = {}


@app.get("/cache/data")
def get_cached_data(mode: str = Query("json_cache"), format: str = Query("json"),
                     n: int = Query(10000), structure: str = Query("flat")):
    key = (mode, structure, n)
    t0 = time.perf_counter()
    hit = key in _CACHE

    if mode == "json_cache":
        if not hit:
            _CACHE[key] = encode_json(DATASET[structure][:n]).encode("utf-8")
        body = _CACHE[key]
        media = "application/json"

    elif mode == "toon_cache":
        if not hit:
            _CACHE[key] = encode_toon(DATASET[structure][:n], structure).encode("utf-8")
        body = _CACHE[key]
        media = "text/toon"

    else:  # canonical_cache
        cache_key = ("canonical_toon", structure, n)
        if cache_key not in _CACHE:
            _CACHE[cache_key] = encode_toon(DATASET[structure][:n], structure).encode("utf-8")
        toon_bytes = _CACHE[cache_key]
        hit = cache_key in _CACHE
        if format == "toon":
            body = toon_bytes
            media = "text/toon"
        else:
            # real conversion happens here: canonical TOON -> JSON, only on read
            rows = decode_toon_flat(toon_bytes.decode("utf-8"))
            body = json.dumps(rows).encode("utf-8")
            media = "application/json"

    total_ms = (time.perf_counter() - t0) * 1000
    headers = {
        "X-Cache-Hit": "true" if hit else "false",
        "X-Total-Time-Ms": f"{total_ms:.4f}",
        "X-Bytes": str(len(body)),
    }
    return Response(content=body, media_type=media, headers=headers)


@app.post("/cache/clear")
def clear_cache():
    _CACHE.clear()
    return {"cleared": True}

"""
TOON vs JSON benchmark API -- v2.

Two GENUINELY SEPARATE database files per structure, loaded once at startup:
    data/dataset_flat.json / data/dataset_nested.json   -- JSON source of truth
    data/dataset_flat.toon / data/dataset_nested.toon   -- TOON source of truth

The TOON files are NOT derived from the JSON files at request time (or at
import time) -- they're independent bundled files generated once (see
build_data.py) and parsed back into Python rows via toon_codec.decode_toon()
at server startup. Serving a JSON request never touches the .toon file, and
serving a TOON request never touches the .json file. The only reason both
happen to contain identical content is that build_data.py generated them
from the same fixed-seed source values once, offline.

Endpoints:
    GET /health       -> status + brotli availability (diagnoses build issues)
    GET /cases        -> list of available experiment cases
    GET /data          -> format x compression (no caching)
    GET /cache/data     -> the 3 caching modes
    POST /cache/clear
"""
import gzip
import json
import time
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

from toon_codec import encode_toon, decode_toon

try:
    import brotli
    HAVE_BROTLI = True
    _BROTLI_IMPORT_ERROR = None
except ImportError as e:
    HAVE_BROTLI = False
    _BROTLI_IMPORT_ERROR = str(e)

app = FastAPI(title="TOON vs JSON Benchmark API v2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path(__file__).parent / "data"

# JSON database -- loaded directly, never touches TOON
JSON_DB = {
    "flat": json.loads((DATA_DIR / "dataset_flat.json").read_text()),
    "nested": json.loads((DATA_DIR / "dataset_nested.json").read_text()),
}

# TOON database -- loaded from its OWN bundled .toon file, parsed once via
# the codec's decoder. Never derived from JSON_DB.
TOON_DB = {
    "flat": decode_toon((DATA_DIR / "dataset_flat.toon").read_text(), "flat"),
    "nested": decode_toon((DATA_DIR / "dataset_nested.toon").read_text(), "nested"),
}

N_CHOICES = [1000, 10000]
ENCODING_CHOICES = ["gzip", "brotli"]
STRUCTURE_CHOICES = ["flat", "nested"]


def compress(body: bytes, encoding: str) -> bytes:
    # Normalize encoding names -- callers may send "br" or "brotli", "gzip", or
    # "identity"/"none". This was the actual root cause of the earlier bug:
    # the frontend sent "brotli" as the query param, but this function only
    # matched the literal string "br", so brotli silently never triggered --
    # even though the brotli package itself was installed and working fine.
    encoding = (encoding or "identity").lower()
    if encoding == "gzip":
        buf = BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9) as f:
            f.write(body)
        return buf.getvalue()
    if encoding in ("br", "brotli"):
        if not HAVE_BROTLI:
            raise RuntimeError(
                f"Brotli requested but not available on this server "
                f"(import error: {_BROTLI_IMPORT_ERROR}). Check /health.")
        try:
            return brotli.compress(body, quality=5)
        except Exception as e:
            raise RuntimeError(f"Brotli compression failed at runtime: {e}")
    return body


@app.get("/health")
def health():
    return {"status": "ok", "brotli_available": HAVE_BROTLI, "brotli_import_error": _BROTLI_IMPORT_ERROR}


@app.get("/cases")
def cases():
    base = []
    for structure in STRUCTURE_CHOICES:
        for n in N_CHOICES:
            for encoding in ENCODING_CHOICES:
                base.append({"type": "plain", "structure": structure, "n": n, "encoding": encoding})
    for mode in ["json_cache", "toon_cache", "canonical_cache"]:
        for structure in STRUCTURE_CHOICES:
            base.append({"type": "cache", "mode": mode, "structure": structure, "n": 10000})
    return {"count": len(base), "cases": base}


@app.get("/data")
def get_data(format: str = Query("json"), encoding: str = Query("identity"),
             n: int = Query(10), structure: str = Query("flat")):
    t0 = time.perf_counter()
    if format == "toon":
        rows = TOON_DB[structure][:n]
        body_str = encode_toon(rows, structure)
        media = "text/toon"
    else:
        rows = JSON_DB[structure][:n]
        body_str = json.dumps(rows)
        media = "application/json"
    encode_ms = (time.perf_counter() - t0) * 1000

    body = body_str.encode("utf-8")
    raw_len = len(body)

    t1 = time.perf_counter()
    try:
        body = compress(body, encoding)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    compress_ms = (time.perf_counter() - t1) * 1000

    headers = {
        "X-Encode-Time-Ms": f"{encode_ms:.4f}",
        "X-Compress-Time-Ms": f"{compress_ms:.4f}",
        "X-Raw-Bytes": str(raw_len),
        "X-Compressed-Bytes": str(len(body)),
    }
    if encoding != "identity":
        headers["Content-Encoding"] = encoding
    return Response(content=body, media_type=media, headers=headers)


# ---------- Cache layer: 3 modes, now working for BOTH structures ----------
# json_cache        -> caches pre-serialized JSON bytes (from JSON_DB)
# toon_cache        -> caches pre-serialized TOON bytes (from TOON_DB)
# canonical_cache   -> caches ONLY TOON bytes (from TOON_DB); JSON reads are
#                      converted from that cached TOON via decode_toon() on
#                      every read (real conversion, on-demand, both structures)

_CACHE: dict = {}


@app.get("/cache/data")
def get_cached_data(mode: str = Query("json_cache"), format: str = Query("json"),
                     n: int = Query(10000), structure: str = Query("flat")):
    key = (mode, structure, n)
    t0 = time.perf_counter()
    hit = key in _CACHE

    if mode == "json_cache":
        if not hit:
            _CACHE[key] = json.dumps(JSON_DB[structure][:n]).encode("utf-8")
        body = _CACHE[key]
        media = "application/json"

    elif mode == "toon_cache":
        if not hit:
            _CACHE[key] = encode_toon(TOON_DB[structure][:n], structure).encode("utf-8")
        body = _CACHE[key]
        media = "text/toon"

    else:  # canonical_cache
        cache_key = ("canonical_toon", structure, n)
        if cache_key not in _CACHE:
            _CACHE[cache_key] = encode_toon(TOON_DB[structure][:n], structure).encode("utf-8")
        toon_bytes = _CACHE[cache_key]
        hit = cache_key in _CACHE
        if format == "toon":
            body = toon_bytes
            media = "text/toon"
        else:
            # real conversion on read, now works for flat AND nested
            rows = decode_toon(toon_bytes.decode("utf-8"), structure)
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

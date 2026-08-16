"""
TOON vs JSON benchmark API -- v3.

Changes from v2 (see accompanying explanation for full rationale):
  - Compression LEVEL is now an explicit, validated query parameter, not hardcoded.
  - Content-Encoding uses the correct standard token "br" for Brotli (was "brotli").
  - Headers renamed for clarity: X-Serialization-Time-Ms, X-Compression-Time-Ms,
    X-Server-Processing-Time-Ms (serialization + compression only, no network).
  - Startup validation: JSON_DB and TOON_DB are asserted semantically equal, once,
    at import time. Startup fails loudly if they ever diverge.
  - New "source" param on /data: "auto" (default -- json format reads JSON_DB,
    toon format reads TOON_DB), "json_db", or "toon_db" to force either format
    to be served from the OTHER database. This lets you test whether serving
    TOON output sourced from the JSON database (or vice versa) behaves any
    differently from the native pairing -- a robustness/equivalence check.
  - /health reports actual brotli availability so level-11-unavailable is never
    silently swallowed.

Preserved from v2 (unchanged by design, per requirements):
  - GET /health, GET /cases, GET /data, GET /cache/data, POST /cache/clear
  - Two independent database files (dataset_flat.json/.toon, dataset_nested.json/.toon)
  - /data never converts JSON<->TOON at request time; only /cache/data's
    canonical_cache mode does a deliberate, measured conversion on read.
"""
import gzip
import json
import sys
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

app = FastAPI(title="TOON vs JSON Benchmark API v3")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path(__file__).parent / "data"

JSON_DB = {
    "flat": json.loads((DATA_DIR / "dataset_flat.json").read_text()),
    "nested": json.loads((DATA_DIR / "dataset_nested.json").read_text()),
}
TOON_DB = {
    "flat": decode_toon((DATA_DIR / "dataset_flat.toon").read_text(), "flat"),
    "nested": decode_toon((DATA_DIR / "dataset_nested.toon").read_text(), "nested"),
}


def _validate_db_equivalence():
    """Runs ONCE at startup (import time), never per-request. Fails loudly and
    aborts startup if the two databases are not semantically identical -- the
    whole benchmark's validity depends on JSON and TOON representing the same
    underlying data."""
    for structure in ("flat", "nested"):
        if JSON_DB[structure] != TOON_DB[structure]:
            print(f"FATAL: JSON_DB['{structure}'] != TOON_DB['{structure}'] -- "
                  f"the two databases are not semantically equivalent. Aborting startup.",
                  file=sys.stderr)
            sys.exit(1)
    print(f"Startup validation OK: JSON_DB and TOON_DB are semantically equal "
          f"({len(JSON_DB['flat'])} flat, {len(JSON_DB['nested'])} nested records).")


_validate_db_equivalence()

N_CHOICES = [1000, 10000]
GZIP_LEVELS = [1, 5, 9]
BROTLI_LEVELS = [1, 5, 9, 11]
STRUCTURE_CHOICES = ["flat", "nested"]


def compress(body: bytes, encoding: str, level: int | None):
    """Returns (compressed_bytes, actual_level_used, content_encoding_token)."""
    encoding = (encoding or "identity").lower()

    if encoding == "gzip":
        lvl = level if level in GZIP_LEVELS else 9
        buf = BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=lvl) as f:
            f.write(body)
        return buf.getvalue(), lvl, "gzip"

    if encoding in ("br", "brotli"):
        if not HAVE_BROTLI:
            raise RuntimeError(
                f"Brotli requested but not available on this server "
                f"(import error: {_BROTLI_IMPORT_ERROR}). Check /health.")
        lvl = level if level in BROTLI_LEVELS else 5
        try:
            return brotli.compress(body, quality=lvl), lvl, "br"
        except Exception as e:
            raise RuntimeError(f"Brotli compression failed at runtime: {e}")

    return body, None, None


@app.get("/health")
def health():
    return {"status": "ok", "brotli_available": HAVE_BROTLI, "brotli_import_error": _BROTLI_IMPORT_ERROR,
            "gzip_levels": GZIP_LEVELS, "brotli_levels": BROTLI_LEVELS}


@app.get("/cases")
def cases():
    base = []
    for structure in STRUCTURE_CHOICES:
        for n in N_CHOICES:
            base.append({"type": "plain", "structure": structure, "n": n, "encoding": "identity", "level": None})
            for lvl in GZIP_LEVELS:
                base.append({"type": "plain", "structure": structure, "n": n, "encoding": "gzip", "level": lvl})
            for lvl in BROTLI_LEVELS:
                base.append({"type": "plain", "structure": structure, "n": n, "encoding": "brotli", "level": lvl})
    for mode in ["json_cache", "toon_cache", "canonical_cache"]:
        for structure in STRUCTURE_CHOICES:
            base.append({"type": "cache", "mode": mode, "structure": structure, "n": 10000})
    return {"count": len(base), "cases": base}


@app.get("/data")
def get_data(format: str = Query("json"), encoding: str = Query("identity"),
             level: int | None = Query(None), n: int = Query(10), structure: str = Query("flat"),
             source: str = Query("auto")):
    # source: "auto" -> json format reads JSON_DB, toon format reads TOON_DB (native pairing).
    # "json_db" / "toon_db" -> force EITHER format to read from the specified database,
    # to test cross-database equivalence (e.g. TOON output sourced from JSON_DB).
    if source == "auto":
        src = "json_db" if format != "toon" else "toon_db"
    elif source in ("json_db", "toon_db"):
        src = source
    else:
        raise HTTPException(status_code=400, detail=f"Invalid source '{source}'; use auto, json_db, or toon_db.")

    db = JSON_DB if src == "json_db" else TOON_DB

    t0 = time.perf_counter()
    if format == "toon":
        rows = db[structure][:n]
        body_str = encode_toon(rows, structure)
        media = "text/toon"
    else:
        rows = db[structure][:n]
        body_str = json.dumps(rows)
        media = "application/json"
    serialization_ms = (time.perf_counter() - t0) * 1000

    body = body_str.encode("utf-8")
    raw_len = len(body)

    t1 = time.perf_counter()
    try:
        body, actual_level, content_encoding_token = compress(body, encoding, level)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    compression_ms = (time.perf_counter() - t1) * 1000

    server_processing_ms = serialization_ms + compression_ms  # excludes network by design

    headers = {
        "X-Serialization-Time-Ms": f"{serialization_ms:.4f}",
        "X-Compression-Time-Ms": f"{compression_ms:.4f}",
        "X-Server-Processing-Time-Ms": f"{server_processing_ms:.4f}",
        "X-Raw-Bytes": str(raw_len),
        "X-Compressed-Bytes": str(len(body)),
        "X-Encoding": encoding,
        "X-Level": str(actual_level) if actual_level is not None else "",
        "X-Source-DB": src,
    }
    if content_encoding_token:
        headers["Content-Encoding"] = content_encoding_token  # "gzip" or "br" -- standard tokens
    return Response(content=body, media_type=media, headers=headers)


# ---------- Cache layer: unchanged design (kept separate from plain /data experiments) ----------
# json_cache        -> caches pre-serialized JSON bytes (from JSON_DB)
# toon_cache        -> caches pre-serialized TOON bytes (from TOON_DB)
# canonical_cache   -> caches ONLY TOON bytes (from TOON_DB); JSON reads convert
#                      from that cached TOON via decode_toon() on every read.

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

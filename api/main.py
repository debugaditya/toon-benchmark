"""
TOON vs JSON benchmark API -- v4.

Changes from v3:
  - /data timing is now broken into four distinct phases, each separately
    timed and returned as its own header:
        X-Data-Selection-Time-Ms   (rows = db[structure][:n])
        X-Serialization-Time-Ms    (rows -> JSON/TOON text)
        X-Utf8-Encoding-Time-Ms    (text -> bytes)
        X-Compression-Time-Ms      (bytes -> gzip/Brotli bytes)
        X-Server-Processing-Time-Ms = sum of the above four (excludes network)
  - /health now reports infrastructure metadata for reproducibility:
    python_version, brotli_version, toon_codec_version, cpu_count, platform.
  - N_CHOICES expanded to [100, 1000, 10000, 100000] (databases regenerated
    to 100,000 records to support this -- see build_data.py).
  - Everything else (endpoints, cache semantics, cross-database source param,
    startup DB-equivalence validation, Content-Encoding tokens) preserved
    unchanged from v3.
"""
import gzip
import json
import platform
import sys
import time
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

import toon_codec
from toon_codec import encode_toon, decode_toon

try:
    import brotli
    HAVE_BROTLI = True
    _BROTLI_IMPORT_ERROR = None
    _BROTLI_VERSION = getattr(brotli, "__version__", "unknown")
except ImportError as e:
    HAVE_BROTLI = False
    _BROTLI_IMPORT_ERROR = str(e)
    _BROTLI_VERSION = None

app = FastAPI(title="TOON vs JSON Benchmark API v4")
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
    aborts startup if the two databases are not semantically identical."""
    for structure in ("flat", "nested"):
        if JSON_DB[structure] != TOON_DB[structure]:
            print(f"FATAL: JSON_DB['{structure}'] != TOON_DB['{structure}'] -- "
                  f"the two databases are not semantically equivalent. Aborting startup.",
                  file=sys.stderr)
            sys.exit(1)
    print(f"Startup validation OK: JSON_DB and TOON_DB are semantically equal "
          f"({len(JSON_DB['flat']):,} flat, {len(JSON_DB['nested']):,} nested records).")


_validate_db_equivalence()

N_CHOICES = [100, 1000, 10000, 100000]
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
    return {
        "status": "ok",
        "brotli_available": HAVE_BROTLI,
        "brotli_import_error": _BROTLI_IMPORT_ERROR,
        "brotli_version": _BROTLI_VERSION,
        "gzip_levels": GZIP_LEVELS,
        "brotli_levels": BROTLI_LEVELS,
        "n_choices": N_CHOICES,
        # Infrastructure metadata for reproducibility (requirement #20).
        # RAM is intentionally NOT reported -- capturing it reliably would
        # need an extra dependency (psutil); this is a documented limitation.
        "python_version": platform.python_version(),
        "toon_codec_version": toon_codec.__version__,
        "cpu_count": __import__("os").cpu_count(),
        "platform": platform.platform(),
    }


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
    # "json_db" / "toon_db" -> force EITHER format to read from the specified database
    # (secondary robustness experiment -- see requirement #12). Kept as a separate
    # mode selection on the frontend so cross-database results are never mixed
    # into the primary native-pairing results.
    if source == "auto":
        src = "json_db" if format != "toon" else "toon_db"
    elif source in ("json_db", "toon_db"):
        src = source
    else:
        raise HTTPException(status_code=400, detail=f"Invalid source '{source}'; use auto, json_db, or toon_db.")

    db = JSON_DB if src == "json_db" else TOON_DB

    # --- Phase 1: data selection ---
    t0 = time.perf_counter()
    rows = db[structure][:n]
    data_selection_ms = (time.perf_counter() - t0) * 1000

    # --- Phase 2: serialization (rows -> text) ---
    t1 = time.perf_counter()
    if format == "toon":
        body_str = encode_toon(rows, structure)
        media = "text/toon"
    else:
        body_str = json.dumps(rows)
        media = "application/json"
    serialization_ms = (time.perf_counter() - t1) * 1000

    # --- Phase 3: UTF-8 encoding (text -> bytes) ---
    t2 = time.perf_counter()
    body = body_str.encode("utf-8")
    utf8_encoding_ms = (time.perf_counter() - t2) * 1000
    raw_len = len(body)

    # --- Phase 4: compression (bytes -> gzip/brotli bytes) ---
    t3 = time.perf_counter()
    try:
        body, actual_level, content_encoding_token = compress(body, encoding, level)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    compression_ms = (time.perf_counter() - t3) * 1000

    server_processing_ms = data_selection_ms + serialization_ms + utf8_encoding_ms + compression_ms

    headers = {
        "X-Data-Selection-Time-Ms": f"{data_selection_ms:.4f}",
        "X-Serialization-Time-Ms": f"{serialization_ms:.4f}",
        "X-Utf8-Encoding-Time-Ms": f"{utf8_encoding_ms:.4f}",
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


# ---------- Cache layer: unchanged semantics from v3 ----------
# json_cache        -> caches pre-serialized JSON bytes (from JSON_DB)
# toon_cache        -> caches pre-serialized TOON bytes (from TOON_DB)
# canonical_cache   -> caches ONLY TOON bytes (from TOON_DB); JSON reads convert
#                      from that cached TOON via decode_toon() on every read.
# The frontend is responsible for the strict cache-miss/warm-cache separation
# (clear -> populate -> discard -> warmup -> discard -> measure, asserting
# X-Cache-Hit=true on every measured request) -- this endpoint just reports
# X-Cache-Hit honestly so the frontend CAN enforce that.

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
        # BUG FIX (found during v4 testing): `hit` must be captured BEFORE
        # populating the cache, not after -- checking membership after the
        # populate-if-missing step always returns True, making every request
        # (including genuine first-ever misses) falsely report as a hit.
        hit = cache_key in _CACHE
        if not hit:
            _CACHE[cache_key] = encode_toon(TOON_DB[structure][:n], structure).encode("utf-8")
        toon_bytes = _CACHE[cache_key]
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

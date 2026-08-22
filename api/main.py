"""
TOON vs JSON benchmark API -- v8 (Bidirectional).

Key Architectural Updates:
  1. Genuinely Bidirectional: Endpoints accept POST payloads in JSON or TOON.
  2. Granular Inbound Timing:
     - request_decompression_ms: Time taken to decompress gzip/brotli incoming bodies.
     - request_deserialization_ms: Time taken to parse JSON or TOON using C++ toon_cpp.
     - request_decode_ms: Total inbound processing time (decompression + deserialization).
  3. Strict C++ Codec Usage: C++ toon_cpp is the sole encoder/decoder for TOON.
  4. Functional Source Modes:
     - Native: Body sent in format X, returned in format X.
     - Cross: Body sent in format Y, translated/returned in format X.
"""
import gzip
import json
import os
import platform
import sys
import time
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware

try:
    import toon_cpp  # C++ codec for flat and nested structures
    HAVE_CPP_TOON = True
    _CPP_TOON_IMPORT_ERROR = None
except ImportError as e:
    HAVE_CPP_TOON = False
    _CPP_TOON_IMPORT_ERROR = str(e)

try:
    import brotli
    HAVE_BROTLI = True
    _BROTLI_IMPORT_ERROR = None
    _BROTLI_VERSION = getattr(brotli, "__version__", "unknown")
except ImportError as e:
    HAVE_BROTLI = False
    _BROTLI_IMPORT_ERROR = str(e)
    _BROTLI_VERSION = None

app = FastAPI(title="TOON vs JSON Benchmark API v8")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

DATA_DIR = Path(__file__).parent / "data"

JSON_DB = {}
if (DATA_DIR / "dataset_flat.json").exists() and (DATA_DIR / "dataset_nested.json").exists():
    JSON_DB["flat"] = json.loads((DATA_DIR / "dataset_flat.json").read_text())
    JSON_DB["nested"] = json.loads((DATA_DIR / "dataset_nested.json").read_text())

def _cpp_encode(rows, structure: str) -> str:
    return toon_cpp.encode_flat(rows) if structure == "flat" else toon_cpp.encode_nested(rows)

def _cpp_decode(text: str, structure: str):
    return toon_cpp.decode_flat(text) if structure == "flat" else toon_cpp.decode_nested(text)

def _validate_startup():
    if not HAVE_CPP_TOON:
        print(f"FATAL: C++ TOON codec is unavailable: {_CPP_TOON_IMPORT_ERROR}. Aborting startup.", file=sys.stderr)
        sys.exit(1)

_validate_startup()

N_CHOICES = [100, 1000, 10000, 100000]
GZIP_LEVELS = [1, 5, 9]
BROTLI_LEVELS = [1, 5, 9, 11]
STRUCTURE_CHOICES = ["flat", "nested"]

def compress(body: bytes, encoding: str, level: int | None):
    encoding = (encoding or "identity").lower()

    if encoding in ("gzip", "gz"):
        lvl = level if level in GZIP_LEVELS else 9
        buf = BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=lvl) as f:
            f.write(body)
        return buf.getvalue(), lvl, "gzip"

    if encoding in ("br", "brotli"):
        if not HAVE_BROTLI:
            raise RuntimeError(f"Brotli requested but not available (import error: {_BROTLI_IMPORT_ERROR}).")
        lvl = level if level in BROTLI_LEVELS else 5
        try:
            return brotli.compress(body, quality=lvl), lvl, "br"
        except Exception as e:
            raise RuntimeError(f"Brotli compression failed at runtime: {e}")

    return body, None, None

def decompress(body: bytes, encoding: str) -> bytes:
    encoding = (encoding or "").lower()
    if "gzip" in encoding or "gz" in encoding:
        return gzip.decompress(body)
    elif "br" in encoding or "brotli" in encoding:
        if not HAVE_BROTLI:
            raise RuntimeError("Brotli decompress requested but brotli package not available")
        return brotli.decompress(body)
    return body

@app.get("/health")
def health():
    return {
        "status": "ok",
        "brotli_available": HAVE_BROTLI,
        "gzip_levels": GZIP_LEVELS,
        "brotli_levels": BROTLI_LEVELS,
        "primary_toon_codec": "cpp",
        "cpp_codec_available": HAVE_CPP_TOON,
        "bidirectional": True
    }

@app.post("/data")
async def post_data(request: Request, format: str = Query("json"), encoding: str = Query("identity"),
                    level: int | None = Query(None), n: int = Query(10), structure: str = Query("flat"),
                    source: str = Query("auto")):
    
    # --- Phase 1a: Request Decompression ---
    t_decomp_start = time.perf_counter()
    raw_body = await request.body()
    content_encoding = request.headers.get("content-encoding", "").lower()
    content_type = request.headers.get("content-type", "").lower()
    
    try:
        body_bytes = decompress(raw_body, content_encoding)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Request decompression failed: {e}")
    request_decompression_ms = (time.perf_counter() - t_decomp_start) * 1000

    # --- Phase 1b: Request Deserialization ---
    t_deser_start = time.perf_counter()
    body_str = body_bytes.decode("utf-8")
    input_format = "toon" if ("toon" in content_type or "x-toon" in content_type) else "json"
    
    try:
        if input_format == "toon":
            rows = _cpp_decode(body_str, structure)
        else:
            rows = json.loads(body_str)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Request deserialization failed ({input_format}): {e}")
        
    request_deserialization_ms = (time.perf_counter() - t_deser_start) * 1000
    request_decode_ms = request_decompression_ms + request_deserialization_ms

    src_db = f"cross_{input_format}_to_{format}" if (source == "cross" or input_format != format) else f"native_{format}"

    if isinstance(rows, list) and n < len(rows):
        rows = rows[:n]

    # --- Phase 2: Outbound Serialization ---
    t_out_ser = time.perf_counter()
    if format == "toon":
        out_body_str = _cpp_encode(rows, structure)
        media = "text/toon"
        active_codec = "cpp"
    else:
        out_body_str = json.dumps(rows)
        media = "application/json"
        active_codec = "json"
    serialization_ms = (time.perf_counter() - t_out_ser) * 1000

    # --- Phase 3: UTF-8 Encoding ---
    t_out_utf8 = time.perf_counter()
    out_body = out_body_str.encode("utf-8")
    utf8_encoding_ms = (time.perf_counter() - t_out_utf8) * 1000
    raw_len = len(out_body)

    # --- Phase 4: Outbound Compression ---
    t_out_comp = time.perf_counter()
    try:
        out_body, actual_level, out_content_encoding = compress(out_body, encoding, level)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    compression_ms = (time.perf_counter() - t_out_comp) * 1000

    server_processing_ms = request_decode_ms + serialization_ms + utf8_encoding_ms + compression_ms

    headers = {
        "X-Request-Decompression-Time-Ms": f"{request_decompression_ms:.4f}",
        "X-Request-Deserialization-Time-Ms": f"{request_deserialization_ms:.4f}",
        "X-Request-Decode-Time-Ms": f"{request_decode_ms:.4f}",
        "X-Serialization-Time-Ms": f"{serialization_ms:.4f}",
        "X-Utf8-Encoding-Time-Ms": f"{utf8_encoding_ms:.4f}",
        "X-Compression-Time-Ms": f"{compression_ms:.4f}",
        "X-Server-Processing-Time-Ms": f"{server_processing_ms:.4f}",
        "X-Raw-Bytes": str(raw_len),
        "X-Compressed-Bytes": str(len(out_body)),
        "X-Encoding": encoding,
        "X-Level": str(actual_level) if actual_level is not None else "",
        "X-Source-DB": src_db,
        "X-Codec": active_codec,
    }
    if out_content_encoding:
        headers["Content-Encoding"] = out_content_encoding
        
    return Response(content=out_body, media_type=media, headers=headers)


# ---------- Cache Layer ----------
_CACHE: dict = {}

@app.post("/cache/data")
async def post_cached_data(request: Request, mode: str = Query("json_cache"), format: str = Query("json"),
                           n: int = Query(10000), structure: str = Query("flat")):
    t0 = time.perf_counter()
    raw_body = await request.body()
    content_encoding = request.headers.get("content-encoding", "").lower()
    content_type = request.headers.get("content-type", "").lower()
    
    key = (mode, structure, n)
    hit = key in _CACHE

    if mode == "json_cache":
        if not hit:
            try:
                body_bytes = decompress(raw_body, content_encoding)
                rows = _cpp_decode(body_bytes.decode("utf-8"), structure) if "toon" in content_type else json.loads(body_bytes.decode("utf-8"))
            except Exception:
                rows = JSON_DB.get(structure, [])[:n] if JSON_DB else []
            _CACHE[key] = json.dumps(rows).encode("utf-8")
        body = _CACHE[key]
        media = "application/json"

    elif mode == "toon_cache":
        if not hit:
            try:
                body_bytes = decompress(raw_body, content_encoding)
                rows = _cpp_decode(body_bytes.decode("utf-8"), structure) if "toon" in content_type else json.loads(body_bytes.decode("utf-8"))
            except Exception:
                rows = JSON_DB.get(structure, [])[:n] if JSON_DB else []
            _CACHE[key] = _cpp_encode(rows, structure).encode("utf-8")
        body = _CACHE[key]
        media = "text/toon"

    else:  # canonical_cache
        cache_key = ("canonical_toon", structure, n)
        hit = cache_key in _CACHE
        if not hit:
            try:
                body_bytes = decompress(raw_body, content_encoding)
                rows = _cpp_decode(body_bytes.decode("utf-8"), structure) if "toon" in content_type else json.loads(body_bytes.decode("utf-8"))
            except Exception:
                rows = JSON_DB.get(structure, [])[:n] if JSON_DB else []
            _CACHE[cache_key] = _cpp_encode(rows, structure).encode("utf-8")
        
        toon_bytes = _CACHE[cache_key]
        if format == "toon":
            body = toon_bytes
            media = "text/toon"
        else:
            rows = _cpp_decode(toon_bytes.decode("utf-8"), structure)
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
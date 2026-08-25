"""
TOON vs JSON benchmark API -- v8 (Bidirectional).

Key Architectural Updates:
  1. The API owns the four canonical benchmark datasets.
  2. Native requests use the matching JSON/TOON database.
  3. Cross requests use the opposite database representation and translate it.
  4. Benchmark requests contain no dataset payload.
  5. C++ toon_cpp is the encoder/decoder for TOON.
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
TOON_DB = {}

if (DATA_DIR / "dataset_flat.json").exists() and (DATA_DIR / "dataset_nested.json").exists():
    JSON_DB["flat"] = json.loads((DATA_DIR / "dataset_flat.json").read_text())
    JSON_DB["nested"] = json.loads((DATA_DIR / "dataset_nested.json").read_text())

# Canonical TOON source-of-truth, decoded once at startup.
# Native TOON uses this DB; cross JSON uses this DB as its input.
if (DATA_DIR / "dataset_flat.toon").exists() and (DATA_DIR / "dataset_nested.toon").exists():
    TOON_DB["flat"] = toon_cpp.decode_flat(
        (DATA_DIR / "dataset_flat.toon").read_text()
    )
    TOON_DB["nested"] = toon_cpp.decode_nested(
        (DATA_DIR / "dataset_nested.toon").read_text()
    )

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
async def post_data(
    request: Request,
    format: str = Query("json"),
    encoding: str = Query("identity"),
    level: int | None = Query(None),
    n: int = Query(10),
    structure: str = Query("flat"),
    source: str = Query("native"),
):
    """
    Benchmark request.

    The frontend sends ONLY experiment parameters. The API already owns the
    four canonical datasets under data/:

        dataset_flat.json
        dataset_nested.json
        dataset_flat.toon
        dataset_nested.toon

    Native:
        JSON  <- JSON_DB
        TOON  <- TOON_DB

    Cross:
        JSON  <- TOON_DB -> JSON
        TOON  <- JSON_DB -> TOON

    No benchmark payload is generated or uploaded by the frontend.
    """

    if format not in ("json", "toon"):
        raise HTTPException(status_code=400, detail="format must be json or toon")
    if source not in ("native", "cross"):
        raise HTTPException(status_code=400, detail="source must be native or cross")
    if structure not in ("flat", "nested"):
        raise HTTPException(status_code=400, detail="structure must be flat or nested")
    if n < 1:
        raise HTTPException(status_code=400, detail="n must be >= 1")

    # The research frontend sends an empty request body. Do not deserialize
    # anything from the request and do not measure nonexistent inbound work.
    raw_body = await request.body()
    if raw_body:
        del raw_body
        raise HTTPException(
            status_code=400,
            detail="Benchmark /data requests must not contain a dataset body"
        )
    del raw_body

    # ------------------------------------------------------------
    # Phase 1: API-side DB retrieval
    # ------------------------------------------------------------
    t_db = time.perf_counter()

    if source == "native":
        input_format = format
        source_rows = (
            JSON_DB.get(structure, [])
            if format == "json"
            else TOON_DB.get(structure, [])
        )
        src_db = f"native_{format}"
    else:
        input_format = "toon" if format == "json" else "json"
        source_rows = (
            TOON_DB.get(structure, [])
            if input_format == "toon"
            else JSON_DB.get(structure, [])
        )
        src_db = f"cross_{input_format}_to_{format}"

    # Only the requested n rows are carried into the serialization phase.
    rows = source_rows[:n]
    del source_rows

    db_retrieval_ms = (time.perf_counter() - t_db) * 1000

    # ------------------------------------------------------------
    # Phase 2: Serialization / translation
    # ------------------------------------------------------------
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

    # Release Python row references immediately after serialization.
    del rows

    # ------------------------------------------------------------
    # Phase 3: UTF-8 encoding
    # ------------------------------------------------------------
    t_out_utf8 = time.perf_counter()
    out_body = out_body_str.encode("utf-8")
    utf8_encoding_ms = (time.perf_counter() - t_out_utf8) * 1000
    del out_body_str

    raw_len = len(out_body)

    # ------------------------------------------------------------
    # Phase 4: Outbound compression
    # ------------------------------------------------------------
    t_out_comp = time.perf_counter()
    try:
        out_body, actual_level, out_content_encoding = compress(
            out_body, encoding, level
        )
    except RuntimeError as e:
        del out_body
        raise HTTPException(status_code=503, detail=str(e))

    compression_ms = (time.perf_counter() - t_out_comp) * 1000

    server_processing_ms = (
        db_retrieval_ms
        + serialization_ms
        + utf8_encoding_ms
        + compression_ms
    )

    headers = {
        "X-DB-Retrieval-Time-Ms": f"{db_retrieval_ms:.4f}",
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

    return Response(
        content=out_body,
        media_type=media,
        headers=headers,
    )

# ---------- Cache Layer ----------
_CACHE: dict = {}

@app.post("/cache/data")
async def post_cached_data(request: Request, mode: str = Query("json_cache"), format: str = Query("json"),
                           n: int = Query(10000), structure: str = Query("flat")):
    t0 = time.perf_counter()
    raw_body = await request.body()

    key = (mode, structure, n)
    hit = key in _CACHE

    if mode == "json_cache":
        if not hit:
            if len(raw_body) == 0:
                rows = JSON_DB.get(structure, [])[:n] if JSON_DB else []
            else:
                content_encoding = request.headers.get("content-encoding", "").lower()
                content_type = request.headers.get("content-type", "").lower()
                try:
                    body_bytes = decompress(raw_body, content_encoding)
                    text = body_bytes.decode("utf-8")
                    rows = _cpp_decode(text, structure) if "toon" in content_type else json.loads(text)
                    del body_bytes
                    del text
                except Exception:
                    rows = JSON_DB.get(structure, [])[:n] if JSON_DB else []

            _CACHE[key] = json.dumps(rows).encode("utf-8")
            del rows

        body = _CACHE[key]
        media = "application/json"

    elif mode == "toon_cache":
        if not hit:
            if len(raw_body) == 0:
                rows = TOON_DB.get(structure, [])[:n] if TOON_DB else []
            else:
                content_encoding = request.headers.get("content-encoding", "").lower()
                content_type = request.headers.get("content-type", "").lower()
                try:
                    body_bytes = decompress(raw_body, content_encoding)
                    text = body_bytes.decode("utf-8")
                    rows = _cpp_decode(text, structure) if "toon" in content_type else json.loads(text)
                    del body_bytes
                    del text
                except Exception:
                    rows = TOON_DB.get(structure, [])[:n] if TOON_DB else []

            _CACHE[key] = _cpp_encode(rows, structure).encode("utf-8")
            del rows

        body = _CACHE[key]
        media = "text/toon"

    else:  # canonical_cache
        cache_key = ("canonical_toon", structure, n)
        hit = cache_key in _CACHE

        if not hit:
            if len(raw_body) == 0:
                rows = TOON_DB.get(structure, [])[:n] if TOON_DB else []
            else:
                content_encoding = request.headers.get("content-encoding", "").lower()
                content_type = request.headers.get("content-type", "").lower()
                try:
                    body_bytes = decompress(raw_body, content_encoding)
                    text = body_bytes.decode("utf-8")
                    rows = _cpp_decode(text, structure) if "toon" in content_type else json.loads(text)
                    del body_bytes
                    del text
                except Exception:
                    rows = TOON_DB.get(structure, [])[:n] if TOON_DB else []

            _CACHE[cache_key] = _cpp_encode(rows, structure).encode("utf-8")
            del rows

        toon_bytes = _CACHE[cache_key]
        if format == "toon":
            body = toon_bytes
            media = "text/toon"
        else:
            rows = _cpp_decode(toon_bytes.decode("utf-8"), structure)
            body = json.dumps(rows).encode("utf-8")
            del rows
            media = "application/json"

    total_ms = (time.perf_counter() - t0) * 1000
    headers = {
        "X-Cache-Hit": "true" if hit else "false",
        "X-Total-Time-Ms": f"{total_ms:.4f}",
        "X-Bytes": str(len(body)),
    }

    del raw_body
    return Response(content=body, media_type=media, headers=headers)


@app.post("/cache/clear")
def clear_cache():
    _CACHE.clear()
    return {"cleared": True}
"""
Frontend / "requesting server" for TOON vs JSON benchmark -- v8.2 (Bidirectional + Paired Caching).

Key Updates:
  1. Fixed UI glitches: Rewrote updateLevels() to inject HTML directly to solve empty dropdowns.
  2. Restored 100-repeat options for final research measurements.
  3. Unified Cache Experiment: Cache tests run BOTH JSON and TOON in a single experiment,
     using the identical randomized pair order as the plain benchmark.
"""
import os
import gc
import random
import statistics
import time
import json
import gzip
import brotli
from datetime import datetime, timezone

import httpx
import toon_cpp
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, Response

API_BASE_URL = "https://toon-benchmark-api.onrender.com"

app = FastAPI(title="TOON vs JSON Benchmark Frontend v8")

GZIP_LEVELS = [1, 5, 9]
BROTLI_LEVELS = [1, 5, 9, 11]
MIN_SAMPLES_FOR_CI = 10

# Separate GC instrumentation. Existing benchmark timings are unchanged.
_GC_TRACKER = {"active": False, "start_ns": None, "elapsed_ns": 0}

def _gc_callback(phase, info):
    if not _GC_TRACKER["active"]:
        return
    if phase == "start":
        _GC_TRACKER["start_ns"] = time.perf_counter_ns()
    elif phase == "stop" and _GC_TRACKER["start_ns"] is not None:
        _GC_TRACKER["elapsed_ns"] += time.perf_counter_ns() - _GC_TRACKER["start_ns"]
        _GC_TRACKER["start_ns"] = None

if _gc_callback not in gc.callbacks:
    gc.callbacks.append(_gc_callback)

def _gc_tracking_start():
    _GC_TRACKER["active"] = True
    _GC_TRACKER["start_ns"] = None
    _GC_TRACKER["elapsed_ns"] = 0

def _gc_tracking_stop():
    if _GC_TRACKER["start_ns"] is not None:
        _GC_TRACKER["elapsed_ns"] += time.perf_counter_ns() - _GC_TRACKER["start_ns"]
        _GC_TRACKER["start_ns"] = None
    _GC_TRACKER["active"] = False
    return _GC_TRACKER["elapsed_ns"] / 1_000_000.0


def generate_dummy_data(structure, n):
    if structure == "flat":
        return [
            {
                "id": i,
                "name": f"Record_{i}",
                "value": i * 1.5,
                "is_active": i % 2 == 0,
                "category": "A" if i % 3 == 0 else "B"
            }
            for i in range(n)
        ]
    else:
        return [
            {
                "id": i,
                "metadata": {
                    "name": f"Record_{i}",
                    "created_at": "2026-01-01T00:00:00Z"
                },
                "metrics": [i * 1.0, i * 1.5, i * 2.0],
                "is_active": i % 2 == 0
            }
            for i in range(n)
        ]


def compute_stats(values):
    if not values:
        return {"n": 0, "mean": 0, "stdev": 0, "min": 0, "max": 0, "p50": 0, "p90": 0, "p95": 0, "p99": 0}
    n = len(values)
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    lo, hi = min(values), max(values)
    if n >= 2:
        q = statistics.quantiles(values, n=100, method="inclusive")
        def qp(p):
            idx = max(0, min(len(q) - 1, p - 1))
            return q[idx]
        p50, p90, p95, p99 = qp(50), qp(90), qp(95), qp(99)
    else:
        p50 = p90 = p95 = p99 = values[0]
    return {"n": n, "mean": round(mean, 3), "stdev": round(sd, 3), "min": round(lo, 3), "max": round(hi, 3),
            "p50": round(p50, 3), "p90": round(p90, 3), "p95": round(p95, 3), "p99": round(p99, 3)}

def bootstrap_ci(values, stat_fn, seed, n_boot=1000, ci=0.95):
    if len(values) < MIN_SAMPLES_FOR_CI:
        return None
    rnd = random.Random(seed)
    n = len(values)
    boots = []
    for _ in range(n_boot):
        resample = [values[rnd.randrange(n)] for _ in range(n)]
        boots.append(stat_fn(resample))
    boots.sort()
    lo_idx = int((1 - ci) / 2 * n_boot)
    hi_idx = int((1 + ci) / 2 * n_boot) - 1
    return {"low": round(boots[lo_idx], 3), "high": round(boots[hi_idx], 3), "ci": ci, "n_boot": n_boot}

def compute_diff(json_val, toon_val):
    abs_diff = toon_val - json_val
    rel_diff_pct = (abs_diff / json_val * 100) if json_val else 0.0
    improvement_pct = ((json_val - toon_val) / json_val * 100) if json_val else 0.0
    return {"absolute_difference": round(abs_diff, 4),
            "relative_difference_percent": round(rel_diff_pct, 2),
            "improvement_percent": round(improvement_pct, 2)}

def size_metrics(raw_bytes, compressed_bytes):
    ratio = (raw_bytes / compressed_bytes) if compressed_bytes else None
    reduction_pct = ((1 - compressed_bytes / raw_bytes) * 100) if raw_bytes else None
    return {"compression_ratio": round(ratio, 3) if ratio else None,
            "size_reduction_percent": round(reduction_pct, 2) if reduction_pct is not None else None}

def build_paired_order(repeats, seed):
    rnd = random.Random(seed)
    order = []
    pair_directions = []
    for _ in range(repeats):
        if rnd.random() < 0.5:
            order += ["json", "toon"]
            pair_directions.append("json_first")
        else:
            order += ["toon", "json"]
            pair_directions.append("toon_first")
    return order, pair_directions


def _do_request(client: httpx.Client, endpoint, fmt, structure, n, encoding, level, source_mode, raw_data, cache_mode=None):
    _gc_tracking_start()
    params = {
        "format": fmt,
        "encoding": encoding,
        "n": n,
        "structure": structure,
        "source": source_mode
    }
    
    if cache_mode:
        params["mode"] = cache_mode
    if level is not None:
        params["level"] = level

    if source_mode == "cross":
        input_fmt = "toon" if fmt == "json" else "json"
    else:
        input_fmt = fmt

    if input_fmt == "json":
        body_bytes = json.dumps(raw_data).encode("utf-8")
        content_type = "application/json"
    else:
        body_bytes = (toon_cpp.encode_flat(raw_data) if structure == "flat" else toon_cpp.encode_nested(raw_data)).encode("utf-8")
        content_type = "application/x-toon"

    headers = {
        "Content-Type": content_type,
        "Accept": "application/x-toon" if fmt == "toon" else "application/json"
    }

    if encoding == "gzip":
        body_bytes = gzip.compress(body_bytes, compresslevel=level if level else 9)
        headers["Content-Encoding"] = "gzip"
    elif encoding in ("brotli", "br"):
        body_bytes = brotli.compress(body_bytes, quality=level if level else 5)
        headers["Content-Encoding"] = "br"

    t0 = time.perf_counter()
    ts = datetime.now(timezone.utc).isoformat()
    try:
        r = client.post(f"{API_BASE_URL}{endpoint}", params=params, content=body_bytes, headers=headers)
        latency_ms = (time.perf_counter() - t0) * 1000
        frontend_gc_ms = _gc_tracking_stop()
        return {
            "timestamp": ts, "format": fmt, "latency_ms": round(latency_ms, 3),
            "frontend_gc_ms": round(frontend_gc_ms, 4),
            "cache_hit": r.headers.get("x-cache-hit") == "true",
            "request_decompression_ms": float(r.headers.get("x-request-decompression-time-ms", 0)),
            "request_deserialization_ms": float(r.headers.get("x-request-deserialization-time-ms", 0)),
            "request_decode_ms": float(r.headers.get("x-request-decode-time-ms", 0)),
            "serialization_ms": float(r.headers.get("x-serialization-time-ms", 0)),
            "utf8_encoding_ms": float(r.headers.get("x-utf8-encoding-time-ms", 0)),
            "compression_ms": float(r.headers.get("x-compression-time-ms", 0)),
            "server_processing_ms": float(r.headers.get("x-server-processing-time-ms", 0)),
            "api_gc_ms": float(r.headers.get("x-api-gc-time-ms", 0)),
            "raw_bytes": int(r.headers.get("x-raw-bytes", 0)),
            "compressed_bytes": int(r.headers.get("x-compressed-bytes", r.headers.get("x-bytes", 0))),
            "status_code": r.status_code,
            "level": r.headers.get("x-level", ""),
            "encoding": r.headers.get("x-encoding", encoding),
            "source_db": r.headers.get("x-source-db", ""),
        }
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        frontend_gc_ms = _gc_tracking_stop()
        return {"timestamp": ts, "format": fmt, "latency_ms": round(latency_ms, 3),
                "frontend_gc_ms": round(frontend_gc_ms, 4),
                "cache_hit": False,
                "request_decompression_ms": 0, "request_deserialization_ms": 0, "request_decode_ms": 0,
                "serialization_ms": 0, "utf8_encoding_ms": 0, "compression_ms": 0, "server_processing_ms": 0,
                "raw_bytes": 0, "compressed_bytes": 0, "status_code": 0, "error": str(e),
                "level": "", "encoding": encoding, "source_db": ""}


def run_plain_case(structure, n, encoding, level, repeats, warmup, seed, source_mode):
    order, pair_directions = build_paired_order(repeats, seed)
    warmup_order, _ = build_paired_order(warmup, seed + 1) if warmup > 0 else ([], [])
    raw_data = generate_dummy_data(structure, n)

    with httpx.Client(timeout=180) as client:
        for fmt in warmup_order:
            _do_request(client, "/data", fmt, structure, n, encoding, level, source_mode, raw_data)

        samples = []
        for idx, fmt in enumerate(order):
            rec = _do_request(client, "/data", fmt, structure, n, encoding, level, source_mode, raw_data)
            rec["request_index"] = idx
            samples.append(rec)

    by_fmt = {"json": [], "toon": []}
    for s in samples:
        by_fmt[s["format"]].append(s)

    results = {}
    for fmt in ["json", "toon"]:
        fs = by_fmt[fmt]
        ok = [s for s in fs if s["status_code"] == 200]
        lat = [s["latency_ms"] for s in ok]
        req_dec = [s["request_decode_ms"] for s in ok]
        req_decomp = [s["request_decompression_ms"] for s in ok]
        req_deser = [s["request_deserialization_ms"] for s in ok]
        ser = [s["serialization_ms"] for s in ok]
        utf8 = [s["utf8_encoding_ms"] for s in ok]
        comp = [s["compression_ms"] for s in ok]
        sproc = [s["server_processing_ms"] for s in ok]
        frontend_gc = [s["frontend_gc_ms"] for s in ok]
        api_gc = [s["api_gc_ms"] for s in ok]

        http_stats = compute_stats(lat)
        http_stats["ci_mean_95"] = bootstrap_ci(lat, statistics.mean, seed) if lat else None
        http_stats["ci_p50_95"] = bootstrap_ci(lat, statistics.median, seed + 1) if lat else None

        raw_b = ok[-1]["raw_bytes"] if ok else 0
        comp_b = ok[-1]["compressed_bytes"] if ok else 0

        results[fmt] = {
            "raw_bytes": raw_b, "compressed_bytes": comp_b,
            **size_metrics(raw_b, comp_b),
            "level_used": ok[-1]["level"] if ok else "",
            "http_latency": http_stats,
            "request_decompression": compute_stats(req_decomp),
            "request_deserialization": compute_stats(req_deser),
            "request_decode": compute_stats(req_dec),
            "serialization": compute_stats(ser),
            "utf8_encoding": compute_stats(utf8),
            "compression": compute_stats(comp),
            "server_processing": compute_stats(sproc),
            "frontend_gc": compute_stats(frontend_gc),
            "api_gc": compute_stats(api_gc),
            "first_measured_request_ms": fs[0]["latency_ms"] if fs else None,
            "error_count": len(fs) - len(ok),
        }

    comparison = {
        "raw_bytes": compute_diff(results["json"]["raw_bytes"], results["toon"]["raw_bytes"]),
        "compressed_bytes": compute_diff(results["json"]["compressed_bytes"], results["toon"]["compressed_bytes"]),
        "http_latency_mean": compute_diff(results["json"]["http_latency"]["mean"], results["toon"]["http_latency"]["mean"]),
        "http_latency_p50": compute_diff(results["json"]["http_latency"]["p50"], results["toon"]["http_latency"]["p50"]),
        "http_latency_p95": compute_diff(results["json"]["http_latency"]["p95"], results["toon"]["http_latency"]["p95"]),
        "request_decode_mean": compute_diff(results["json"]["request_decode"]["mean"], results["toon"]["request_decode"]["mean"]),
        "serialization_mean": compute_diff(results["json"]["serialization"]["mean"], results["toon"]["serialization"]["mean"]),
        "compression_mean": compute_diff(results["json"]["compression"]["mean"], results["toon"]["compression"]["mean"]),
    }

    return {"order": order, "pair_directions": pair_directions, "warmup_order": warmup_order,
            "samples": samples, "results": results, "comparison": comparison}


def run_cache_case(mode, structure, n, repeats, warmup, seed):
    raw_data = generate_dummy_data(structure, n)

    with httpx.Client(timeout=180) as client:
        cache_miss_latency_ms = {}
        cache_miss_was_actually_miss = {}

        # 1. Equivalent Miss Treatment - Clear and run JSON
        client.post(f"{API_BASE_URL}/cache/clear")
        json_mode = "canonical_cache" if mode == "canonical_cache" else "json_cache"
        miss_json = _do_request(client, "/cache/data", "json", structure, n, "identity", None, "native", raw_data, cache_mode=json_mode)
        cache_miss_latency_ms["json"] = miss_json["latency_ms"]
        cache_miss_was_actually_miss["json"] = not miss_json.get("cache_hit", False)

        # 2. Equivalent Miss Treatment - Clear and run TOON
        client.post(f"{API_BASE_URL}/cache/clear")
        toon_mode = "canonical_cache" if mode == "canonical_cache" else "toon_cache"
        miss_toon = _do_request(client, "/cache/data", "toon", structure, n, "identity", None, "native", raw_data, cache_mode=toon_mode)
        cache_miss_latency_ms["toon"] = miss_toon["latency_ms"]
        cache_miss_was_actually_miss["toon"] = not miss_toon.get("cache_hit", False)

        # 3. Warm-up (Randomized pairs)
        client.post(f"{API_BASE_URL}/cache/clear")
        rnd = random.Random(seed)
        warmup_order, _ = build_paired_order(warmup, seed + 1) if warmup > 0 else ([], [])
        for fmt in warmup_order:
            fmt_mode = "canonical_cache" if mode == "canonical_cache" else f"{fmt}_cache"
            _do_request(client, "/cache/data", fmt, structure, n, "identity", None, "native", raw_data, cache_mode=fmt_mode)

        # 4. Measured Pairs (Exactly matching the format x compression pairing strategy)
        order, pair_directions = build_paired_order(repeats, seed)
        samples = []
        any_miss = False
        
        for idx, fmt in enumerate(order):
            fmt_mode = "canonical_cache" if mode == "canonical_cache" else f"{fmt}_cache"
            rec = _do_request(client, "/cache/data", fmt, structure, n, "identity", None, "native", raw_data, cache_mode=fmt_mode)
            rec["request_index"] = idx
            
            if not rec.get("cache_hit", True):
                any_miss = True
            samples.append(rec)

    if any_miss:
        return {
            "failed": True,
            "reason": "One or more measured requests was a cache MISS. Cache was either cleared externally or never warmed correctly.",
            "samples": samples
        }

    results = {}
    for fmt in ["json", "toon"]:
        fs = [s for s in samples if s["format"] == fmt]
        lat = [s["latency_ms"] for s in fs]
        results[fmt] = {
            "bytes": fs[-1]["compressed_bytes"] if fs else 0,
            "cache_hit_rate_pct": 100.0,
            "latency": compute_stats(lat),
        }
    
    comparison = {
        "cache_miss_latency": compute_diff(cache_miss_latency_ms["json"], cache_miss_latency_ms["toon"]),
        "bytes": compute_diff(results["json"]["bytes"], results["toon"]["bytes"]),
        "warm_cache_latency_mean": compute_diff(results["json"]["latency"]["mean"], results["toon"]["latency"]["mean"]),
        "warm_cache_latency_p50": compute_diff(results["json"]["latency"]["p50"], results["toon"]["latency"]["p50"]),
        "warm_cache_latency_p95": compute_diff(results["json"]["latency"]["p95"], results["toon"]["latency"]["p95"]),
    }

    return {
        "failed": False,
        "cache_miss_latency_ms": cache_miss_latency_ms,
        "cache_miss_was_actually_miss": cache_miss_was_actually_miss,
        "warmup_order": warmup_order,
        "order": order,
        "pair_directions": pair_directions,
        "samples": samples,
        "results": results,
        "comparison": comparison
    }


@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/api-base")
def api_base(): return {"api_base_url": API_BASE_URL}

@app.get("/run")
def run(case_type: str = Query("plain"), structure: str = Query("flat"), n: int = Query(1000),
        encoding: str = Query("identity"), level: int = Query(None), mode: str = Query("native"),
        repeats: int = Query(15), warmup: int = Query(3), seed: int = Query(42),
        source_mode: str = Query("native"), trials: int = Query(1)):

    trials = max(1, min(trials, 5)) 
    timestamp = datetime.now(timezone.utc).isoformat()
    base_experiment_id = f"{timestamp.replace(':', '').replace('-', '').split('.')[0]}Z_{structure}_{n}_{encoding}{level or ''}_{repeats}"

    try:
        if case_type == "cache":
            data = run_cache_case(mode, structure, n, repeats, warmup, seed)
            return {
                "experiment_id": base_experiment_id, "timestamp": timestamp,
                "case_type": case_type, "structure": structure, "n": n, "mode": mode,
                "repeats": repeats, "warmup": warmup, "seed": seed,
                "api_base_url": API_BASE_URL, "preliminary": repeats < 30,
                **data,
            }

        trial_results = []
        for t in range(trials):
            trial_seed = seed + t * 1000 
            trial_data = run_plain_case(structure, n, encoding, level, repeats, warmup, trial_seed, source_mode)
            trial_results.append({"trial_index": t, "seed": trial_seed, **trial_data})

        combined = None
        if trials > 1:
            pooled_json = [s["latency_ms"] for tr in trial_results for s in tr["samples"]
                           if s["format"] == "json" and s["status_code"] == 200]
            pooled_toon = [s["latency_ms"] for tr in trial_results for s in tr["samples"]
                           if s["format"] == "toon" and s["status_code"] == 200]
            combined = {"json_http_latency": compute_stats(pooled_json),
                        "toon_http_latency": compute_stats(pooled_toon)}

        return {
            "experiment_id": base_experiment_id, "timestamp": timestamp,
            "case_type": case_type, "structure": structure, "n": n, "encoding": encoding, "level": level,
            "repeats": repeats, "warmup": warmup, "seed": seed, "source_mode": source_mode, "trials": trials,
            "api_base_url": API_BASE_URL,
            "percentile_method": "statistics.quantiles(n=100, method='inclusive'), linear interpolation",
            "ci_method": f"percentile bootstrap, 1000 resamples, 95% CI, skipped below n={MIN_SAMPLES_FOR_CI}",
            "preliminary": repeats < 30,
            "trial_results": trial_results,
            "combined_across_trials": combined,
            **{k: v for k, v in trial_results[0].items() if k not in ("trial_index", "seed")},
        }
    except httpx.ConnectError as e:
        return {"error": "connect_error", "message": f"Could not reach API at {API_BASE_URL}: {e}"}
    except httpx.TimeoutException as e:
        return {"error": "timeout", "message": f"Request to {API_BASE_URL} timed out: {e}"}
    except Exception as e:
        return {"error": "unexpected", "message": f"{type(e).__name__}: {e}", "api_base_url": API_BASE_URL}


@app.get("/run-research")
def run_research(repeats: int = Query(15), warmup: int = Query(3), seed: int = Query(42)):
    """Restored Research Mode: 1 Iteration executes the full pairing for JSON/TOON per matrix element."""
    if repeats not in (15, 30, 100):
        repeats = 15

    matrix = []
    for structure in ["flat", "nested"]:
        for n in [1000, 10000]:
            # Generate the plain benchmark conditions
            matrix.append({"type": "plain", "structure": structure, "n": n, "encoding": "identity", "level": None})
            for lvl in GZIP_LEVELS:
                matrix.append({"type": "plain", "structure": structure, "n": n, "encoding": "gzip", "level": lvl})
            for lvl in BROTLI_LEVELS:
                matrix.append({"type": "plain", "structure": structure, "n": n, "encoding": "brotli", "level": lvl})
            
            # Generate the embedded cache experiments (each runs a JSON+TOON pair test)
            matrix.append({"type": "cache", "mode": "native", "structure": structure, "n": n})
            matrix.append({"type": "cache", "mode": "canonical_cache", "structure": structure, "n": n})

    run_id = f"research_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    all_results = []
    for case in matrix:
        try:
            if case["type"] == "plain":
                res = run_plain_case(case["structure"], case["n"], case["encoding"], case["level"], repeats, warmup, seed, "native")
                all_results.append({**case, **res})
            else:
                res = run_cache_case(case["mode"], case["structure"], case["n"], repeats, warmup, seed)
                all_results.append({**case, **res})
        except Exception as e:
            all_results.append({**case, "error": str(e)})

    return {"research_run_id": run_id, "matrix_size": len(matrix), "repeats": repeats,
            "warmup": warmup, "seed": seed, "cases": all_results,
            "note": "Exploration tool. For final research data, run cells individually via /run"}


INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TOON vs JSON Benchmark</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 1050px; margin: 40px auto; padding: 0 20px; color: #222; }
  h1 { font-size: 22px; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: flex-end; margin: 20px 0; }
  .field { display: flex; flex-direction: column; gap: 4px; flex: 0 0 auto; }
  label { font-size: 12px; color: #666; }
  select, button, input {
    padding: 7px 9px;
    font-size: 13px;
    border-radius: 6px;
    border: 1px solid #ccc;
    background: #fff;
    box-sizing: border-box;
    height: 35px;
  }
  select { min-width: 72px; }
  #level { width: 72px; min-width: 72px; }
  #encoding { min-width: 88px; }
  #sourceMode { min-width: 180px; }
  #repeats { min-width: 145px; }
  #runBtn { align-self: flex-end; }
  button { background: #222; color: white; border: none; cursor: pointer; }
  button:disabled { background: #999; }
  table { border-collapse: collapse; width: 100%; margin-top: 16px; font-size: 12.5px; }
  th, td { border: 1px solid #ddd; padding: 5px 8px; text-align: center; }
  th { background: #f5f5f5; }
  #status { color: #666; font-size: 13px; margin-top: 10px; }
  .warn { color: #a33; font-weight: 600; }
  .note { color: #888; font-size: 12px; margin: 6px 0; }
  .export-btns { margin-top: 10px; display: flex; gap: 8px; }
  .fail-box { background: #fee; border: 1px solid #c33; padding: 10px; border-radius: 6px; }
</style>
</head>
<body>
<h1>TOON vs JSON Benchmark</h1>
<p style="color:#666">Requests are fired server-side against the API service in randomized PAIRS (JSON→TOON or TOON→JSON per pair, direction seeded), using one persistent connection for the whole run.</p>
<details><summary>Methodology / terminology</summary><div class="note" style="margin-top:8px">JSON and TOON are measured in seeded randomized pairs. Warm-up requests are excluded from measured statistics. Cache misses are populated separately and excluded from warm-cache statistics.</div></details>

<div class="controls">
  <div class="field">
    <label>Case type</label>
    <select id="caseType">
      <option value="plain">Format x compression</option>
      <option value="cache">Cache layer</option>
      <option value="research">Research mode</option>
    </select>
  </div>
  <div class="field" id="structureField">
    <label>Structure</label>
    <select id="structure"><option value="flat">flat</option><option value="nested">nested</option></select>
  </div>
  <div class="field" id="nField">
    <label>n (records)</label>
    <select id="n"><option value="100">100</option><option value="1000">1000</option><option value="10000" selected>10000</option><option value="100000">100000</option></select>
  </div>
  <div class="field" id="encodingField">
    <label>Compression</label>
    <select id="encoding">
      <option value="identity">none</option>
      <option value="gzip">gzip</option>
      <option value="brotli">brotli</option>
    </select>
  </div>
  <div class="field" id="levelField">
    <label>Level</label>
    <select id="level"><option value="1">1</option><option value="5" selected>5</option><option value="9">9</option><option value="11">11</option></select>
  </div>
  <div class="field" id="sourceModeField">
    <label>Database source</label>
    <select id="sourceMode">
      <option value="native">Native (primary)</option>
      <option value="cross">Cross (translation/conversion)</option>
    </select>
  </div>
  <div class="field" id="modeField" style="display:none">
    <label>Cache mode</label>
    <select id="mode">
      <option value="native">Native caches (JSON/TOON)</option>
      <option value="canonical_cache">Canonical cache</option>
    </select>
  </div>
  <div class="field" id="warmupField">
    <label>Warm-up</label>
    <select id="warmup"><option value="0">0</option><option value="2">2</option><option value="3" selected>3</option></select>
  </div>
  <div class="field" id="repeatsField">
    <label>Repeats</label>
    <select id="repeats">
      <option value="5">5 (quick test)</option>
      <option value="15" selected>15</option>
      <option value="30">30</option>
      <option value="100">100 (final research)</option>
    </select>
  </div>
  <div class="field" id="trialsField">
    <label>Trials</label>
    <select id="trials"><option value="1" selected>1</option><option value="3">3</option></select>
  </div>
  
  <div class="field" id="researchRepeatsField" style="display:none">
    <label>Repeats (research)</label>
    <select id="researchRepeats">
      <option value="15" selected>15</option>
      <option value="30">30</option>
      <option value="100">100</option>
    </select>
  </div>
  <div class="field">
    <label>Seed</label>
    <input id="seed" type="number" value="42" style="width:60px">
  </div>
  <button id="runBtn" type="button">Run</button>
</div>
<p class="note" id="researchWarning" style="display:none">Research mode is an EXPLORATION tool, not for final data collection. Iterates through all combinations.</p>

<div id="status"></div>
<div id="resultsArea"></div>

<script>

let lastData = null;

function updateLevels() {
  const encoding = document.getElementById('encoding');
  const levelField = document.getElementById('levelField');
  const level = document.getElementById('level');
  if (!encoding || !levelField || !level) return;

  const levels =
    encoding.value === 'gzip' ? [1, 5, 9] :
    encoding.value === 'brotli' ? [1, 5, 9, 11] :
    [];

  const previous = level.value;
  level.replaceChildren();

  for (const value of levels) {
    const option = document.createElement('option');
    option.value = String(value);
    option.textContent = String(value);
    level.appendChild(option);
  }

  if (levels.length) {
    level.value = levels.includes(Number(previous)) ? previous : '5';
    if (!level.value) level.value = String(levels[0]);
    levelField.style.display = 'flex';
  } else {
    levelField.style.display = 'none';
  }
}

function toggleFields() {
  const ct = document.getElementById('caseType').value;
  const isCache = ct === 'cache';
  const isResearch = ct === 'research';
  const enc = document.getElementById('encoding').value;

  document.getElementById('encodingField').style.display =
    (isCache || isResearch) ? 'none' : 'flex';
  document.getElementById('sourceModeField').style.display =
    isCache ? 'none' : 'flex';
  document.getElementById('modeField').style.display =
    isCache ? 'flex' : 'none';

  const levelField = document.getElementById('levelField');
  levelField.style.display =
    (!isCache && !isResearch && enc !== 'identity') ? 'flex' : 'none';

  document.getElementById('nField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('structureField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('warmupField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('repeatsField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('trialsField').style.display =
    (isCache || isResearch) ? 'none' : 'flex';
  document.getElementById('researchRepeatsField').style.display =
    isResearch ? 'flex' : 'none';
  document.getElementById('researchWarning').style.display =
    isResearch ? 'block' : 'none';
}

async function runCase() {
  const btn = document.getElementById('runBtn');
  const status = document.getElementById('status');
  const ct = document.getElementById('caseType').value;

  btn.disabled = true;
  status.textContent = 'Running...';
  document.getElementById('resultsArea').innerHTML =
    '<p class="note">Benchmark is running. Please wait...</p>';

  let url;

  if (ct === 'research') {
    const params = new URLSearchParams({
      repeats: document.getElementById('researchRepeats').value,
      seed: document.getElementById('seed').value,
    });
    url = '/run-research?' + params.toString();
  } else {
    const params = new URLSearchParams({
      case_type: ct,
      structure: document.getElementById('structure').value,
      n: document.getElementById('n').value,
      encoding: document.getElementById('encoding').value,
      mode: document.getElementById('mode').value,
      repeats: document.getElementById('repeats').value,
      warmup: document.getElementById('warmup').value,
      seed: document.getElementById('seed').value,
      source_mode: document.getElementById('sourceMode').value,
      trials: document.getElementById('trials').value,
    });

    const level = document.getElementById('level').value;
    if (level && document.getElementById('levelField').style.display !== 'none') {
      params.set('level', level);
    }

    url = '/run?' + params.toString();
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 300000);

  try {
    console.log('Starting benchmark:', url);

    const res = await fetch(url, {
      method: 'GET',
      cache: 'no-store',
      signal: controller.signal,
      headers: { 'Accept': 'application/json' }
    });

    const text = await res.text();

    if (!res.ok) {
      throw new Error(`HTTP ${res.status}: ${text.slice(0, 1000)}`);
    }

    let data;
    try {
      data = JSON.parse(text);
    } catch (parseError) {
      throw new Error(
        `Server returned non-JSON response: ${text.slice(0, 1000)}`
      );
    }

    if (data.error) {
      throw new Error(data.message || data.error);
    }

    lastData = data;
    render(data);
    status.textContent = 'Done.';

  } catch (e) {
    console.error('Benchmark request failed:', e);

    const message =
      e.name === 'AbortError'
        ? 'Request timed out after 5 minutes. Check API_BASE_URL and API service logs.'
        : e.message;

    status.textContent = 'Error: ' + message;
    document.getElementById('resultsArea').innerHTML =
      `<div class="fail-box"><b>Request failed:</b> ${message}</div>`;
  } finally {
    clearTimeout(timeout);
    btn.disabled = false;
  }
}

function statsRow(label, jsonV, toonV, diff) {
  return `<tr><td>${label}</td><td>${jsonV}</td><td>${toonV}</td>` +
         `<td>${diff ? diff.absolute_difference : ''}</td>` +
         `<td>${diff ? diff.improvement_percent + '%' : ''}</td></tr>`;
}

function render(data) {
  if (data.research_run_id) {
    renderResearch(data);
    return;
  }

  if (data.case_type === 'cache') {
    if (data.failed) {
        document.getElementById('resultsArea').innerHTML = `<div class="fail-box"><b>Cache experiment failed:</b> ${data.reason}</div>`;
        return;
    }
    const r = data.results, c = data.comparison;
    let html = `<p><b>Case:</b> structure=${data.structure}, n=${data.n}, cache_mode=${data.mode}, repeats=${data.repeats}, warmup=${data.warmup}, seed=${data.seed}</p>`;
    html += '<table><tr><th>Metric</th><th>JSON</th><th>TOON</th><th>Abs diff (TOON-JSON)</th><th>Improvement (TOON vs JSON)</th></tr>';
    html += statsRow('cache miss latency ms', data.cache_miss_latency_ms.json, data.cache_miss_latency_ms.toon, c.cache_miss_latency);
    html += statsRow('cache hit rate %', r.json.cache_hit_rate_pct, r.toon.cache_hit_rate_pct, null);
    html += statsRow('bytes', r.json.bytes, r.toon.bytes, c.bytes);
    html += statsRow('warm-cache latency mean ms', r.json.latency.mean, r.toon.latency.mean, c.warm_cache_latency_mean);
    html += statsRow('warm-cache latency p50 ms', r.json.latency.p50, r.toon.latency.p50, c.warm_cache_latency_p50);
    html += statsRow('warm-cache latency p90 ms', r.json.latency.p90, r.toon.latency.p90, null);
    html += statsRow('warm-cache latency p95 ms', r.json.latency.p95, r.toon.latency.p95, c.warm_cache_latency_p95);
    html += statsRow('warm-cache latency p99 ms', r.json.latency.p99, r.toon.latency.p99, null);
    html += statsRow('warm-cache latency stdev ms', r.json.latency.stdev, r.toon.latency.stdev, null);
    html += statsRow('warm-cache latency min/max ms', r.json.latency.min + ' / ' + r.json.latency.max, r.toon.latency.min + ' / ' + r.toon.latency.max, null);
    html += statsRow('sample count', r.json.latency.n, r.toon.latency.n, null);
    html += '</table>';
    html += `<div class="export-btns"><button onclick="exportJson()">Download JSON</button><button onclick="exportCsv()">Download CSV (samples)</button></div>`;
    document.getElementById('resultsArea').innerHTML = html;
    return;
  }

  // Fallback to normal rendering logic for format x compression
  const r = data.results, c = data.comparison;
  let html = `<p><b>Case:</b> structure=${data.structure}, n=${data.n}, encoding=${data.encoding}${data.level ? ' level='+data.level : ''}, ` +
             `repeats=${data.repeats}, warmup=${data.warmup}, seed=${data.seed}, source=${data.source_mode}, trials=${data.trials}</p>`;
  html += '<table><tr><th>Metric</th><th>JSON</th><th>TOON</th><th>Abs diff</th><th>Improvement</th></tr>';
  html += statsRow('raw_bytes', r.json.raw_bytes, r.toon.raw_bytes, c.raw_bytes);
  html += statsRow('compressed_bytes', r.json.compressed_bytes, r.toon.compressed_bytes, c.compressed_bytes);
  html += statsRow('frontend_gc mean_ms', r.json.frontend_gc.mean, r.toon.frontend_gc.mean,
                    compute_diff(r.json.frontend_gc.mean, r.toon.frontend_gc.mean));
  html += statsRow('api_gc mean_ms', r.json.api_gc.mean, r.toon.api_gc.mean,
                    compute_diff(r.json.api_gc.mean, r.toon.api_gc.mean));
  html += statsRow('request_decompression mean_ms', r.json.request_decompression.mean, r.toon.request_decompression.mean, null);
  html += statsRow('request_deserialization mean_ms', r.json.request_deserialization.mean, r.toon.request_deserialization.mean, null);
  html += statsRow('request_decode mean_ms', r.json.request_decode.mean, r.toon.request_decode.mean, c.request_decode_mean);
  html += statsRow('serialization mean_ms', r.json.serialization.mean, r.toon.serialization.mean, c.serialization_mean);
  html += statsRow('compression mean_ms', r.json.compression.mean, r.toon.compression.mean, c.compression_mean);
  html += statsRow('server_processing mean_ms', r.json.server_processing.mean, r.toon.server_processing.mean, null);
  html += statsRow('http_latency MEAN_ms', r.json.http_latency.mean, r.toon.http_latency.mean, c.http_latency_mean);
  html += statsRow('http_latency p50_ms', r.json.http_latency.p50, r.toon.http_latency.p50, c.http_latency_p50);
  html += statsRow('http_latency p95_ms', r.json.http_latency.p95, r.toon.http_latency.p95, c.http_latency_p95);
  html += '</table>';
  html += `<div class="export-btns">
    <button onclick="exportJson()">Download JSON</button>
    <button onclick="exportCsv()">Download CSV (samples)</button>
  </div>`;
  document.getElementById('resultsArea').innerHTML = html;
}

function renderResearch(data) {
  let html = `<p><b>Research run:</b> ${data.research_run_id} -- ${data.matrix_size} cases, repeats=${data.repeats}, warmup=${data.warmup}</p>`;
  html += '<table><tr><th>Type</th><th>Structure</th><th>n</th><th>Encoding/Mode</th><th>Level</th>' +
          '<th>JSON p50 ms</th><th>TOON p50 ms</th></tr>';
  for (const c of data.cases) {
    if (c.error || c.failed) {
      html += `<tr><td>${c.type}</td><td>${c.structure}</td><td>${c.n}</td><td colspan="4" class="warn">${c.error || c.reason}</td></tr>`;
      continue;
    }
    const r = c.results;
    let enc_mode = c.type === 'cache' ? c.mode : c.encoding;
    let lvl = c.level || '';
    let jp50 = c.type === 'cache' ? r.json.latency.p50 : r.json.http_latency.p50;
    let tp50 = c.type === 'cache' ? r.toon.latency.p50 : r.toon.http_latency.p50;
    
    html += `<tr><td>${c.type}</td><td>${c.structure}</td><td>${c.n}</td><td>${enc_mode}</td><td>${lvl}</td>` +
            `<td>${jp50}</td><td>${tp50}</td></tr>`;
  }
  html += '</table>';
  html += `<div class="export-btns"><button onclick="exportJson()">Download full JSON</button></div>`;
  document.getElementById('resultsArea').innerHTML = html;
}


function exportJson() {
  if (!lastData) return;
  const blob = new Blob([JSON.stringify(lastData, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (lastData.experiment_id || lastData.research_run_id || 'results') + '.json';
  a.click();
}

function exportCsv() {
  if (!lastData) return;
  const cols = ['trial','experiment_id','timestamp','structure','n','format','encoding','level',
                'request_index','frontend_gc_ms','api_gc_ms','request_decode_ms','request_decompression_ms','request_deserialization_ms',
                'serialization_ms','utf8_encoding_ms','compression_ms','server_processing_ms',
                'http_latency_ms','raw_bytes','compressed_bytes','status_code','source_db'];
  let csv = cols.join(',') + '\\n';
  const trialList = lastData.trial_results || [{trial_index: 0, samples: lastData.samples}];
  for (const tr of trialList) {
    if (!tr.samples) continue; 
    for (const s of tr.samples) {
      const row = {
        trial: tr.trial_index, experiment_id: lastData.experiment_id, timestamp: s.timestamp,
        structure: lastData.structure, n: lastData.n, format: s.format, encoding: s.encoding,
        level: s.level, request_index: s.request_index, frontend_gc_ms: s.frontend_gc_ms, api_gc_ms: s.api_gc_ms, request_decode_ms: s.request_decode_ms,
        request_decompression_ms: s.request_decompression_ms, request_deserialization_ms: s.request_deserialization_ms,
        serialization_ms: s.serialization_ms, utf8_encoding_ms: s.utf8_encoding_ms,
        compression_ms: s.compression_ms, server_processing_ms: s.server_processing_ms,
        http_latency_ms: s.latency_ms, raw_bytes: s.raw_bytes, compressed_bytes: s.compressed_bytes,
        status_code: s.status_code, source_db: s.source_db,
      };
      csv += cols.map(c => row[c] !== undefined ? row[c] : '').join(',') + '\\n';
    }
  }
  const blob = new Blob([csv], {type: 'text/csv'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = (lastData.experiment_id || 'results') + '_samples.csv';
  a.click();
}

document.addEventListener('DOMContentLoaded', () => {
  const caseType = document.getElementById('caseType');
  const encoding = document.getElementById('encoding');
  const runBtn = document.getElementById('runBtn');

  caseType.addEventListener('change', toggleFields);
  encoding.addEventListener('change', () => {
    updateLevels();
    toggleFields();
  });
  runBtn.addEventListener('click', runCase);

  updateLevels();
  toggleFields();
});

</script>
</body>
</html>
"""

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
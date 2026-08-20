"""
Frontend / "requesting server" for the TOON vs JSON benchmark -- v4.

Changes from v3 (per the Final Improvement Requirements doc):
  1. PAIRED randomization: each repetition is a pair (JSON,TOON) or
     (TOON,JSON), direction chosen randomly per pair via a seeded RNG.
     Keeps JSON/TOON measurements close in time while still preventing
     systematic ordering bias -- replaces v3's fully independent shuffle.
  2. Every sample now carries the full 4-phase server timing breakdown
     (data_selection_ms, serialization_ms, utf8_encoding_ms, compression_ms,
     server_processing_ms) plus a per-sample timestamp.
  3. "cold_start_first_request_ms" renamed to "first_measured_request_ms"
     (after warmup, it is not a true cold-start figure).
  4. Cache experiments now strictly separate cache-miss from warm-cache:
     clear -> 1 populate request (discarded, reported as cache_miss_latency_ms)
     -> warmup (discarded) -> measured requests, each asserted X-Cache-Hit=true.
     If any measured request is a miss, the experiment is marked failed rather
     than silently reporting contaminated numbers.
  5. Independent trials: repeat an entire measurement N times (different seeds)
     to check reproducibility -- returned as a list, plus a combined pooled result.
  6. Lightweight bootstrap confidence intervals (95%) for mean and P50 latency,
     stdlib-only, skipped (reported as null) below a minimum sample size.
  7. compression_ratio and size_reduction_percent reported per format, with
     raw-size reduction and compressed-size reduction always kept distinct.
  8. CSV export matches the exact column list from the requirements doc.
  9. Payload sizes now include 100 and 100,000 (databases regenerated to
     100,000 records to support this).

Env vars:
    API_BASE_URL   -- base URL of the deployed api/ service
"""
import os
import random
import statistics
import time
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:9950")
if API_BASE_URL and not API_BASE_URL.startswith("http"):
    API_BASE_URL = f"https://{API_BASE_URL}"

app = FastAPI(title="TOON vs JSON Benchmark Frontend v4")

GZIP_LEVELS = [1, 5, 9]
BROTLI_LEVELS = [1, 5, 9, 11]
MIN_SAMPLES_FOR_CI = 10  # below this, CI is reported as null rather than misleadingly precise


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api-base")
def api_base():
    return {"api_base_url": API_BASE_URL}


@app.get("/debug-connection")
def debug_connection():
    try:
        with httpx.Client(timeout=60) as client:
            r = client.get(f"{API_BASE_URL}/health")
        return {"api_base_url": API_BASE_URL, "reachable": True, "status_code": r.status_code, "body": r.text}
    except httpx.ConnectError as e:
        return {"api_base_url": API_BASE_URL, "reachable": False, "error": "connect_error", "detail": str(e)}
    except httpx.TimeoutException as e:
        return {"api_base_url": API_BASE_URL, "reachable": False, "error": "timeout", "detail": str(e)}
    except Exception as e:
        return {"api_base_url": API_BASE_URL, "reachable": False, "error": type(e).__name__, "detail": str(e)}


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def compute_stats(values):
    """Percentile method: statistics.quantiles(values, n=100, method='inclusive'),
    linear interpolation. Unchanged from v3 -- kept as the documented standard."""
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
    """Percentile bootstrap CI, stdlib-only. Returns None below MIN_SAMPLES_FOR_CI
    rather than reporting a CI that would overclaim precision from a tiny sample."""
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
    """absolute_difference = TOON - JSON. improvement_percent is positive when
    TOON is LOWER (better for a cost metric like latency or bytes)."""
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


# ---------------------------------------------------------------------------
# Paired randomization (requirement #3)
# ---------------------------------------------------------------------------

def build_paired_order(repeats, seed):
    """For each of `repeats` pairs, randomly choose (json,toon) or (toon,json)
    as the pair direction. Returns the flat request sequence AND the pair
    directions separately, both recorded in output for reproducibility."""
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


def _do_request(client: httpx.Client, fmt, structure, n, encoding, level, source_mode):
    params = {
        "format": fmt,
        "encoding": encoding,
        "n": n,
        "structure": structure
    }

    if level is not None:
        params["level"] = level
    if source_mode == "cross":
        params["source"] = "toon_db" if fmt == "json" else "json_db"

    t0 = time.perf_counter()
    ts = datetime.now(timezone.utc).isoformat()
    try:
        r = client.get(f"{API_BASE_URL}/data", params=params)
        latency_ms = (time.perf_counter() - t0) * 1000
        return {
            "timestamp": ts, "format": fmt, "latency_ms": round(latency_ms, 3),
            "data_selection_ms": float(r.headers.get("x-data-selection-time-ms", 0)),
            "serialization_ms": float(r.headers.get("x-serialization-time-ms", 0)),
            "utf8_encoding_ms": float(r.headers.get("x-utf8-encoding-time-ms", 0)),
            "compression_ms": float(r.headers.get("x-compression-time-ms", 0)),
            "server_processing_ms": float(r.headers.get("x-server-processing-time-ms", 0)),
            "raw_bytes": int(r.headers.get("x-raw-bytes", 0)),
            "compressed_bytes": int(r.headers.get("x-compressed-bytes", 0)),
            "status_code": r.status_code,
            "level": r.headers.get("x-level", ""),
            "encoding": r.headers.get("x-encoding", encoding),
            "source_db": r.headers.get("x-source-db", ""),
        }
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        return {"timestamp": ts, "format": fmt, "latency_ms": round(latency_ms, 3),
                "data_selection_ms": 0, "serialization_ms": 0, "utf8_encoding_ms": 0,
                "compression_ms": 0, "server_processing_ms": 0, "raw_bytes": 0, "compressed_bytes": 0,
                "status_code": 0, "error": str(e), "level": "", "encoding": encoding, "source_db": ""}


def run_plain_case(structure, n, encoding, level, repeats, warmup, seed, source_mode):
    order, pair_directions = build_paired_order(repeats, seed)
    warmup_order, _ = build_paired_order(warmup, seed + 1) if warmup > 0 else ([], [])

    with httpx.Client(timeout=180) as client:  # ONE persistent client for the whole run
        for fmt in warmup_order:
            _do_request(client, fmt, structure, n, encoding, level, source_mode)  # discarded

        samples = []
        for idx, fmt in enumerate(order):
            rec = _do_request(client, fmt, structure, n, encoding, level, source_mode)
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
        dsel = [s["data_selection_ms"] for s in ok]
        ser = [s["serialization_ms"] for s in ok]
        utf8 = [s["utf8_encoding_ms"] for s in ok]
        comp = [s["compression_ms"] for s in ok]
        sproc = [s["server_processing_ms"] for s in ok]

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
            "data_selection": compute_stats(dsel),
            "serialization": compute_stats(ser),
            "utf8_encoding": compute_stats(utf8),
            "compression": compute_stats(comp),
            "server_processing": compute_stats(sproc),
            "first_measured_request_ms": fs[0]["latency_ms"] if fs else None,
            "error_count": len(fs) - len(ok),
        }

    comparison = {
        "raw_bytes": compute_diff(results["json"]["raw_bytes"], results["toon"]["raw_bytes"]),
        "compressed_bytes": compute_diff(results["json"]["compressed_bytes"], results["toon"]["compressed_bytes"]),
        "http_latency_mean": compute_diff(results["json"]["http_latency"]["mean"], results["toon"]["http_latency"]["mean"]),
        "http_latency_p50": compute_diff(results["json"]["http_latency"]["p50"], results["toon"]["http_latency"]["p50"]),
        "http_latency_p95": compute_diff(results["json"]["http_latency"]["p95"], results["toon"]["http_latency"]["p95"]),
        "serialization_mean": compute_diff(results["json"]["serialization"]["mean"], results["toon"]["serialization"]["mean"]),
        "compression_mean": compute_diff(results["json"]["compression"]["mean"], results["toon"]["compression"]["mean"]),
    }

    return {"order": order, "pair_directions": pair_directions, "warmup_order": warmup_order,
            "samples": samples, "results": results, "comparison": comparison}


# ---------------------------------------------------------------------------
# Cache experiments -- strict cache-miss / warm-cache separation (req #31)
# ---------------------------------------------------------------------------

def run_cache_case(mode, structure, n, repeats, warmup, seed):
    formats_to_test = ["json", "toon"] if mode == "canonical_cache" else \
                       (["json"] if mode == "json_cache" else ["toon"])

    with httpx.Client(timeout=180) as client:
        # 1. Clear cache
        client.post(f"{API_BASE_URL}/cache/clear")

        # 2. ONE populate request (discarded from stats, reported as cache_miss_latency_ms).
        #    For canonical_cache, populating with "toon" warms the shared canonical entry,
        #    which both json and toon reads then hit.
        populate_fmt = "toon" if mode == "canonical_cache" else formats_to_test[0]
        t0 = time.perf_counter()
        miss_r = client.get(f"{API_BASE_URL}/cache/data",
                             params={"mode": mode, "format": populate_fmt, "n": n, "structure": structure})
        cache_miss_latency_ms = round((time.perf_counter() - t0) * 1000, 3)
        cache_miss_was_actually_miss = miss_r.headers.get("x-cache-hit") == "false"

        # 3. Warmup (discarded) -- interleaved across formats_to_test if canonical_cache
        rnd = random.Random(seed)
        warmup_order = []
        for fmt in formats_to_test:
            warmup_order += [fmt] * warmup
        rnd.shuffle(warmup_order)
        for fmt in warmup_order:
            client.get(f"{API_BASE_URL}/cache/data",
                       params={"mode": mode, "format": fmt, "n": n, "structure": structure})

        # 4. Measured requests -- EVERY one must be a cache hit. Fail the
        #    experiment (not just warn) if any measured request is a miss.
        order = []
        for fmt in formats_to_test:
            order += [fmt] * repeats
        rnd.shuffle(order)

        samples = []
        any_miss = False
        for idx, fmt in enumerate(order):
            t0 = time.perf_counter()
            r = client.get(f"{API_BASE_URL}/cache/data",
                            params={"mode": mode, "format": fmt, "n": n, "structure": structure})
            latency_ms = round((time.perf_counter() - t0) * 1000, 3)
            is_hit = r.headers.get("x-cache-hit") == "true"
            if not is_hit:
                any_miss = True
            samples.append({
                "request_index": idx, "format": fmt, "latency_ms": latency_ms,
                "cache_hit": is_hit, "bytes": int(r.headers.get("x-bytes", 0)), "status_code": r.status_code,
            })

    if any_miss:
        return {
            "failed": True,
            "reason": "One or more measured requests was a cache MISS (X-Cache-Hit=false). "
                      "Per the warm-cache experimental protocol, all measured requests must be "
                      "hits; a miss here indicates the cache was cleared, evicted, or never "
                      "warmed correctly. Results are NOT reported to avoid contaminated numbers.",
            "cache_miss_latency_ms": cache_miss_latency_ms,
            "samples": samples,
        }

    results = {}
    for fmt in formats_to_test:
        fs = [s for s in samples if s["format"] == fmt]
        lat = [s["latency_ms"] for s in fs]
        results[fmt] = {
            "bytes": fs[-1]["bytes"] if fs else 0,
            "cache_hit_rate_pct": 100.0,  # guaranteed -- any_miss would have short-circuited above
            "latency": compute_stats(lat),
        }

    if mode != "canonical_cache":
        only = formats_to_test[0]
        results = {only: results[only]}

    return {
        "failed": False,
        "cache_miss_latency_ms": cache_miss_latency_ms,
        "cache_miss_was_actually_miss": cache_miss_was_actually_miss,
        "warmup_order": warmup_order, "order": order, "samples": samples, "results": results,
    }


@app.get("/run")
def run(case_type: str = Query("plain"), structure: str = Query("flat"), n: int = Query(1000),
        encoding: str = Query("identity"), level: int = Query(None), mode: str = Query("json_cache"),
        repeats: int = Query(15), warmup: int = Query(3), seed: int = Query(42),
        source_mode: str = Query("native"), trials: int = Query(1)):
    trials = max(1, min(trials, 5))  # sane cap -- not in the spec, but unbounded trials via one
                                      # HTTP call risks the same timeout problem research mode has
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
            trial_seed = seed + t * 1000  # distinct, reproducible seed per trial
            trial_data = run_plain_case(structure, n, encoding, level, repeats, warmup, trial_seed, source_mode)
            trial_results.append({"trial_index": t, "seed": trial_seed, **trial_data})

        # Pooled/combined view across trials, for a quick reproducibility check
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
            # top-level convenience mirrors of trial 0, so single-trial callers (the default)
            # don't need to dig into trial_results[0] for the common case
            **{k: v for k, v in trial_results[0].items() if k not in ("trial_index", "seed")},
        }
    except httpx.ConnectError as e:
        return {"error": "connect_error",
                "message": f"Could not reach the API service at {API_BASE_URL}. Detail: {e}"}
    except httpx.TimeoutException as e:
        return {"error": "timeout",
                "message": f"Request to {API_BASE_URL} timed out (cold start or slow compression level). Detail: {e}"}
    except Exception as e:
        return {"error": "unexpected", "message": f"{type(e).__name__}: {e}", "api_base_url": API_BASE_URL}


@app.get("/run-research")
def run_research(repeats: int = Query(15), warmup: int = Query(2), seed: int = Query(42)):
    """Auto-runs the full matrix. Per requirement #18, this is explicitly an
    EXPLORATION tool, not the recommended path for final data collection --
    run individual cells via /run for that, so each cell's raw samples can be
    exported and saved as it completes, rather than risking one giant HTTP
    call timing out and losing everything."""
    if repeats not in (15, 30):
        repeats = 15

    matrix = []
    for structure in ["flat", "nested"]:
        for n in [1000, 10000]:
            matrix.append((structure, n, "identity", None))
            for lvl in GZIP_LEVELS:
                matrix.append((structure, n, "gzip", lvl))
            for lvl in BROTLI_LEVELS:
                matrix.append((structure, n, "brotli", lvl))

    run_id = f"research_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    all_results = []
    for structure, n, encoding, level in matrix:
        try:
            res = run_plain_case(structure, n, encoding, level, repeats, warmup, seed, "native")
            all_results.append({"structure": structure, "n": n, "encoding": encoding, "level": level, **res})
        except Exception as e:
            all_results.append({"structure": structure, "n": n, "encoding": encoding, "level": level,
                                 "error": str(e)})

    return {"research_run_id": run_id, "matrix_size": len(matrix), "repeats": repeats,
            "warmup": warmup, "seed": seed, "cases": all_results,
            "note": "Exploration tool. For final research data, run cells individually via /run "
                    "and export each cell's samples as it completes (requirement #18)."}


INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TOON vs JSON Benchmark</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 1050px; margin: 40px auto; padding: 0 20px; color: #222; }
  h1 { font-size: 22px; }
  .controls { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; margin: 20px 0; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  label { font-size: 12px; color: #666; }
  select, button, input { padding: 7px 9px; font-size: 13px; border-radius: 6px; border: 1px solid #ccc; }
  button { background: #222; color: white; border: none; cursor: pointer; }
  button:disabled { background: #999; }
  table { border-collapse: collapse; width: 100%; margin-top: 16px; font-size: 12.5px; }
  th, td { border: 1px solid #ddd; padding: 5px 8px; text-align: center; }
  th { background: #f5f5f5; }
  #status { color: #666; font-size: 13px; margin-top: 10px; }
  .warn { color: #a33; font-weight: 600; }
  .note { color: #888; font-size: 12px; margin: 6px 0; }
  details { margin: 14px 0; font-size: 13px; background: #fafafa; padding: 10px; border-radius: 6px; }
  summary { cursor: pointer; font-weight: 600; }
  .hist { display: flex; align-items: flex-end; gap: 2px; height: 60px; margin-top: 6px; }
  .bar { background: #444; width: 6px; }
  .bar.toon { background: #c60; }
  .export-btns { margin-top: 10px; display: flex; gap: 8px; }
  .fail-box { background: #fee; border: 1px solid #c33; padding: 10px; border-radius: 6px; }
</style>
</head>
<body>
<h1>TOON vs JSON Benchmark</h1>
<p style="color:#666">Requests are fired server-side against the API service, in randomized PAIRS (JSON&rarr;TOON or TOON&rarr;JSON per pair, direction seeded), using one persistent connection for the whole run.</p>

<details>
<summary>Methodology / terminology</summary>
<p><b>Data selection</b>: time to slice rows = db[structure][:n] from the in-memory database.</p>
<p><b>Serialization</b>: time to build the JSON/TOON text from those rows.</p>
<p><b>UTF-8 encoding</b>: time to encode that text to bytes.</p>
<p><b>Compression</b>: time to gzip/Brotli those bytes at the selected level.</p>
<p><b>Server processing</b> = the sum of the four phases above. Explicitly excludes network transmission.</p>
<p><b>HTTP latency</b> ("client-observed end-to-end request latency"): time from just before the request is sent to just after the response is received. Includes network + all server phases + response transmission. This is NOT pure network latency, and network time is never inferred by subtraction.</p>
<p><b>P50/P90/P95/P99</b>: statistics.quantiles(method="inclusive"), linear interpolation.</p>
<p><b>Confidence intervals</b>: 95% percentile bootstrap (1000 resamples), only computed at n&ge;10 samples -- shown as null otherwise rather than a misleadingly precise interval.</p>
<p><b>Cache-miss vs warm-cache</b>: cache is cleared, one populate request is discarded and timed separately (cache_miss_latency_ms), then warmup requests are discarded, then ALL measured requests are asserted to be cache hits -- the experiment fails outright (not silently) if any measured request is a miss.</p>
<p><b>Cross-database mode</b>: forces JSON output from TOON_DB (or vice versa) as a secondary robustness check -- kept fully separate from primary native-pairing results.</p>
</details>

<div class="controls">
  <div class="field">
    <label>Case type</label>
    <select id="caseType" onchange="toggleFields()">
      <option value="plain">Format x compression</option>
      <option value="cache">Cache layer</option>
      <option value="research">Research mode (exploration only)</option>
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
    <select id="encoding" onchange="updateLevels()">
      <option value="identity">none</option>
      <option value="gzip">gzip</option>
      <option value="brotli">brotli</option>
    </select>
  </div>
  <div class="field" id="levelField">
    <label>Level</label>
    <select id="level"></select>
  </div>
  <div class="field" id="sourceModeField">
    <label>Database source</label>
    <select id="sourceMode">
      <option value="native">Native (primary)</option>
      <option value="cross">Cross (secondary/robustness)</option>
    </select>
  </div>
  <div class="field" id="modeField" style="display:none">
    <label>Cache mode</label>
    <select id="mode">
      <option value="json_cache">JSON cache</option>
      <option value="toon_cache">TOON cache</option>
      <option value="canonical_cache">Canonical (TOON, converted on read)</option>
    </select>
  </div>
  <div class="field" id="warmupField">
    <label>Warm-up</label>
    <select id="warmup"><option value="0">0</option><option value="2">2</option><option value="3" selected>3</option><option value="5">5</option></select>
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
    <label>Independent trials</label>
    <select id="trials"><option value="1" selected>1</option><option value="3">3</option></select>
  </div>
  <div class="field" id="researchRepeatsField" style="display:none">
    <label>Repeats (research mode)</label>
    <select id="researchRepeats"><option value="15" selected>15</option><option value="30">30</option></select>
  </div>
  <div class="field">
    <label>Seed</label>
    <input id="seed" type="number" value="42" style="width:60px">
  </div>
  <button id="runBtn" onclick="runCase()">Run</button>
</div>

<p class="note" id="researchWarning" style="display:none">Research mode is an EXPLORATION tool, not for final data collection -- one giant HTTP call risks timing out and losing everything. For final research data, run cells individually (Format x compression, repeats=100) and export each cell's CSV/JSON as it completes.</p>

<div id="status"></div>
<div id="resultsArea"></div>

<script>
let lastData = null;

function updateLevels() {
  const enc = document.getElementById('encoding').value;
  const levelSel = document.getElementById('level');
  levelSel.innerHTML = '';
  let levels = [];
  if (enc === 'gzip') levels = [1,5,9];
  else if (enc === 'brotli') levels = [1,5,9,11];
  document.getElementById('levelField').style.display = levels.length ? 'flex' : 'none';
  for (const l of levels) {
    const opt = document.createElement('option');
    opt.value = l; opt.textContent = l;
    if (l === 5) opt.selected = true;
    levelSel.appendChild(opt);
  }
}

function toggleFields() {
  const ct = document.getElementById('caseType').value;
  const isCache = ct === 'cache';
  const isResearch = ct === 'research';
  document.getElementById('encodingField').style.display = (isCache || isResearch) ? 'none' : 'flex';
  document.getElementById('levelField').style.display = (isCache || isResearch) ? 'none' : (document.getElementById('encoding').value !== 'identity' ? 'flex' : 'none');
  document.getElementById('sourceModeField').style.display = isCache ? 'none' : 'flex';
  document.getElementById('modeField').style.display = isCache ? 'flex' : 'none';
  document.getElementById('nField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('structureField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('warmupField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('repeatsField').style.display = isResearch ? 'none' : 'flex';
  document.getElementById('trialsField').style.display = (isCache || isResearch) ? 'none' : 'flex';
  document.getElementById('researchRepeatsField').style.display = isResearch ? 'flex' : 'none';
  document.getElementById('researchWarning').style.display = isResearch ? 'block' : 'none';
}

async function runCase() {
  const btn = document.getElementById('runBtn');
  const status = document.getElementById('status');
  const ct = document.getElementById('caseType').value;
  btn.disabled = true;
  status.textContent = 'Running...';
  document.getElementById('resultsArea').innerHTML = '';

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
    const lvl = document.getElementById('level').value;
    if (lvl) params.set('level', lvl);
    url = '/run?' + params.toString();
  }

  try {
    const res = await fetch(url);
    const data = await res.json();
    lastData = data;
    if (data.error) {
      status.textContent = 'Error (' + data.error + ')';
      document.getElementById('resultsArea').innerHTML = '<p class="warn">' + data.message + '</p>';
    } else if (data.research_run_id) {
      renderResearch(data);
      status.textContent = 'Done. ' + data.note;
    } else if (data.failed) {
      status.textContent = 'Experiment FAILED';
      document.getElementById('resultsArea').innerHTML =
        '<div class="fail-box"><b>Cache experiment failed:</b> ' + data.reason + '</div>';
    } else {
      render(data);
      status.textContent = 'Done. Experiment ID: ' + data.experiment_id + (data.preliminary ? ' (PRELIMINARY -- repeats<30)' : '');
    }
  } catch (e) {
    status.textContent = 'Error: ' + e;
  }
  btn.disabled = false;
}

function histogramHtml(samples, key) {
  const jsonVals = samples.filter(s => s.format === 'json').map(s => s[key]);
  const toonVals = samples.filter(s => s.format === 'toon').map(s => s[key]);
  const all = jsonVals.concat(toonVals);
  if (!all.length) return '';
  const max = Math.max(...all);
  let html = '<div class="hist">';
  for (const v of jsonVals) html += `<div class="bar" style="height:${max ? (v/max*60) : 0}px" title="JSON ${v}"></div>`;
  html += '</div><div class="hist">';
  for (const v of toonVals) html += `<div class="bar toon" style="height:${max ? (v/max*60) : 0}px" title="TOON ${v}"></div>`;
  html += '</div>';
  return html;
}

function statsRow(label, jsonV, toonV, diff) {
  return `<tr><td>${label}</td><td>${jsonV}</td><td>${toonV}</td>` +
         `<td>${diff ? diff.absolute_difference : ''}</td>` +
         `<td>${diff ? diff.improvement_percent + '%' : ''}</td></tr>`;
}

function ciText(ci) {
  if (!ci) return 'n/a (sample too small)';
  return `[${ci.low}, ${ci.high}] (${Math.round(ci.ci*100)}%)`;
}

function renderOneTrial(data, r, c) {
  let html = '<table><tr><th>Metric</th><th>JSON</th><th>TOON</th><th>Abs diff (TOON-JSON)</th><th>Improvement (TOON vs JSON)</th></tr>';
  html += statsRow('raw_bytes', r.json.raw_bytes, r.toon.raw_bytes, c.raw_bytes);
  html += statsRow('compressed_bytes', r.json.compressed_bytes, r.toon.compressed_bytes, c.compressed_bytes);
  html += statsRow('compression_ratio', r.json.compression_ratio, r.toon.compression_ratio, null);
  html += statsRow('size_reduction_percent', r.json.size_reduction_percent, r.toon.size_reduction_percent, null);
  html += statsRow('level_used', r.json.level_used, r.toon.level_used, null);
  html += statsRow('first_measured_request_ms', r.json.first_measured_request_ms, r.toon.first_measured_request_ms, null);
  html += statsRow('data_selection mean_ms', r.json.data_selection.mean, r.toon.data_selection.mean, null);
  html += statsRow('serialization mean_ms', r.json.serialization.mean, r.toon.serialization.mean, c.serialization_mean);
  html += statsRow('utf8_encoding mean_ms', r.json.utf8_encoding.mean, r.toon.utf8_encoding.mean, null);
  html += statsRow('compression mean_ms', r.json.compression.mean, r.toon.compression.mean, c.compression_mean);
  html += statsRow('server_processing mean_ms', r.json.server_processing.mean, r.toon.server_processing.mean, null);
  html += statsRow('http_latency MEAN_ms', r.json.http_latency.mean, r.toon.http_latency.mean, c.http_latency_mean);
  html += statsRow('http_latency p50_ms', r.json.http_latency.p50, r.toon.http_latency.p50, c.http_latency_p50);
  html += statsRow('http_latency p90_ms', r.json.http_latency.p90, r.toon.http_latency.p90, null);
  html += statsRow('http_latency p95_ms', r.json.http_latency.p95, r.toon.http_latency.p95, c.http_latency_p95);
  html += statsRow('http_latency p99_ms', r.json.http_latency.p99, r.toon.http_latency.p99, null);
  html += statsRow('http_latency stdev_ms', r.json.http_latency.stdev, r.toon.http_latency.stdev, null);
  html += statsRow('http_latency min/max_ms', r.json.http_latency.min + ' / ' + r.json.http_latency.max,
                    r.toon.http_latency.min + ' / ' + r.toon.http_latency.max, null);
  html += statsRow('95% CI (mean)', ciText(r.json.http_latency.ci_mean_95), ciText(r.toon.http_latency.ci_mean_95), null);
  html += statsRow('95% CI (p50)', ciText(r.json.http_latency.ci_p50_95), ciText(r.toon.http_latency.ci_p50_95), null);
  html += statsRow('sample count', r.json.http_latency.n, r.toon.http_latency.n, null);
  html += statsRow('errors', r.json.error_count, r.toon.error_count, null);
  html += '</table>';
  return html;
}

function render(data) {
  let html = `<p><b>Case:</b> structure=${data.structure}, n=${data.n}, encoding=${data.encoding}${data.level ? ' level='+data.level : ''}, ` +
             `repeats=${data.repeats}, warmup=${data.warmup}, seed=${data.seed}, source=${data.source_mode}, trials=${data.trials}</p>`;
  html += `<p class="note">Percentiles: ${data.percentile_method}<br>CI: ${data.ci_method}</p>`;
  if (data.preliminary) html += `<p class="warn">PRELIMINARY: repeats &lt; 30 -- percentiles (especially P99) may not be statistically meaningful.</p>`;

  if (data.trials > 1) {
    html += `<p><b>${data.trials} independent trials</b> (reproducibility check):</p>`;
    for (const tr of data.trial_results) {
      html += `<details style="margin-bottom:8px"><summary>Trial ${tr.trial_index} (seed=${tr.seed})</summary>`;
      html += renderOneTrial(data, tr.results, tr.comparison);
      html += `<p class="note">Order (pairs): ${tr.pair_directions.join(', ')}</p></details>`;
    }
    if (data.combined_across_trials) {
      html += `<p><b>Pooled across all trials:</b></p>`;
      html += `<table><tr><th></th><th>JSON http_latency</th><th>TOON http_latency</th></tr>`;
      const cj = data.combined_across_trials.json_http_latency, ct2 = data.combined_across_trials.toon_http_latency;
      html += `<tr><td>mean</td><td>${cj.mean}</td><td>${ct2.mean}</td></tr>`;
      html += `<tr><td>p50</td><td>${cj.p50}</td><td>${ct2.p50}</td></tr>`;
      html += `<tr><td>n</td><td>${cj.n}</td><td>${ct2.n}</td></tr></table>`;
    }
  } else {
    html += renderOneTrial(data, data.results, data.comparison);
    html += '<p class="note">Latency distribution (each bar = one request):</p>';
    html += histogramHtml(data.samples, 'latency_ms');
    html += `<p class="note">Pair directions (seed=${data.seed}): ${data.pair_directions.join(', ')}</p>`;
  }

  html += `<div class="export-btns">
    <button onclick="exportJson()">Download JSON</button>
    <button onclick="exportCsv()">Download CSV (samples)</button>
  </div>`;
  document.getElementById('resultsArea').innerHTML = html;
}

function renderResearch(data) {
  let html = `<p><b>Research run:</b> ${data.research_run_id} -- ${data.matrix_size} cases, repeats=${data.repeats}, warmup=${data.warmup}</p>`;
  html += `<p class="warn">${data.note}</p>`;
  html += '<table><tr><th>Structure</th><th>n</th><th>Encoding</th><th>Level</th>' +
          '<th>JSON compressed_bytes</th><th>TOON compressed_bytes</th>' +
          '<th>JSON p50 ms</th><th>TOON p50 ms</th></tr>';
  for (const c of data.cases) {
    if (c.error) {
      html += `<tr><td>${c.structure}</td><td>${c.n}</td><td>${c.encoding}</td><td>${c.level||''}</td>` +
              `<td colspan="4" class="warn">${c.error}</td></tr>`;
      continue;
    }
    const r = c.results;
    html += `<tr><td>${c.structure}</td><td>${c.n}</td><td>${c.encoding}</td><td>${c.level||''}</td>` +
            `<td>${r.json.compressed_bytes}</td><td>${r.toon.compressed_bytes}</td>` +
            `<td>${r.json.http_latency.p50}</td><td>${r.toon.http_latency.p50}</td></tr>`;
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
                'request_index','data_selection_ms','serialization_ms','utf8_encoding_ms',
                'compression_ms','server_processing_ms','http_latency_ms','raw_bytes',
                'compressed_bytes','status_code','source_db'];
  let csv = cols.join(',') + '\\n';
  const trialList = lastData.trial_results || [{trial_index: 0, samples: lastData.samples}];
  for (const tr of trialList) {
    for (const s of tr.samples) {
      const row = {
        trial: tr.trial_index, experiment_id: lastData.experiment_id, timestamp: s.timestamp,
        structure: lastData.structure, n: lastData.n, format: s.format, encoding: s.encoding,
        level: s.level, request_index: s.request_index, data_selection_ms: s.data_selection_ms,
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

updateLevels();
toggleFields();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
"""
Frontend / "requesting server" for the TOON vs JSON benchmark -- v3.

Key methodology changes from v2 (see accompanying explanation for full rationale):
  - Requests are INTERLEAVED in randomized order (not all-JSON-then-all-TOON),
    using a seeded RNG so the exact order is reproducible and recorded in output.
  - ONE persistent httpx.Client serves the entire comparison run (both formats),
    not a fresh client per format.
  - Percentiles (P50/P90/P95/P99) use Python's `statistics.quantiles` (method=
    "inclusive", linear interpolation) instead of manual array-index math.
  - Every individual request is recorded (timestamp, format, order position,
    latency, serialization time, compression time, bytes, status) and returned
    under "samples" -- not just aggregates.
  - Configurable warm-up requests (excluded from stats) plus an explicit
    cold-start figure (the first REAL measured request per format).
  - Compression level is a first-class parameter, validated against what the
    API actually used (echoed back in X-Level, displayed in the UI).
  - Cross-database mode: force JSON output from TOON_DB, or TOON output from
    JSON_DB, to test format/database independence.
  - Experiment ID + full metadata on every run; raw samples exportable as
    JSON/CSV from the browser.
  - "Research Mode" auto-runs the full compression x level x size x structure
    matrix. Repetition choices there are capped at 15 or 30 (not 100) because
    the full matrix at high Brotli quality levels is already very slow on a
    constrained free-tier CPU -- see the methodology notes in the UI.

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

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:9800")
if API_BASE_URL and not API_BASE_URL.startswith("http"):
    API_BASE_URL = f"https://{API_BASE_URL}"

app = FastAPI(title="TOON vs JSON Benchmark Frontend v3")

GZIP_LEVELS = [1, 5, 9]
BROTLI_LEVELS = [1, 5, 9, 11]


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
# Statistics -- percentile method documented explicitly, per requirement #3.
# ---------------------------------------------------------------------------

def compute_stats(values):
    """Percentile method: statistics.quantiles(values, n=100, method='inclusive'),
    linear interpolation between the two nearest ranks. Requires >=2 samples for
    quantiles; falls back to the single value otherwise."""
    if not values:
        return {"n": 0, "mean": 0, "stdev": 0, "min": 0, "max": 0, "p50": 0, "p90": 0, "p95": 0, "p99": 0}
    n = len(values)
    mean = statistics.mean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    lo, hi = min(values), max(values)
    if n >= 2:
        q = statistics.quantiles(values, n=100, method="inclusive")  # 99 cut points

        def qp(p):
            idx = max(0, min(len(q) - 1, p - 1))
            return q[idx]
        p50, p90, p95, p99 = qp(50), qp(90), qp(95), qp(99)
    else:
        p50 = p90 = p95 = p99 = values[0]
    return {"n": n, "mean": round(mean, 3), "stdev": round(sd, 3), "min": round(lo, 3), "max": round(hi, 3),
            "p50": round(p50, 3), "p90": round(p90, 3), "p95": round(p95, 3), "p99": round(p99, 3)}


def compute_diff(json_val, toon_val):
    """absolute_difference = TOON - JSON. improvement_percent is positive when
    TOON is LOWER (better for a cost metric like latency or bytes)."""
    abs_diff = toon_val - json_val
    rel_diff_pct = (abs_diff / json_val * 100) if json_val else 0.0
    improvement_pct = ((json_val - toon_val) / json_val * 100) if json_val else 0.0
    return {"absolute_difference": round(abs_diff, 4),
            "relative_difference_percent": round(rel_diff_pct, 2),
            "improvement_percent": round(improvement_pct, 2)}


def build_interleaved_order(repeats, seed):
    order = ["json"] * repeats + ["toon"] * repeats
    random.Random(seed).shuffle(order)
    return order


def _do_request(client: httpx.Client, fmt, structure, n, encoding, level, source_mode):
    params = {"format": fmt, "encoding": encoding, "n": n, "structure": structure}
    if level is not None:
        params["level"] = level
    if source_mode == "cross":
        params["source"] = "toon_db" if fmt == "json" else "json_db"
    # source_mode == "native" (default): omit "source", API applies auto-pairing

    t0 = time.perf_counter()
    try:
        r = client.get(f"{API_BASE_URL}/data", params=params)
        latency_ms = (time.perf_counter() - t0) * 1000
        return {
            "format": fmt, "latency_ms": round(latency_ms, 3),
            "serialization_ms": float(r.headers.get("x-serialization-time-ms", 0)),
            "compression_ms": float(r.headers.get("x-compression-time-ms", 0)),
            "raw_bytes": int(r.headers.get("x-raw-bytes", 0)),
            "compressed_bytes": int(r.headers.get("x-compressed-bytes", 0)),
            "status_code": r.status_code,
            "level_used": r.headers.get("x-level", ""),
            "source_db": r.headers.get("x-source-db", ""),
        }
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        return {"format": fmt, "latency_ms": round(latency_ms, 3), "serialization_ms": 0,
                "compression_ms": 0, "raw_bytes": 0, "compressed_bytes": 0,
                "status_code": 0, "error": str(e), "level_used": "", "source_db": ""}


def run_plain_case(structure, n, encoding, level, repeats, warmup, seed, source_mode):
    order = build_interleaved_order(repeats, seed)
    warmup_order = build_interleaved_order(warmup, seed + 1) if warmup > 0 else []

    with httpx.Client(timeout=120) as client:  # ONE persistent client for the whole run
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
        lat = [s["latency_ms"] for s in fs if s["status_code"] == 200]
        ser = [s["serialization_ms"] for s in fs if s["status_code"] == 200]
        comp = [s["compression_ms"] for s in fs if s["status_code"] == 200]
        ok = [s for s in fs if s["status_code"] == 200]
        results[fmt] = {
            "raw_bytes": ok[-1]["raw_bytes"] if ok else 0,
            "compressed_bytes": ok[-1]["compressed_bytes"] if ok else 0,
            "level_used": ok[-1]["level_used"] if ok else "",
            "http_latency": compute_stats(lat),
            "serialization": compute_stats(ser),
            "compression": compute_stats(comp),
            "cold_start_first_request_ms": fs[0]["latency_ms"] if fs else None,
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

    return {"order": order, "warmup_order": warmup_order, "samples": samples,
            "results": results, "comparison": comparison}


def run_cache_case(mode, structure, n, repeats, seed):
    formats_to_test = ["json", "toon"] if mode == "canonical_cache" else \
                       (["json"] if mode == "json_cache" else ["toon"])
    order = []
    rnd = random.Random(seed)
    for fmt in formats_to_test:
        order += [fmt] * repeats
    rnd.shuffle(order)

    results = {}
    samples = []
    with httpx.Client(timeout=120) as client:
        client.post(f"{API_BASE_URL}/cache/clear")
        for idx, fmt in enumerate(order):
            t0 = time.perf_counter()
            r = client.get(f"{API_BASE_URL}/cache/data",
                            params={"mode": mode, "format": fmt, "n": n, "structure": structure})
            latency_ms = (time.perf_counter() - t0) * 1000
            samples.append({
                "request_index": idx, "format": fmt, "latency_ms": round(latency_ms, 3),
                "cache_hit": r.headers.get("x-cache-hit") == "true",
                "bytes": int(r.headers.get("x-bytes", 0)), "status_code": r.status_code,
            })

    for fmt in formats_to_test:
        fs = [s for s in samples if s["format"] == fmt]
        lat = [s["latency_ms"] for s in fs]
        hits = sum(1 for s in fs if s["cache_hit"])
        results[fmt] = {
            "bytes": fs[-1]["bytes"] if fs else 0,
            "cache_hit_rate_pct": round(100 * hits / len(fs), 1) if fs else 0,
            "latency": compute_stats(lat),
            "cold_start_first_request_ms": fs[0]["latency_ms"] if fs else None,
        }

    if mode != "canonical_cache":
        only = formats_to_test[0]
        results = {only: results[only]}

    return {"order": order, "samples": samples, "results": results}


@app.get("/run")
def run(case_type: str = Query("plain"), structure: str = Query("flat"), n: int = Query(1000),
        encoding: str = Query("identity"), level: int = Query(None), mode: str = Query("json_cache"),
        repeats: int = Query(15), warmup: int = Query(3), seed: int = Query(42),
        source_mode: str = Query("native")):
    experiment_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{structure}_{n}_{encoding}{level or ''}_{repeats}"
    timestamp = datetime.now(timezone.utc).isoformat()

    try:
        if case_type == "cache":
            data = run_cache_case(mode, structure, n, repeats, seed)
        else:
            data = run_plain_case(structure, n, encoding, level, repeats, warmup, seed, source_mode)

        return {
            "experiment_id": experiment_id, "timestamp": timestamp,
            "case_type": case_type, "structure": structure, "n": n, "encoding": encoding, "level": level,
            "mode": mode, "repeats": repeats, "warmup": warmup, "seed": seed, "source_mode": source_mode,
            "api_base_url": API_BASE_URL,
            "percentile_method": "statistics.quantiles(n=100, method='inclusive'), linear interpolation",
            "preliminary": repeats < 30,
            **data,
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
    """Auto-runs the full matrix: structure x n x (identity, gzip@1/5/9, brotli@1/5/9/11).
    WARNING: this can take a very long time -- Brotli at quality 11 on a
    constrained CPU can take 10-30+ seconds PER REQUEST at n=10000, and this
    runs many combinations sequentially. Consider starting with a subset."""
    if repeats not in (15, 30):
        repeats = 15  # research mode intentionally caps repeats at 15 or 30

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
            "warmup": warmup, "seed": seed, "cases": all_results}


INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TOON vs JSON Benchmark</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 1000px; margin: 40px auto; padding: 0 20px; color: #222; }
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
  .warn { color: #a33; }
  .note { color: #888; font-size: 12px; margin: 6px 0; }
  details { margin: 14px 0; font-size: 13px; background: #fafafa; padding: 10px; border-radius: 6px; }
  summary { cursor: pointer; font-weight: 600; }
  .hist { display: flex; align-items: flex-end; gap: 2px; height: 60px; margin-top: 6px; }
  .bar { background: #444; width: 6px; }
  .bar.toon { background: #c60; }
  .export-btns { margin-top: 10px; display: flex; gap: 8px; }
</style>
</head>
<body>
<h1>TOON vs JSON Benchmark</h1>
<p style="color:#666">Requests are fired server-side against the API service, interleaved in randomized order (seeded, reproducible), using one persistent connection for the whole run.</p>

<details>
<summary>Methodology / terminology</summary>
<p><b>Raw bytes</b>: size after serialization, before compression.</p>
<p><b>Compressed bytes</b>: size after gzip/Brotli at the selected level.</p>
<p><b>Serialization time</b>: time to build the JSON/TOON text from the in-memory data.</p>
<p><b>Compression time</b>: time to compress that text with gzip/Brotli.</p>
<p><b>HTTP latency</b> ("end-to-end request latency"): client-observed time from just before the request is sent to just after the response is received -- includes network, server processing (serialization + compression), and response transmission. This is NOT pure network latency.</p>
<p><b>P50/P90/P95/P99</b>: percentiles via statistics.quantiles(method="inclusive"), linear interpolation. With small sample counts (under ~30), especially P99, treat these as preliminary, not statistically robust.</p>
<p><b>Cross-database mode</b>: forces the JSON-format request to read from the TOON database (or vice versa) to check the two databases are truly interchangeable -- results should be byte-identical to native mode if the data is equivalent.</p>
</details>

<div class="controls">
  <div class="field">
    <label>Case type</label>
    <select id="caseType" onchange="toggleFields()">
      <option value="plain">Format x compression</option>
      <option value="cache">Cache layer</option>
      <option value="research">Research mode (full matrix)</option>
    </select>
  </div>
  <div class="field" id="structureField">
    <label>Structure</label>
    <select id="structure"><option value="flat">flat</option><option value="nested">nested</option></select>
  </div>
  <div class="field" id="nField">
    <label>n (records)</label>
    <select id="n"><option value="1000">1000</option><option value="10000" selected>10000</option></select>
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
      <option value="native">Native (json&rarr;JSON_DB, toon&rarr;TOON_DB)</option>
      <option value="cross">Cross (json&rarr;TOON_DB, toon&rarr;JSON_DB)</option>
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
      <option value="100">100 (research)</option>
    </select>
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

<p class="note" id="researchWarning" style="display:none">Research mode runs the full structure x size x compression x level matrix sequentially. High Brotli levels at n=10000 can take many seconds PER REQUEST -- this can take a long time and may exceed platform request timeouts. Consider running smaller subsets manually first.</p>

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
  document.getElementById('researchRepeatsField').style.display = isResearch ? 'flex' : 'none';
  document.getElementById('researchWarning').style.display = isResearch ? 'block' : 'none';
}

async function runCase() {
  const btn = document.getElementById('runBtn');
  const status = document.getElementById('status');
  const ct = document.getElementById('caseType').value;
  btn.disabled = true;
  status.textContent = 'Running... (this can take a while, especially research mode or high Brotli levels)';
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
      document.getElementById('resultsArea').innerHTML = '<p style="color:#b00">' + data.message + '</p>';
    } else if (data.research_run_id) {
      renderResearch(data);
      status.textContent = 'Done. Research run: ' + data.research_run_id;
    } else {
      render(data);
      status.textContent = 'Done. Experiment ID: ' + data.experiment_id + (data.preliminary ? ' (PRELIMINARY -- low repeat count)' : '');
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

function statsRow(label, jsonStats, toonStats, diff) {
  return `<tr><td>${label}</td><td>${jsonStats}</td><td>${toonStats}</td>` +
         `<td>${diff ? diff.absolute_difference : ''}</td>` +
         `<td>${diff ? diff.improvement_percent + '%' : ''}</td></tr>`;
}

function render(data) {
  const r = data.results;
  const formatsPresent = Object.keys(r);
  let html = `<p><b>Case:</b> ${data.case_type}, structure=${data.structure}, n=${data.n}, ` +
             `${data.case_type === 'cache' ? 'mode=' + data.mode : 'encoding=' + data.encoding + (data.level ? ' level=' + data.level : '')}, ` +
             `repeats=${data.repeats}, warmup=${data.warmup || 0}, seed=${data.seed}, source=${data.source_mode || 'n/a'}</p>`;
  html += `<p class="note">Percentile method: ${data.percentile_method}</p>`;
  if (data.preliminary) html += `<p class="warn">PRELIMINARY: repeats &lt; 30 -- percentiles (especially P99) may not be statistically meaningful.</p>`;

  if (formatsPresent.length === 1) {
    const only = formatsPresent[0];
    const s = r[only];
    html += `<p>Single-format cache (${only.toUpperCase()} only) -- cold vs warm behavior.</p>`;
    html += '<table><tr><th>Metric</th><th>Value</th></tr>';
    html += `<tr><td>bytes</td><td>${s.bytes}</td></tr>`;
    html += `<tr><td>cache_hit_rate_pct</td><td>${s.cache_hit_rate_pct}</td></tr>`;
    html += `<tr><td>first_request_ms (cold start)</td><td>${s.cold_start_first_request_ms}</td></tr>`;
    html += `<tr><td>mean latency_ms</td><td>${s.latency.mean}</td></tr>`;
    html += `<tr><td>p50 latency_ms</td><td>${s.latency.p50}</td></tr>`;
    html += `<tr><td>p95 latency_ms</td><td>${s.latency.p95}</td></tr>`;
    html += '</table>';
  } else if (r.json && r.json.http_latency) {
    const c = data.comparison || {};
    html += '<table><tr><th>Metric</th><th>JSON</th><th>TOON</th><th>Abs diff (TOON-JSON)</th><th>Improvement (TOON vs JSON)</th></tr>';
    html += statsRow('raw_bytes', r.json.raw_bytes, r.toon.raw_bytes, c.raw_bytes);
    html += statsRow('compressed_bytes', r.json.compressed_bytes, r.toon.compressed_bytes, c.compressed_bytes);
    html += statsRow('level_used', r.json.level_used, r.toon.level_used, null);
    html += statsRow('cold_start_first_request_ms', r.json.cold_start_first_request_ms, r.toon.cold_start_first_request_ms, null);
    html += statsRow('serialization mean_ms', r.json.serialization.mean, r.toon.serialization.mean, c.serialization_mean);
    html += statsRow('compression mean_ms', r.json.compression.mean, r.toon.compression.mean, c.compression_mean);
    html += statsRow('http_latency MEAN_ms', r.json.http_latency.mean, r.toon.http_latency.mean, c.http_latency_mean);
    html += statsRow('http_latency p50_ms', r.json.http_latency.p50, r.toon.http_latency.p50, c.http_latency_p50);
    html += statsRow('http_latency p90_ms', r.json.http_latency.p90, r.toon.http_latency.p90, null);
    html += statsRow('http_latency p95_ms', r.json.http_latency.p95, r.toon.http_latency.p95, c.http_latency_p95);
    html += statsRow('http_latency p99_ms', r.json.http_latency.p99, r.toon.http_latency.p99, null);
    html += statsRow('http_latency stdev_ms', r.json.http_latency.stdev, r.toon.http_latency.stdev, null);
    html += statsRow('http_latency min/max_ms', r.json.http_latency.min + ' / ' + r.json.http_latency.max,
                      r.toon.http_latency.min + ' / ' + r.toon.http_latency.max, null);
    html += statsRow('sample count', r.json.http_latency.n, r.toon.http_latency.n, null);
    html += statsRow('errors', r.json.error_count, r.toon.error_count, null);
    html += '</table>';
    html += '<p class="note">Latency distribution (each bar = one request, JSON above / TOON below, taller = slower):</p>';
    html += histogramHtml(data.samples, 'latency_ms');
    html += `<p class="note">Request order (interleaved, seed=${data.seed}): ${data.order.join(', ')}</p>`;
  }

  html += `<div class="export-btns">
    <button onclick="exportJson()">Download JSON</button>
    <button onclick="exportCsv()">Download CSV (samples)</button>
  </div>`;
  document.getElementById('resultsArea').innerHTML = html;
}

function renderResearch(data) {
  let html = `<p><b>Research run:</b> ${data.research_run_id} -- ${data.matrix_size} cases, repeats=${data.repeats}, warmup=${data.warmup}</p>`;
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
  if (!lastData || !lastData.samples) return;
  const cols = ['request_index','format','latency_ms','serialization_ms','compression_ms','raw_bytes','compressed_bytes','status_code','level_used','source_db'];
  let csv = cols.join(',') + '\\n';
  for (const s of lastData.samples) {
    csv += cols.map(c => s[c] !== undefined ? s[c] : '').join(',') + '\\n';
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

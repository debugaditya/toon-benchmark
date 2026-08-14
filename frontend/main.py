"""
Frontend / "requesting server" for the TOON vs JSON benchmark.

Serves a single-page UI listing all 21 cases. When the user picks a case
and clicks Run, this server (not the browser) fires the repeated requests
against the API service, times them, and returns aggregated results as
JSON for the page to render.

Env vars:
    API_BASE_URL   -- base URL of the deployed api/ service, e.g.
                       https://toon-benchmark-api.onrender.com
                       (defaults to http://localhost:9001 for local dev)

Run locally:
    pip install -r requirements.txt
    API_BASE_URL=http://localhost:9001 uvicorn main:app --port 9000

Deploy on Render as a Web Service:
    Build command: pip install -r requirements.txt
    Start command: uvicorn main:app --host 0.0.0.0 --port $PORT
    Environment:   API_BASE_URL = <your deployed api service URL>
"""
import os
import time

import httpx
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:9001")
if API_BASE_URL and not API_BASE_URL.startswith("http"):
    # Render's fromService/property:host gives a bare hostname, e.g. "toon-benchmark-api.onrender.com"
    API_BASE_URL = f"https://{API_BASE_URL}"

app = FastAPI(title="TOON vs JSON Benchmark Frontend")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api-base")
def api_base():
    return {"api_base_url": API_BASE_URL}


@app.get("/debug-connection")
def debug_connection():
    """Directly tests whether this service can reach the API service, and reports exactly why if not."""
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


def run_plain_case(structure, n, encoding, repeats):
    results = {}
    for fmt in ["json", "toon"]:
        latencies, raw_b, comp_b, enc_ms = [], 0, 0, 0
        with httpx.Client(timeout=60) as client:
            for _ in range(repeats):
                headers = {"Accept-Encoding": {"identity": "identity", "gzip": "gzip", "brotli": "br"}[encoding]}
                t0 = time.perf_counter()
                r = client.get(f"{API_BASE_URL}/data",
                                params={"format": fmt, "encoding": encoding, "n": n, "structure": structure},
                                headers=headers)
                latencies.append((time.perf_counter() - t0) * 1000)
                raw_b = int(r.headers.get("x-raw-bytes", 0))
                comp_b = int(r.headers.get("x-compressed-bytes", 0))
                enc_ms = float(r.headers.get("x-encode-time-ms", 0))
        latencies.sort()
        ln = len(latencies)
        results[fmt] = {
            "raw_bytes": raw_b, "compressed_bytes": comp_b, "encode_ms": round(enc_ms, 4),
            "latency_p50_ms": round(latencies[ln // 2], 3),
            "latency_p95_ms": round(latencies[max(int(ln * 0.95) - 1, 0)], 3),
        }
    return results


def run_cache_case(mode, structure, n, repeats):
    results = {}
    formats_to_test = ["json", "toon"] if mode == "canonical_cache" else \
                       (["json"] if mode == "json_cache" else ["toon"])
    with httpx.Client(timeout=60) as client:
        client.post(f"{API_BASE_URL}/cache/clear")
        for fmt in formats_to_test:
            latencies, hits, byte_counts = [], 0, []
            for i in range(repeats):
                t0 = time.perf_counter()
                r = client.get(f"{API_BASE_URL}/cache/data",
                                params={"mode": mode, "format": fmt, "n": n, "structure": structure})
                latencies.append((time.perf_counter() - t0) * 1000)
                if r.headers.get("x-cache-hit") == "true":
                    hits += 1
                byte_counts.append(int(r.headers.get("x-bytes", 0)))
            latencies.sort()
            ln = len(latencies)
            results[fmt] = {
                "bytes": byte_counts[-1] if byte_counts else 0,
                "cache_hit_rate_pct": round(100 * hits / repeats, 1),
                "latency_first_request_ms": round(latencies[0], 3),
                "latency_p50_ms": round(latencies[ln // 2], 3),
                "latency_warm_avg_ms": round(sum(latencies[1:]) / max(len(latencies) - 1, 1), 3) if ln > 1 else 0,
            }
    if mode != "canonical_cache":
        # single-format cache: report cold-vs-warm under one key instead of a fake json/toon pair
        only = formats_to_test[0]
        results = {only: results[only]}
    return results


@app.get("/run")
def run(case_type: str = Query("plain"), structure: str = Query("flat"), n: int = Query(1000),
         encoding: str = Query("identity"), mode: str = Query("json_cache"), repeats: int = Query(15)):
    try:
        if case_type == "cache":
            data = run_cache_case(mode, structure, n, repeats)
        else:
            data = run_plain_case(structure, n, encoding, repeats)
        return {"case_type": case_type, "structure": structure, "n": n, "encoding": encoding,
                "mode": mode, "repeats": repeats, "results": data}
    except httpx.ConnectError as e:
        return {"error": "connect_error",
                "message": f"Could not reach the API service at {API_BASE_URL}. "
                            f"Check that API_BASE_URL is set correctly on the frontend service, "
                            f"and that the API service is deployed and awake. Detail: {e}"}
    except httpx.TimeoutException as e:
        return {"error": "timeout",
                "message": f"Request to {API_BASE_URL} timed out -- this usually means the API "
                            f"service was asleep (Render free tier cold start can take 30-60s). "
                            f"Try hitting {API_BASE_URL}/health directly first to wake it, then retry. "
                            f"Detail: {e}"}
    except Exception as e:
        return {"error": "unexpected", "message": f"{type(e).__name__}: {e}", "api_base_url": API_BASE_URL}


INDEX_HTML = """
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>TOON vs JSON Benchmark</title>
<style>
  body { font-family: -apple-system, sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; color: #222; }
  h1 { font-size: 22px; }
  .controls { display: flex; gap: 12px; flex-wrap: wrap; align-items: end; margin: 20px 0; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  label { font-size: 12px; color: #666; }
  select, button { padding: 8px 10px; font-size: 14px; border-radius: 6px; border: 1px solid #ccc; }
  button { background: #222; color: white; border: none; cursor: pointer; }
  button:disabled { background: #999; }
  table { border-collapse: collapse; width: 100%; margin-top: 20px; font-size: 13px; }
  th, td { border: 1px solid #ddd; padding: 6px 10px; text-align: center; }
  th { background: #f5f5f5; }
  #status { color: #666; font-size: 13px; margin-top: 10px; }
</style>
</head>
<body>
<h1>TOON vs JSON Benchmark</h1>
<p style="color:#666">Requests are fired server-side against the API service, not from your browser.</p>

<div class="controls">
  <div class="field">
    <label>Case type</label>
    <select id="caseType" onchange="toggleFields()">
      <option value="plain">Format x compression</option>
      <option value="cache">Cache layer</option>
    </select>
  </div>
  <div class="field" id="structureField">
    <label>Structure</label>
    <select id="structure"><option value="flat">flat</option><option value="nested">nested</option></select>
  </div>
  <div class="field">
    <label>n (records)</label>
    <select id="n"><option value="10">10</option><option value="1000">1000</option><option value="10000" selected>10000</option></select>
  </div>
  <div class="field" id="encodingField">
    <label>Compression</label>
    <select id="encoding"><option value="identity">none</option><option value="gzip">gzip</option><option value="brotli">brotli</option></select>
  </div>
  <div class="field" id="modeField" style="display:none">
    <label>Cache mode</label>
    <select id="mode">
      <option value="json_cache">JSON cache</option>
      <option value="toon_cache">TOON cache</option>
      <option value="canonical_cache">Canonical (TOON, converted on read)</option>
    </select>
  </div>
  <div class="field">
    <label>Repeats</label>
    <select id="repeats"><option value="5">5</option><option value="15" selected>15</option><option value="30">30</option></select>
  </div>
  <button id="runBtn" onclick="runCase()">Run</button>
</div>

<div id="status"></div>
<div id="resultsArea"></div>

<script>
function toggleFields() {
  const isCache = document.getElementById('caseType').value === 'cache';
  document.getElementById('encodingField').style.display = isCache ? 'none' : 'flex';
  document.getElementById('modeField').style.display = isCache ? 'flex' : 'none';
}

async function runCase() {
  const btn = document.getElementById('runBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'Running...';
  document.getElementById('resultsArea').innerHTML = '';

  const params = new URLSearchParams({
    case_type: document.getElementById('caseType').value,
    structure: document.getElementById('structure').value,
    n: document.getElementById('n').value,
    encoding: document.getElementById('encoding').value,
    mode: document.getElementById('mode').value,
    repeats: document.getElementById('repeats').value,
  });

  try {
    const res = await fetch('/run?' + params.toString());
    const data = await res.json();
    render(data);
    status.textContent = 'Done.';
  } catch (e) {
    status.textContent = 'Error: ' + e;
  }
  btn.disabled = false;
}

function render(data) {
  const r = data.results;
  const formatsPresent = Object.keys(r);
  let html = `<p><b>Case:</b> ${data.case_type}, structure=${data.structure}, n=${data.n}, ` +
             `${data.case_type === 'cache' ? 'mode=' + data.mode : 'encoding=' + data.encoding}, repeats=${data.repeats}</p>`;

  if (formatsPresent.length === 1) {
    html += `<p style="color:#666">Single-format cache (${formatsPresent[0].toUpperCase()} only) — showing cold vs warm request behavior.</p>`;
    html += '<table><tr><th>Metric</th><th>Value</th></tr>';
    const only = formatsPresent[0];
    for (const k of Object.keys(r[only])) {
      html += `<tr><td>${k}</td><td>${r[only][k]}</td></tr>`;
    }
    html += '</table>';
  } else {
    html += '<table><tr><th>Metric</th><th>JSON</th><th>TOON</th></tr>';
    const keys = Object.keys(r.json);
    for (const k of keys) {
      html += `<tr><td>${k}</td><td>${r.json[k]}</td><td>${r.toon[k]}</td></tr>`;
    }
    html += '</table>';
  }
  document.getElementById('resultsArea').innerHTML = html;
}

toggleFields();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML

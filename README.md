# TOON vs JSON Benchmark

## Experimental Evaluation of Serialization, Compression, Caching, and End-to-End HTTP Performance

[![Language: C++](https://img.shields.io/badge/serializer-C%2B%2B-blue.svg)](https://isocpp.org/)
[![Language: Python](https://img.shields.io/badge/API-Python-yellow.svg)](https://www.python.org/)
[![Benchmark](https://img.shields.io/badge/benchmark-100%2C000%20records-informational.svg)](https://github.com/debugaditya/toon-benchmark)

This repository contains the complete implementation and experimental artifacts for a systems-level comparison of **JSON** and **TOON (Token-Oriented Object Notation)** as API response formats.

The project was built to answer a more useful question than simply *"Which format produces fewer bytes?"*

> **When serialization, compression, server processing, caching, and HTTP delivery are considered together, does TOON's structural compactness produce a measurable end-to-end performance advantage over JSON?**

The benchmark deliberately measures the complete serving path. It also separates the effects of **representation size** from the cost of **encoding and compression**, because a smaller representation does not automatically imply a faster implementation.

---

## Author

**Aditya Narayan Barmola**  
**Netaji Subhas University of Technology (NSUT)**  
New Delhi, India

**Email:** `adibarmola@gmail.com`

**Experiment Setup:**  
https://github.com/debugaditya/toon-benchmark

---

# 1. Research Overview

JSON is the dominant interchange format for web APIs because it is simple, interoperable, human-readable, and supported by practically every programming ecosystem. Its representation, however, repeatedly stores object keys and structural delimiters for every record.

TOON takes a schema-oriented approach for regular structured data. Instead of repeating the same field names for every object, the schema is declared once and the records are represented as compact rows.

For example, the flat benchmark workload uses the logical schema:

```text
[100000]{id,name,age,city}:
```

with records such as:

```text
1,QAHFTR,52,Mumbai
2,PACGPO,63,Bhopal
3,KLHWTE,45,Kolkata
4,HFTCJJ,40,Surat
5,GBLDXC,36,Surat
```

The nested workload uses:

```text
[100000]{id,name,address{city,zip},tags}:
```

with records such as:

```text
1,QAHFTR,{Bhopal,191161},[2]{trial,vip}
2,AFNAFQ,{Bhopal,539898},null
3,FPVAUS,{Kolkata,391369},[2]{new,vip}
4,YICCWP,{Delhi,865179},null
```

The experiment therefore studies both a regular tabular workload and a more structurally demanding nested workload.

---

# 2. What This Project Is Trying to Measure

The benchmark is intentionally **pipeline-oriented**.

A response does not simply go from a database to a byte count. In the deployed experiment, the request passes through several stages:

```text
Database retrieval
       ↓
Data representation / serialization
       ↓
Optional compression
       ↓
Server-side processing and encoding overhead
       ↓
HTTP response
       ↓
Client-observed latency
```

For cached responses, the path changes because previously generated response material can be served without repeating the complete generation pipeline.

The benchmark therefore measures:

- payload size
- compressed payload size
- database retrieval time
- serialization time
- compression time
- server processing time
- complete HTTP latency
- HTTP p50 latency
- HTTP p95 latency
- cache miss latency
- warm-cache latency distribution
- cache hit rate
- cache variability

This allows the experiment to distinguish between:

**Representation efficiency**

and

**implementation efficiency**

and finally determine whether either advantage survives at the **end-to-end HTTP level**.

---

# 3. Research Questions

The experiment is organized around the following questions.

### RQ1 — Payload compactness

Does TOON reduce the number of bytes required to represent the same 100,000 logical records compared with JSON?

### RQ2 — Post-compression compactness

After applying Brotli compression, how much of TOON's original byte advantage remains?

The experiment evaluates:

- Identity / no compression
- Brotli 5
- Brotli 9
- Brotli 11

### RQ3 — Serialization cost

Does the native C++ TOON serializer require more or less time than JSON serialization?

Does this difference depend on whether the workload is flat or nested?

### RQ4 — Compression cost

Does the smaller TOON input reduce compression work enough to offset any additional serialization cost?

### RQ5 — End-to-end latency

When database retrieval, serialization, compression, server-side overhead, and HTTP delivery are considered together, does TOON reduce mean, p50, and p95 latency?

### RQ6 — Source robustness

Do the conclusions remain consistent when the requested output format is produced from the **opposite source representation** rather than its normal/native source?

### RQ7 — Cache behavior

When responses are already materialized in a cache, does TOON's smaller representation produce an even stronger serving advantage?

---

# 4. Experimental Hypotheses

The benchmark was designed around several competing effects rather than assuming that TOON must win every metric.

### H1 — Spatial compactness

TOON should substantially reduce raw payload size because repeated field names and structural syntax are represented once at the schema level.

### H2 — Compression convergence

General-purpose compression should reduce the absolute difference between JSON and TOON because compressors can exploit repeated JSON structure as well.

Therefore, the percentage byte advantage after compression is expected to be smaller than the raw-byte advantage.

### H3 — Structure-dependent serialization

A specialized C++ TOON encoder should be competitive on regular flat records but may incur greater overhead on nested objects, arrays, null handling, and dynamic memory operations.

### H4 — Compression-cost crossover

At higher Brotli levels, the computational cost of compression may dominate the request. TOON's smaller input may then reduce compression work enough to compensate for serialization overhead.

### H5 — End-to-end transport benefit

If the reduction in transmitted bytes is sufficiently large, TOON should reduce complete HTTP latency even when its serializer is slower.

### H6 — Cross-source robustness

If the TOON advantage remains when the output format is generated from the opposite source representation, the result is less likely to be caused simply by source-format locality.

### H7 — Cache amplification

When serialization and compression are removed or amortized by caching, TOON's compact representation should become more directly visible in cache-serving latency and bandwidth-related behavior.

---

# 5. Experimental Matrix

The format/compression benchmark varies four major dimensions.

| Dimension | Values |
|---|---|
| Format | JSON, TOON |
| Structure | Flat, Nested |
| Compression | Identity, Brotli 5, Brotli 9, Brotli 11 |
| Source | Native (primary), Cross (opposite DB) |

This produces the complete format/compression matrix used in the final research collection.

A separate cache matrix evaluates:

| Cache dimension | Values |
|---|---|
| Structure | Flat, Nested |
| Cache mode | Canonical cache, Native cache |
| Format | JSON, TOON |

---

# 6. Dataset Design

Every benchmark workload contains exactly:

```text
100,000 records
```

The datasets are generated deterministically and stored as JSON and TOON source files.

## 6.1 Flat schema

```text
[100000]{id,name,age,city}:
```

Representative data:

```text
1,QAHFTR,52,Mumbai
2,PACGPO,63,Bhopal
3,KLHWTE,45,Kolkata
4,HFTCJJ,40,Surat
5,GBLDXC,36,Surat
6,XJEBRU,23,Pune
7,WJLVEJ,60,Jaipur
8,SRBQNG,47,Nagpur
9,HYRFIT,21,Pune
10,VUKBXO,63,Kolkata
```

The flat dataset isolates the benefits of schema-level compactness in a highly regular record layout.

## 6.2 Nested schema

```text
[100000]{id,name,address{city,zip},tags}:
```

Representative data:

```text
1,QAHFTR,{Bhopal,191161},[2]{trial,vip}
2,AFNAFQ,{Bhopal,539898},null
3,FPVAUS,{Kolkata,391369},[2]{new,vip}
4,YICCWP,{Delhi,865179},null
5,LDXCHQ,{Kolkata,705397},null
6,EBRUZW,{Mumbai,498591},[2]{flagged,new}
7,QJJFGY,{Mumbai,738720},null
8,QNGMHY,{Bhopal,330283},null
```

The nested workload deliberately introduces:

- nested objects
- arrays
- nullable fields
- additional structural traversal
- more complex memory handling

This makes it useful for testing whether TOON's compact representation remains advantageous when encoding itself becomes more complicated.

---

# 7. TOON Serializer Implementation

A central design decision was to use a **native C++ TOON serializer** rather than a slow Python reference implementation.

The serializer is exposed to the Python API through a compiled Python extension.

```text
Python API
    │
    │ Python/C++ extension
    ▼
Native C++ TOON encoder
    │
    ▼
TOON byte/string representation
```

The implementation was optimized specifically for the structured benchmark data used in this study.

This matters for fairness.

A benchmark comparing a production-style JSON implementation against an intentionally slow prototype encoder would measure implementation quality rather than format behavior. The C++ implementation therefore attempts to provide a practical TOON serving path for the chosen workload.

At the same time, this means that the serialization measurements should be interpreted as measurements of the **implemented C++ TOON encoder**, not as a universal upper bound on TOON's possible performance.

The nested serializer remains an important area for future optimization, particularly around traversal, allocation, buffer management, and nested object/array handling.

---

# 8. System Architecture

The project is split into a backend API, native serializer, data-generation layer, and browser-based benchmark frontend.

```text
                         ┌──────────────────────────┐
                         │     Benchmark Dashboard  │
                         │       Frontend UI        │
                         └────────────┬─────────────┘
                                      │
                                      │ HTTP benchmark request
                                      ▼
                         ┌──────────────────────────┐
                         │       API Service        │
                         │        main.py           │
                         └────────────┬─────────────┘
                                      │
                    ┌─────────────────┼─────────────────┐
                    │                 │                 │
                    ▼                 ▼                 ▼
             ┌────────────┐   ┌──────────────┐   ┌──────────────┐
             │ DB / Data  │   │ Serialization │   │ Compression  │
             │ Retrieval  │   │              │   │              │
             └────────────┘   └──────┬───────┘   └──────────────┘
                                     │
                              ┌──────▼───────┐
                              │ Native C++   │
                              │ TOON Encoder │
                              └──────┬───────┘
                                     │
                                     ▼
                              ┌──────────────┐
                              │ HTTP Response│
                              └──────────────┘
```

For a normal uncached request, the benchmark therefore observes the combined cost of retrieval, representation generation, optional compression, server-side overhead, and HTTP delivery.

For a cached request, previously generated response material can be reused, changing the hot path substantially.

---

# 9. Backend Architecture

The backend lives under:

```text
deploy/api/
```

Its main responsibilities are:

1. expose the benchmark API
2. load or retrieve benchmark data
3. select JSON or TOON
4. invoke the native TOON serializer
5. apply optional Brotli compression
6. measure timing components
7. return benchmark results
8. expose cache behavior for the cache experiments

The main entry point is:

```text
deploy/api/main.py
```

The service is deployed on stable Render Pro-tier infrastructure for the final research collection.

The benchmark uses a persistent HTTP connection throughout a run so that repeated connection establishment does not dominate the measurements.

---

# 10. Native and Cross-Source Experimental Modes

The format/compression experiments contain two source conditions.

## Native (primary)

The requested output format is associated with the primary source representation used by the benchmark configuration.

This approximates the normal serving path.

## Cross (opposite DB)

The benchmark deliberately uses the **opposite source representation**.

For example, a TOON response can be generated from the JSON-side source, while a JSON response can be generated from the TOON-side source.

The purpose is methodological rather than architectural.

A native-only benchmark can accidentally favor a representation because the upstream data is already stored in or prepared for that representation.

The Cross condition removes some of that locality.

Therefore:

```text
Native:
source representation → requested output

Cross:
opposite source representation → requested output
```

If a performance relationship survives both conditions, there is stronger evidence that the result is associated with the serving representation and processing pipeline rather than simply with source-format preparation.

---

# 11. Why Cross-Source Testing Matters for Legacy Systems

Cross-source testing also represents a realistic integration scenario.

A company may have:

```text
existing database
       ↓
existing JSON application code
       ↓
existing API ecosystem
```

and may not want to rewrite its entire data layer merely to experiment with a new wire representation.

A practical deployment could instead introduce TOON at a boundary:

```text
Legacy JSON-oriented application
              ↓
       representation layer
              ↓
        TOON response
              ↓
          HTTP client
```

The existing application and database can remain JSON-oriented while the response representation is changed at the serving boundary.

The Cross experiments therefore investigate whether a format can retain benefits even when the source representation is not already aligned with the requested output.

This is particularly relevant to adoption: **a representation format that requires an entire application rewrite has a much higher migration cost than one that can be introduced as a serving-layer optimization.**

---

# 12. Compression Pipeline

The benchmark evaluates four compression regimes.

## Identity

No compression is applied.

This isolates the raw representation and transport effects.

## Brotli 5

Moderate compression.

This represents a middle regime where compression is meaningful but not pushed toward maximum computational cost.

## Brotli 9

Stronger compression.

At this point compression CPU time becomes a significant component of the serving pipeline.

## Brotli 11

Very high Brotli compression effort.

This configuration is particularly useful for observing whether TOON's smaller pre-compressed input can reduce compressor work enough to compensate for serialization overhead.

The benchmark therefore does not assume:

```text
smaller payload = faster request
```

Instead it measures:

```text
serialization cost
+
compression cost
+
transport cost
```

together.

---

# 13. Benchmark Timing Model

The measured serving path can be represented conceptually as:

```text
T_HTTP =
    T_DB
  + T_serialization
  + T_compression
  + T_overhead
  + T_transport
```

The implementation records server-side components separately where possible.

The API also records UTF-8 encoding time through:

```text
X-Utf8-Encoding-Time-Ms
```

For the main result matrix, UTF-8 encoding time is included in the reported server-processing/overhead path rather than being displayed as a separate table row.

Consequently, small differences between the sum of displayed timing components and server-processing time can include UTF-8 encoding and other server-side overhead.

---

# 14. Benchmark Protocol

The final research configuration uses:

```text
Records per workload:       100,000
Warm-up requests:           3
Measured repetitions:       100
Trials:                     1
Random seed:                42
HTTP connection:            Persistent
Request ordering:           Randomized pairs
```

For each randomized pair, the benchmark selects one direction:

```text
JSON → TOON
```

or

```text
TOON → JSON
```

The direction is seeded to make the ordering reproducible.

The three warm-up requests are discarded.

The following 100 repetitions form the reported sample.

The final collection was performed on stable Pro-tier deployment infrastructure after constrained-resource results were discarded.

---

# 15. Metrics Collected

## Representation metrics

### Raw bytes

The number of bytes in the uncompressed response.

### Compressed bytes

The number of bytes after the selected compression configuration.

These measurements directly answer the compactness questions.

## Server metrics

### DB retrieval mean

Mean database/data-source retrieval time.

### Serialization mean

Mean time required to produce the JSON or TOON representation.

### Compression mean

Mean time spent in the selected compression stage.

### Server processing mean

Overall measured server-side processing time, including the relevant overhead.

## HTTP metrics

### HTTP latency mean

Mean end-to-end request latency.

### HTTP latency p50

Median observed HTTP latency.

### HTTP latency p95

Tail latency at the 95th percentile.

## Cache metrics

The cache experiments additionally measure:

- cache miss latency
- cache hit rate
- bytes
- warm-cache mean
- warm-cache p50
- warm-cache p90
- warm-cache p95
- warm-cache p99
- warm-cache standard deviation
- warm-cache minimum
- warm-cache maximum
- sample count

---

# 16. Cache Experiment

Caching is treated as a separate experimental regime because it can remove expensive response-generation work from the repeated-request path.

The benchmark evaluates:

```text
Canonical cache
Native cache
```

for flat and nested datasets.

The cache experiment is not intended to claim that caching inherently makes one serialization format faster.

Instead, it asks:

> Once response generation has been materialized, does the compact representation provide a stronger serving advantage?

This isolates a different part of the format tradeoff.

For example, the canonical flat cache experiment reported:

```text
Cache miss latency
JSON : 344.650 ms
TOON :  66.003 ms
```

with an approximately:

```text
80.85% improvement
```

The warm-cache mean was:

```text
JSON : 302.760 ms
TOON :  33.650 ms
```

with an approximately:

```text
88.89% improvement
```

The warm-cache p95 was also substantially lower for TOON.

These results illustrate why caching deserves independent analysis rather than being treated as another serialization measurement.

---

# 17. Why the Results Are Not Reduced to Payload Size

One of the main purposes of this repository is to avoid a misleading benchmark methodology.

A format can have:

```text
smaller bytes
+
slower serialization
```

and still be faster end-to-end.

Conversely, a format can have:

```text
smaller bytes
+
more expensive compression
```

and lose at a particular compression level.

The benchmark therefore tracks the complete pipeline.

The results demonstrate a computation-versus-transport tradeoff:

```text
                  More compact format
                         │
                         ▼
                fewer bytes to compress
                         │
                         ▼
                fewer bytes to transmit
                         │
                         ▼
                 lower transport cost
```

against:

```text
             additional serialization work
                         │
                         ▼
                additional CPU time
```

The observed winner depends on which side dominates.

---

# 18. Main Observations

The final measurements show several consistent patterns.

### Raw representation

TOON reduces raw bytes by roughly **58–62%** for the evaluated workloads.

### Identity / no compression

With no compression, the representation-size advantage is exposed directly to the transport layer.

The flat native identity configuration, for example, reports approximately:

```text
59.98% raw-byte reduction
67.68% mean HTTP-latency improvement
```

The nested identity configurations also show substantial HTTP improvements despite the slower nested TOON serializer.

### Nested serialization

The nested workload exposes the current C++ TOON encoder's overhead more strongly.

The serializer must handle:

- nested objects
- arrays
- nulls
- additional structural traversal
- more complex output construction

Therefore, TOON serialization can be substantially slower than JSON on nested workloads.

This is an implementation cost, not evidence that compact representation itself requires more CPU in every possible encoder.

### Brotli

At moderate compression, the JSON and TOON byte sizes move closer together.

At higher Brotli levels, however, the compressor becomes a major component of request cost.

The smaller TOON input can then reduce compression work sufficiently to reverse the balance.

The Brotli 11 configurations demonstrate this effect particularly strongly.

---

# 19. Important Performance Interpretation

The experiment reveals a **compression-dependent crossover**.

At low or moderate compression:

```text
representation advantage
        ↓
transport advantage
```

is visible, but serialization and compression costs remain significant.

At aggressive compression:

```text
compression CPU
        ↓
dominant pipeline cost
```

becomes increasingly important.

TOON's smaller input can then produce a computational saving inside the compressor.

This means that the net benefit of TOON is not a monotonic function of compression level.

The relevant question is:

> How much additional CPU is spent producing and compressing the representation, and how much CPU/network work is avoided because the representation is smaller?

The benchmark was designed specifically to expose this interaction.

---

# 20. Why the Native C++ Encoder Is Important

The experiment should not be interpreted as:

> "The current TOON serializer is already optimal."

It is the opposite.

The current implementation provides a practical C++ baseline and allows the experiment to quantify how much serialization overhead currently exists.

The most interesting engineering opportunity is therefore:

```text
TOON compactness
       +
optimized C++ encoder
       +
existing compression pipeline
       +
HTTP transport
```

If serializer overhead is reduced, the representation advantage observed in the current benchmark could become stronger.

The nested workload is particularly valuable here because it identifies the part of the implementation where optimization is most likely to matter.

---

# 21. Repository Structure

The repository is organized around the separation between the deployed API, native serializer, datasets, frontend, and deployment configuration.

```text
toon-benchmark/
│
├── deploy/
│   │
│   ├── api/
│   │   │
│   │   ├── data/
│   │   │   ├── dataset_flat.json
│   │   │   ├── dataset_flat.toon
│   │   │   ├── dataset_nested.json
│   │   │   └── dataset_nested.toon
│   │   │
│   │   ├── toon_cpp/
│   │   │   ├── build/
│   │   │   ├── toon_cpp.egg-info/
│   │   │   ├── pyproject.toml
│   │   │   ├── setup.py
│   │   │   └── toon_cpp.cpp
│   │   │
│   │   ├── toon_cpp.cpython-312-win_amd64.pyd
│   │   ├── build_data.py
│   │   ├── main.py
│   │   └── requirements.txt
│   │
│   └── frontend/
│       │
│       ├── static/
│       │
│       ├── toon_cpp/
│       │   ├── toon_cpp.egg-info/
│       │   ├── pyproject.toml
│       │   ├── setup.py
│       │   ├── toon_cpp.cpython-312-win_amd64.pyd
│       │   └── toon_cpp.cpp
│       │
│       ├── main.py
│       └── requirements.txt
│
├── .gitignore
├── README.md
└── render.yaml
```

> **Note:** the compiled `.pyd` and build/egg-info directories are platform/build artifacts. A fresh environment may need to rebuild the native extension for its own Python version and operating system.

---

# 22. File-by-File Description

## `deploy/api/main.py`

The primary backend/API service.

It handles the benchmark request path and exposes the functionality required by the benchmark dashboard.

Conceptually:

```text
request
  ↓
configuration
  ↓
data retrieval
  ↓
JSON / TOON serialization
  ↓
optional Brotli compression
  ↓
timing collection
  ↓
HTTP response
```

---

## `deploy/api/build_data.py`

Dataset-generation utility.

It generates the controlled benchmark workloads and maintains the JSON/TOON representations used by the experiment.

The generated files are placed under:

```text
deploy/api/data/
```

---

## `deploy/api/data/dataset_flat.json`

JSON representation of the 100,000-record flat dataset.

---

## `deploy/api/data/dataset_flat.toon`

TOON representation of the same logical flat dataset.

---

## `deploy/api/data/dataset_nested.json`

JSON representation of the 100,000-record nested dataset.

---

## `deploy/api/data/dataset_nested.toon`

TOON representation of the same logical nested dataset.

---

## `deploy/api/toon_cpp/toon_cpp.cpp`

Native C++ implementation of the TOON serializer exposed to Python.

This is one of the most important files in the benchmark because serialization time is explicitly measured as part of the serving pipeline.

---

## `deploy/api/toon_cpp/setup.py`

Build/install configuration for the native C++ Python extension.

---

## `deploy/api/toon_cpp/pyproject.toml`

Python build-system metadata for the C++ extension.

---

## `deploy/api/toon_cpp/build/`

Generated build artifacts for the native extension.

---

## `deploy/api/toon_cpp/toon_cpp.egg-info/`

Python packaging metadata generated during extension packaging/building.

---

## `deploy/api/toon_cpp.cpython-312-win_amd64.pyd`

Compiled Python extension for the corresponding CPython/platform build.

A platform-specific binary should not be assumed to work on every operating system or Python version.

---

## `deploy/frontend/main.py`

Frontend/dashboard application.

It provides the interactive benchmark interface used to configure and execute experimental cases and display the resulting measurement table.

---

## `deploy/frontend/static/`

Static frontend assets.

---

## `deploy/frontend/toon_cpp/`

Frontend-side copy/build configuration associated with the native TOON extension used by the project structure.

It contains the corresponding C++ source and Python build metadata.

---

## `render.yaml`

Deployment configuration for the Render-based deployment.

The final research measurements were collected from stable Pro-tier deployment infrastructure.

---

# 23. Benchmark Dashboard

The dashboard exposes the benchmark dimensions directly rather than hiding them behind a fixed script.

Depending on the selected case, the interface allows configuration of:

- case type
- flat/nested structure
- record count
- compression mode
- Brotli level
- database/source mode
- cache mode
- warm-up count
- number of repetitions
- trial count
- seed

A completed benchmark reports the JSON and TOON measurements together.

For format/compression experiments, the result table includes:

```text
Metric
JSON
TOON
Abs diff
Improvement
```

For cache experiments, the result table additionally exposes cache-specific statistics.

The interface also provides:

```text
Download JSON
Download CSV (samples)
```

so the measurements can be retained outside the dashboard.

---

# 24. Result Artifact Naming

The result screenshots use a deterministic naming scheme.

### Format/compression results

```text
plain_<structure>_n100000_<compression>_<source>.png
```

Examples:

```text
plain_flat_n100000_none_native.png
plain_flat_n100000_none_cross.png
plain_flat_n100000_brotli5_native.png
plain_flat_n100000_brotli5_cross.png
plain_nested_n100000_brotli11_native.png
plain_nested_n100000_brotli11_cross.png
```

### Cache results

```text
cache_<structure>_n100000_<cache-mode>.png
```

Examples:

```text
cache_flat_n100000_canonical.png
cache_flat_n100000_native.png
cache_nested_n100000_canonical.png
cache_nested_n100000_native.png
```

This naming convention makes every result independently traceable to its experimental condition.

---

# 25. Research Paper

The associated research paper is:

> **TOON vs. JSON: An Experimental Evaluation of Serialization, Compression, and End-to-End HTTP Performance**

The paper documents:

- research motivation
- research questions
- hypotheses
- system architecture
- dataset construction
- C++ serializer implementation
- experimental protocol
- Native/Cross methodology
- compression results
- cache-layer results
- complete result matrix
- discussion
- engineering implications
- limitations
- future work

The figures used in the paper correspond directly to the benchmark result artifacts.

---

# 26. Reproducing the Experiment

Clone the repository:

```bash
git clone https://github.com/debugaditya/toon-benchmark.git
cd toon-benchmark
```

Move to the API implementation:

```bash
cd deploy/api
```

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

The native TOON extension is built from:

```text
deploy/api/toon_cpp/
```

Depending on the platform, the extension may need to be rebuilt.

The dataset generator is:

```bash
python build_data.py
```

The API entry point is:

```bash
python main.py
```

The exact deployment configuration and benchmark UI are contained in the repository.

---

# 27. Interpreting the Benchmark Correctly

These results should be interpreted as a **systems benchmark**, not a universal ranking of serialization formats.

The benchmark answers:

> What happens when the evaluated JSON and TOON implementations are placed into this particular API serving pipeline under these controlled workload and deployment conditions?

It does not establish:

- that every TOON implementation is faster than every JSON implementation
- that every workload will show the same byte reduction
- that every network environment will show the same latency reduction
- that the current C++ serializer is optimal
- that one compression level is universally best

Instead, the experiment demonstrates how the interaction between representation, implementation, compression, caching, and transport can change the final result.

---

# 28. Limitations

The main limitations are:

1. The workloads are controlled synthetic datasets.
2. Each final configuration uses one trial with 100 measured repetitions.
3. The C++ TOON serializer is specialized for the benchmark data shapes.
4. The measurements depend on the deployed infrastructure and network conditions.
5. Only the selected compression configurations were evaluated.
6. Additional independent trials would provide stronger statistical evidence.
7. Different schemas may produce different serialization and compression behavior.
8. The experiment does not claim a universal TOON-versus-JSON performance ranking.

The most direct implementation limitation is the cost of nested TOON serialization.

This is also a useful future-work direction because optimization of the encoder can be evaluated using the exact same benchmark matrix.

---

# 29. Future Work

The benchmark can be extended in several directions.

### Serializer optimization

- reduce allocations
- improve buffer management
- optimize nested traversal
- optimize array handling
- reduce string-copying
- improve schema-aware encoding
- benchmark allocator behavior

### Workload expansion

- larger record counts
- deeper nesting
- wider schemas
- different null distributions
- different string lengths
- different array sizes
- realistic production datasets

### Compression expansion

- gzip
- zstd
- additional Brotli configurations
- compression-level sweeps

### Systems evaluation

- concurrent clients
- requests per second
- CPU utilization
- memory utilization
- bandwidth consumption
- multiple persistent connections
- connection pooling
- varying network conditions

### Statistical validation

- multiple independent trials
- confidence intervals
- variance analysis
- significance testing
- repeated deployment runs

### Cache research

- different cache capacities
- different hit/miss distributions
- cache eviction policies
- serialized-response caching
- compressed-response caching
- multi-client cache behavior

---

# 30. Why This Repository Exists

The main objective is reproducibility.

A claim such as:

> "TOON is 60% smaller than JSON"

is incomplete as a systems result.

The more meaningful questions are:

```text
How much CPU does serialization require?
How much CPU does compression require?
How many bytes remain after compression?
How does the result affect HTTP latency?
What happens for nested data?
What happens when the source is not native to the output format?
What happens after the response is cached?
```

This repository provides the implementation and benchmark structure required to investigate those questions.

The benchmark therefore treats serialization format as one component of a larger API performance pipeline.

---

# 31. Contact and Experiment Source

For questions about the implementation, experimental design, measurements, or possible extensions:

**Email:** `adibarmola@gmail.com`

The complete experiment source and benchmark implementation are available at:

**Experiment Setup:**  
https://github.com/debugaditya/toon-benchmark


# 32. Clone and Deploy the Benchmark

This section describes the recommended deployment procedure for reproducing the deployed benchmark.

The repository contains **two Render web services**:

```text
toon-benchmark-api
        │
        │ HTTP API
        ▼
toon-benchmark-frontend
        │
        ▼
Benchmark Dashboard
```

Both services are configured for the **Singapore** region in `render.yaml`.

The repository is designed to be deployed as a **Render Blueprint** first. After the Blueprint creates both services, upgrade **both services to the Pro plan** before running the final research benchmark.

---

## 32.1 Clone the Repository

Clone the experiment repository:

```bash
git clone https://github.com/debugaditya/toon-benchmark.git
```

Enter the repository:

```bash
cd toon-benchmark
```

The repository should contain the deployment configuration:

```text
toon-benchmark/
├── deploy/
├── render.yaml
├── README.md
└── ...
```

The `render.yaml` file defines the two services required by the benchmark.

---

## 32.2 Deploy Using the Render Blueprint

The easiest way to deploy the complete system is to use the Blueprint defined by:

```text
render.yaml
```

In Render:

1. Open the Render dashboard.
2. Select **New**.
3. Select **Blueprint**.
4. Connect the GitHub repository:

```text
debugaditya/toon-benchmark
```

5. Select the repository.
6. Select the branch containing `render.yaml`.
7. Review the two services detected from the Blueprint.
8. Apply/create the Blueprint.

The Blueprint creates:

```text
toon-benchmark-api
toon-benchmark-frontend
```

Both services are initially declared with:

```yaml
plan: free
```

This is intentional so that the Blueprint can create the complete deployment from the repository configuration.

---

## 32.3 Upgrade Both Services to Pro

After the Blueprint has successfully created both services, upgrade:

```text
toon-benchmark-api
```

and

```text
toon-benchmark-frontend
```

to the **Pro** plan.

The final research measurements should not be collected from the free-tier instances.

The final benchmark used stable Pro-tier infrastructure because constrained/shared resources can introduce additional variability into:

- CPU time
- compression time
- serialization time
- HTTP latency
- tail latency
- cache behavior

Therefore the intended research deployment is:

```text
                 Singapore region
                       │
             ┌─────────┴─────────┐
             │                   │
             ▼                   ▼
       API — Pro tier      Frontend — Pro tier
             │                   │
             └────── HTTP ───────┘
```

---

## 32.4 Configure the Frontend with the Backend URL

The frontend needs to know where the API service is running.

The API service created by Render receives its own public URL, for example:

```text
API_BASE_URL.com
```

That URL must be supplied to the frontend through:

```text
API_BASE_URL
```

The corresponding section of `render.yaml` is:

```yaml
  - type: web
    name: toon-benchmark-frontend
    runtime: python
    plan: free
    region: singapore
    rootDir: frontend
    buildCommand: pip install -r requirements.txt && cd toon_cpp && python setup.py build_ext --inplace && cp toon_cpp*.so .. && cd .. && python -c "import toon_cpp; print('TOON MODULE:', toon_cpp.__file__); print('TOON ATTRS:', [x for x in dir(toon_cpp) if 'encode' in x]); assert hasattr(toon_cpp, 'encode_flat'); assert hasattr(toon_cpp, 'encode_nested')"
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars:
      - key: API_BASE_URL
        value: API_BASE_URL.com
```

### Important

The URL above is the backend URL used in the documented deployment configuration.

If Render assigns a different URL to the newly created API service, replace:

```yaml
value: API_BASE_URL.com
```

with the actual URL of:

```text
toon-benchmark-api
```

Do **not** put the frontend URL in `API_BASE_URL`.

The relationship must be:

```text
API_BASE_URL
      │
      ▼
toon-benchmark-api
```

not:

```text
API_BASE_URL
      │
      ▼
toon-benchmark-frontend
```

---

## 32.5 Render Service Configuration

The API service is defined by the following deployment configuration:

```yaml
services:
  - type: web
    name: toon-benchmark-api
    runtime: python
    plan: free
    region: singapore
    rootDir: api
    buildCommand: pip install -r requirements.txt && cd toon_cpp && python setup.py build_ext --inplace && cp toon_cpp*.so .. && cd .. && python -c "import toon_cpp; print('TOON MODULE:', toon_cpp.__file__); print('TOON ATTRS:', [x for x in dir(toon_cpp) if 'encode' in x]); assert hasattr(toon_cpp, 'encode_flat'); assert hasattr(toon_cpp, 'encode_nested')"
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
```

The frontend service is:

```yaml
  - type: web
    name: toon-benchmark-frontend
    runtime: python
    plan: free
    region: singapore
    rootDir: frontend
    buildCommand: pip install -r requirements.txt && cd toon_cpp && python setup.py build_ext --inplace && cp toon_cpp*.so .. && cd .. && python -c "import toon_cpp; print('TOON MODULE:', toon_cpp.__file__); print('TOON ATTRS:', [x for x in dir(toon_cpp) if 'encode' in x]); assert hasattr(toon_cpp, 'encode_flat'); assert hasattr(toon_cpp, 'encode_nested')"
    startCommand: uvicorn main:app --host 0.0.0.0 --port $PORT
    healthCheckPath: /health
    envVars:
      - key: API_BASE_URL
        value: API_BASE_URL.com
```

The `plan: free` values in the repository are the initial Blueprint configuration. For the research deployment, both services should subsequently be upgraded to Pro in Render.

---

## 32.6 What the Build Command Does

The build command performs several operations.

First, it installs the Python dependencies:

```bash
pip install -r requirements.txt
```

It then enters the native serializer directory:

```bash
cd toon_cpp
```

and builds the C++ Python extension:

```bash
python setup.py build_ext --inplace
```

The compiled extension is then copied into the service directory:

```bash
cp toon_cpp*.so ..
```

This makes the native module importable by the Python API.

Finally, the deployment runs an explicit verification step:

```python
import toon_cpp
print('TOON MODULE:', toon_cpp.__file__)
print('TOON ATTRS:', [x for x in dir(toon_cpp) if 'encode' in x])
assert hasattr(toon_cpp, 'encode_flat')
assert hasattr(toon_cpp, 'encode_nested')
```

This is important because the benchmark depends on the native C++ implementation.

The deployment should fail rather than silently continue if either required encoder is unavailable.

---

## 32.7 Start Command and Health Check

Both services are started with Uvicorn:

```bash
uvicorn main:app --host 0.0.0.0 --port $PORT
```

Render supplies the `$PORT` environment variable.

The deployment also exposes:

```text
/health
```

as the health-check endpoint.

A successful health check confirms that the service is running and reachable by the deployment platform.

---

## 32.8 Verify the Deployment

After deployment, verify the backend first.

Open the backend service URL:

```text
API_BASE_URL.com
```

Then check:

```text
API_BASE_URL.com/health
```

The health endpoint should respond successfully.

Next open the frontend service URL.

The dashboard should load and allow benchmark configuration.

Before collecting research measurements, verify that:

```text
Frontend
   │
   │ API_BASE_URL
   ▼
Backend
   │
   ├── dataset loading
   ├── JSON serialization
   ├── native C++ TOON serialization
   ├── Brotli compression
   ├── timing instrumentation
   └── benchmark response
```

is functioning correctly.

---

## 32.9 Final Research Deployment Checklist

Before running the final experiment, confirm all of the following:

- [ ] Repository cloned from `debugaditya/toon-benchmark`
- [ ] Render Blueprint deployed successfully
- [ ] `toon-benchmark-api` exists
- [ ] `toon-benchmark-frontend` exists
- [ ] Both services are in the Singapore region
- [ ] Both services have been upgraded to Pro
- [ ] Backend `/health` endpoint is healthy
- [ ] Native `toon_cpp` extension builds successfully
- [ ] `encode_flat` exists
- [ ] `encode_nested` exists
- [ ] Frontend `API_BASE_URL` points to the backend
- [ ] Frontend can successfully communicate with the API
- [ ] Benchmark dashboard loads
- [ ] Dataset generation/loading is working
- [ ] Warm-up configuration is set correctly
- [ ] Repetitions are set to 100
- [ ] Seed is set to 42
- [ ] Persistent HTTP connection is used
- [ ] Final measurements are collected only after deployment is stable

---

## 32.10 Running the Research Benchmark After Deployment

Once deployment is verified, use the dashboard to run the experimental matrix.

For format/compression experiments, configure:

```text
Records:       100,000
Warm-up:       3
Repeats:       100
Trials:        1
Seed:          42
```

Run the required combinations of:

```text
Flat
Nested
```

with:

```text
Identity
Brotli 5
Brotli 9
Brotli 11
```

and:

```text
Native
Cross
```

as specified by the experimental matrix.

Then run the cache configurations separately.

The benchmark dashboard provides downloadable JSON and CSV outputs for retaining the raw measurements.

---

## 32.11 Deployment Architecture Summary

The complete deployment can be summarized as:

```text
                         GitHub Repository
                                │
                                ▼
                         Render Blueprint
                                │
                 ┌──────────────┴──────────────┐
                 │                             │
                 ▼                             ▼
       toon-benchmark-api            toon-benchmark-frontend
          Singapore / Pro               Singapore / Pro
                 │                             │
                 │                             │
                 │       API_BASE_URL           │
                 └─────────────────────────────┘
                                │
                                ▼
                         Benchmark API
                                │
              ┌─────────────────┼─────────────────┐
              │                 │                 │
              ▼                 ▼                 ▼
           Dataset         C++ TOON          Brotli
           Retrieval       Serializer       Compression
              │                 │                 │
              └─────────────────┼─────────────────┘
                                ▼
                         Timing Instrumentation
                                │
                                ▼
                          HTTP Response
                                │
                                ▼
                         Dashboard Results
```

This architecture keeps the benchmark frontend and API as separate services while allowing the frontend to communicate with the backend through the configurable `API_BASE_URL`.

The separation also makes it possible to independently scale or replace either component without changing the core experimental methodology.

---

## License / Usage

See the repository for the applicable project files and configuration.

If you use this benchmark or extend the experimental matrix, please preserve the workload conditions and clearly document any changes to:

- serializer implementation
- dataset
- compression level
- cache configuration
- infrastructure
- repetitions
- trial count
- measurement methodology

That distinction is important when comparing new measurements with the results reported in the accompanying research paper.

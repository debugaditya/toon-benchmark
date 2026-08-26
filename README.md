TOON vs JSON Benchmark

Experimental Evaluation of Serialization, Compression, Caching, and End-to-End HTTP Performance

This repository contains the experimental implementation and benchmark artifacts for evaluating TOON (Token-Oriented Object Notation) against JSON for structured API responses.

The experiment investigates whether TOON's compact representation translates into practical systems-level benefits when serialization, compression, server-side processing, HTTP delivery, and caching are considered together.

The benchmark was designed to evaluate more than serialized byte count. It measures the serving pipeline and studies how the relative advantage changes with data structure, compression level, source condition, and caching.

Author

Aditya Narayan Barmola
Netaji Subhas University of Technology (NSUT)
New Delhi, India

Email: adibarmola@gmail.com

Experiment Setup:
https://github.com/debugaditya/toon-benchmark

1. Research Objective

Modern APIs commonly use JSON because of its interoperability, mature tooling, and widespread adoption. However, JSON repeatedly encodes structural information such as object keys and delimiters.

TOON represents structured tabular data using a more compact schema-oriented representation.

A flat workload in this experiment is represented as:

[100000]{id,name,age,city}:
  1,QAHFTR,52,Mumbai
  2,PACGPO,63,Bhopal
  3,KLHWTE,45,Kolkata

A nested workload is represented as:

[100000]{id,name,address{city,zip},tags}:
  1,QAHFTR,{Bhopal,191161},[2]{trial,vip}
  2,AFNAFQ,{Bhopal,539898},null

The central research question is:

Does TOON's reduction in representation size translate into better real-world API performance once serialization, compression, server processing, and HTTP delivery are included?

A second question is:

Does TOON remain advantageous after general-purpose compression reduces much of the representation-level redundancy?

The experiment also evaluates whether caching exposes a stronger benefit from TOON's compact representation by removing expensive generation work from repeated requests.

2. What This Repository Contains

The repository contains:

benchmark API server

native C++ TOON serializer

Python/C++ integration through a compiled extension

flat and nested datasets

JSON and TOON source files

Brotli compression experiments

cache-layer experiments

Native and Cross-source experiments

benchmark result generation

CSV/JSON result export

frontend benchmark dashboard

research result figures

LaTeX research-paper source

The repository is intended to make the experimental implementation inspectable rather than presenting only the final measurements.

3. Experimental Design

The benchmark compares:

Formats

JSON

TOON

Workload structures

Flat

Nested

Compression

Identity / no compression

Brotli 5

Brotli 9

Brotli 11

Source modes

Native (primary)

Cross (opposite DB)

Cache modes

Canonical cache

Native cache

Each format/compression configuration uses the same logical dataset and benchmark conditions.

4. Dataset

Each workload contains exactly:

100,000 records

Two structures are evaluated.

4.1 Flat Dataset

Schema:

[100000]{id,name,age,city}

Example:

[100000]{id,name,age,city}:
  1,QAHFTR,52,Mumbai
  2,PACGPO,63,Bhopal
  3,KLHWTE,45,Kolkata
  4,HFTCJJ,40,Surat
  5,GBLDXC,36,Surat

The flat workload represents a regular tabular API response with repeated fields and minimal nesting.

4.2 Nested Dataset

Schema:

[100000]{id,name,address{city,zip},tags}

Example:

[100000]{id,name,address{city,zip},tags}:
  1,QAHFTR,{Bhopal,191161},[2]{trial,vip}
  2,AFNAFQ,{Bhopal,539898},null
  3,FPVAUS,{Kolkata,391369},[2]{new,vip}
  4,YICCWP,{Delhi,865179},null

The nested workload introduces nested objects, arrays, and nullable values. It therefore provides a more demanding test of the TOON encoder.

5. TOON Serializer

The benchmark uses a native C++ TOON serializer exposed to the Python API through a compiled Python extension.

The serializer was implemented and optimized for the structured data used in this experiment. This was done to avoid comparing a native JSON implementation against an artificially slow Python-level TOON prototype.

The implementation should therefore be understood as a workload-specialized C++ TOON encoder. The reported serialization numbers characterize the implementation used in this study and do not represent a theoretical upper bound for every possible TOON implementation.

Relevant files include:

deploy/api/toon_cpp/
├── toon_cpp.cpp
├── setup.py
├── pyproject.toml
└── toon_cpp*.pyd

6. System Architecture

The experimental serving path is conceptually:

                 ┌──────────────────────────┐
                 │      Benchmark Client    │
                 │ randomized request pairs │
                 └────────────┬─────────────┘
                              │
                              │ HTTP
                              ▼
                 ┌──────────────────────────┐
                 │       API Service        │
                 │         main.py          │
                 └────────────┬─────────────┘
                              │
              ┌───────────────┼────────────────┐
              │               │                │
              ▼               ▼                ▼
       ┌─────────────┐ ┌──────────────┐ ┌─────────────┐
       │ DB Retrieval│ │ Serialization│ │ Compression │
       └─────────────┘ └──────┬───────┘ └─────────────┘
                              │
                       ┌──────▼──────┐
                       │ TOON C++    │
                       │ Serializer  │
                       └─────────────┘
                              │
                              ▼
                         HTTP Response

The deployment used stable Render Pro-tier infrastructure for the final research collection. The benchmark was run server-side against the deployed API service using a persistent HTTP connection.

7. API Service

The API entry point is:

deploy/api/main.py

The service is responsible for:

receiving benchmark requests

selecting the workload

retrieving the requested data

selecting JSON or TOON

serializing the data

applying optional Brotli compression

recording timing information

returning the HTTP response

The frontend communicates with this service to execute benchmark configurations and display the measurements.

8. Dataset Generation

Dataset generation is handled by:

deploy/api/build_data.py

The generated source files are:

deploy/api/data/
├── dataset_flat.json
├── dataset_flat.toon
├── dataset_nested.json
└── dataset_nested.toon

The JSON and TOON files represent the same logical datasets in their respective formats.

9. Native and Cross-Source Modes

The format/compression experiments use two source modes.

Native (primary)

Native uses the primary data source associated with the benchmark configuration. It represents the normal serving path.

Cross (opposite DB)

Cross deliberately uses the opposite source representation.

The purpose is methodological: it reduces the possibility that a format receives an unfair advantage simply because the source data was already prepared in that same representation.

Cross therefore asks whether the observed output-format advantage persists when the upstream source representation is changed.

This is also relevant to legacy-system integration. Existing organizations may already have JSON-oriented data pipelines and may not want to rewrite their upstream application and database logic merely to introduce a different wire representation.

The experiment therefore evaluates TOON as a representation/serving layer rather than requiring an entirely TOON-native application stack.

10. Benchmark Request Methodology

Requests are fired server-side against the API service in randomized pairs:

JSON → TOON
or
TOON → JSON

The direction is seeded.

A single persistent HTTP connection is used throughout a run, reducing repeated connection-establishment effects.

The benchmark is therefore intended to compare the request/response processing path rather than repeatedly measuring connection setup.

11. Warm-up and Repetitions

The final research configuration uses:

Records:        100,000
Warm-up:        3
Measured runs:  100
Trials:         1
Seed:           42
Connection:     Persistent HTTP

The three warm-up requests are discarded.

The following 100 requests form the reported measurement sample.

12. Metrics

The benchmark records measurements at several stages.

Raw bytes

Uncompressed serialized response size.

Compressed bytes

Response size after Brotli compression.

DB retrieval mean

Time required to retrieve the dataset.

Serialization mean

Time required to serialize the retrieved data into JSON or TOON.

Compression mean

Time required to compress the serialized representation.

Server processing mean

Measured server-side processing time including the relevant server overhead.

HTTP latency

Complete request latency, reported as:

mean

p50

p95

The backend also records UTF-8 encoding time through:

X-Utf8-Encoding-Time-Ms

For the main result tables, UTF-8 encoding is folded into the server-processing/overhead path rather than shown as a separate row. Consequently, small differences between displayed DB, serialization, compression, and server-processing components can include UTF-8 encoding and other measured server-side overhead.

13. Timing Model

The benchmark treats end-to-end request time as a combination of:

database retrieval
+ serialization
+ compression
+ server-side overhead
+ HTTP/network delivery

This decomposition is important because a representation can be smaller while its encoder is slower.

The experiment therefore separates:

Representation advantage

from:

Implementation/computational cost

and finally evaluates whether the combined effect appears in actual HTTP latency.

14. Compression Experiments

Four compression regimes are evaluated:

Identity
Brotli 5
Brotli 9
Brotli 11

Identity represents the uncompressed baseline.

Brotli 5 represents moderate compression.

Brotli 9 represents stronger compression.

Brotli 11 represents a computationally expensive compression regime.

The purpose is to determine whether TOON's structural compactness remains useful after a general-purpose compressor is applied.

15. Cache-Layer Experiment

Caching is evaluated separately because caching can remove serialization and compression from the repeated-request hot path.

The cache benchmark records:

cache miss latency

cache hit rate

bytes

warm-cache mean latency

warm-cache p50

warm-cache p90

warm-cache p95

warm-cache p99

warm-cache standard deviation

warm-cache minimum/maximum

sample count

The cache configurations include:

Canonical cache
Native cache

The purpose is to determine whether TOON's compact representation becomes more directly beneficial once expensive response generation has been amortized.

16. Why Caching Matters

The cache experiment answers a different question from the serialization benchmark.

It asks:

If response generation is already materialized, does TOON's smaller representation provide a stronger serving advantage?

This is particularly important for repeated API responses.

The nested TOON serializer showed a measurable serialization penalty in the evaluated implementation. A cache can remove or amortize that cost, potentially exposing the payload and transport advantages much more directly.

17. Key Experimental Findings

The measured results show several important patterns.

Identity / No Compression

TOON reduces raw payload size substantially across the evaluated flat and nested workloads.

The measured raw-size reduction is approximately:

58–62%

However, the nested workload also demonstrates that a smaller representation does not automatically mean a faster serializer.

Brotli

As compression becomes stronger, the absolute difference between JSON and TOON compressed sizes becomes smaller.

At the same time, compression CPU time becomes increasingly important.

At high Brotli levels, TOON's smaller input can reduce compressor work sufficiently to overcome its serialization penalty.

Nested workloads

Nested data exposes the cost of the current TOON C++ encoder more strongly than flat data.

The nested encoder performs additional structural and memory-management work, producing higher serialization times than JSON in the measured implementation.

Nevertheless, the resulting representation can still produce lower end-to-end latency.

Cache

The cache experiments show that TOON's compact representation can become especially valuable when serialization and compression are removed from the hot path.

For example, the canonical flat cache experiment reports approximately:

JSON cache miss latency: 344.65 ms
TOON cache miss latency:  66.003 ms

and approximately:

80.85% improvement

The reported warm-cache mean is:

JSON: 302.76 ms
TOON:  33.65 ms

with approximately:

88.89% improvement

18. Interpretation

The experiment does not establish that TOON is universally faster than JSON.

Instead, it demonstrates that serialization-format performance depends on the surrounding serving pipeline.

A useful conceptual model is:

Format
   ↓
Serialization cost
   ↓
Compressed representation
   ↓
Compression cost
   ↓
Transport cost
   ↓
Caching
   ↓
End-to-end latency

A format that wins on raw bytes can lose on serialization.

A format that loses on serialization can still win end-to-end if the resulting reduction in bytes sufficiently reduces compression or transport cost.

19. Repository Structure

The principal project structure is:

toon-benchmark/
│
├── deploy/
│   │
│   ├── api/
│   │   ├── data/
│   │   │   ├── dataset_flat.json
│   │   │   ├── dataset_flat.toon
│   │   │   ├── dataset_nested.json
│   │   │   └── dataset_nested.toon
│   │   │
│   │   ├── toon_cpp/
│   │   │   ├── build/
│   │   │   ├── toon_cpp.cpp
│   │   │   ├── setup.py
│   │   │   ├── pyproject.toml
│   │   │   └── toon_cpp*.pyd
│   │   │
│   │   ├── build_data.py
│   │   ├── main.py
│   │   └── requirements.txt
│   │
│   └── frontend/
│       ├── static/
│       └── toon_cpp/
│
├── README.md
├── .gitignore
└── render.yaml

20. Benchmark Dashboard

The frontend benchmark dashboard allows researchers to configure:

case type

flat/nested structure

record count

compression

Brotli level

database source

warm-up count

repetitions

trials

random seed

Results are presented with JSON and TOON values alongside:

absolute difference

percentage improvement

The dashboard also provides JSON and CSV result downloads.

21. Result Artifacts

The experiment uses a consistent naming convention for result screenshots.

Examples:

plain_flat_n100000_none_native.png
plain_flat_n100000_none_cross.png

plain_flat_n100000_brotli5_native.png
plain_flat_n100000_brotli5_cross.png

plain_flat_n100000_brotli9_native.png
plain_flat_n100000_brotli9_cross.png

plain_flat_n100000_brotli11_native.png
plain_flat_n100000_brotli11_cross.png

plain_nested_n100000_none_native.png
plain_nested_n100000_none_cross.png

plain_nested_n100000_brotli5_native.png
plain_nested_n100000_brotli5_cross.png

plain_nested_n100000_brotli9_native.png
plain_nested_n100000_brotli9_cross.png

plain_nested_n100000_brotli11_native.png
plain_nested_n100000_brotli11_cross.png

Cache figures use:

cache_flat_n100000_canonical.png
cache_flat_n100000_native.png
cache_nested_n100000_canonical.png
cache_nested_n100000_native.png

22. Reproducing the Project

Clone the repository:

git clone https://github.com/debugaditya/toon-benchmark.git
cd toon-benchmark

The API implementation is under:

deploy/api/

The datasets are under:

deploy/api/data/

The native TOON implementation is under:

deploy/api/toon_cpp/

The main API entry point is:

deploy/api/main.py

Dataset generation is:

deploy/api/build_data.py

Because the TOON serializer is a compiled Python extension, a fresh environment may require rebuilding the extension for the local Python version and operating system.

23. Research Paper

The accompanying research paper is:

TOON vs JSON: An Experimental Evaluation of Serialization, Compression, and End-to-End HTTP Performance

The paper documents:

motivation

research questions

hypotheses

system architecture

dataset construction

serializer implementation

experimental methodology

Native/Cross methodology

compression experiments

cache experiments

complete result matrix

discussion

engineering implications

limitations

future work

24. Limitations

The results should be interpreted within the scope of this experimental setup.

Important limitations include:

The datasets are controlled benchmark workloads.

Each final configuration uses one trial with 100 measured repetitions.

The TOON serializer is optimized for the evaluated data structures.

Results depend on the deployment infrastructure and network conditions.

The experiment does not establish a theoretical maximum for TOON performance.

Different schemas, workloads, hardware, compression algorithms, and clients may produce different results.

Additional independent trials would be required for stronger statistical inference.

The nested serializer overhead also motivates future optimization of allocation, traversal, string handling, and buffer management.

25. Future Work

Future work can investigate:

further C++ TOON encoder optimization

memory allocation and buffer management

deeper nested structures

larger record counts

different data distributions

additional compression algorithms

gzip and zstd

concurrent clients

throughput

CPU utilization

memory utilization

multiple independent trials

confidence intervals

statistical significance

additional caching strategies

real production API workloads

The same benchmark matrix can be rerun after serializer optimization to isolate how much of the observed performance difference is caused by the representation itself versus the current encoder implementation.

26. Repository and Contact

Repository:

https://github.com/debugaditya/toon-benchmark

Author:

Aditya Narayan Barmola

Email:

adibarmola@gmail.com

Institution:

Netaji Subhas University of Technology (NSUT)
New Delhi, India

27. Summary

The central conclusion of this experiment is that serialization formats should be evaluated as part of the complete serving pipeline rather than by payload size alone.

TOON substantially reduces the raw representation size for the evaluated datasets. However, the current C++ encoder introduces additional serialization cost, especially for nested structures.

The end-to-end outcome therefore depends on the interaction between:

representation
+ serialization
+ compression
+ caching
+ transport

Without compression, TOON's smaller representation can produce a substantial HTTP advantage.

Under moderate compression, the advantage becomes more dependent on the balance between serializer and compressor costs.

At high compression levels, TOON's smaller input can reduce compression work enough to produce large end-to-end improvements.

The cache experiments further show that when response-generation work is amortized, the compact representation can become even more directly beneficial.

The repository provides the implementation and experimental artifacts needed to inspect, reproduce, extend, and challenge these findings.

import json
import os
import random
import string
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR / "toon_cpp"))

import toon_cpp

CITIES = [
    "Delhi", "Mumbai", "Chennai", "Pune", "Jaipur",
    "Kolkata", "Nagpur", "Indore", "Bhopal", "Surat"
]
RECORD_COUNT = 100000
SEED = 42

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

random.seed(SEED)
flat_data = []
for i in range(1, RECORD_COUNT + 1):
    flat_data.append({
        "id": i,
        "name": "".join(random.choices(string.ascii_uppercase, k=6)),
        "age": random.randint(18, 65),
        "city": random.choice(CITIES)
    })

random.seed(SEED)
nested_data = []
for i in range(1, RECORD_COUNT + 1):
    record = {
        "id": i,
        "name": "".join(random.choices(string.ascii_uppercase, k=6)),
        "address": {
            "city": random.choice(CITIES),
            "zip": random.randint(100000, 999999)
        },
        "tags": random.sample(["vip", "new", "flagged", "trial"], k=2) if (i % 3 == 0 or i == 1) else None
    }
    nested_data.append(record)

flat_json_path = DATA_DIR / "dataset_flat.json"
with open(flat_json_path, "w") as f:
    json.dump(flat_data, f)

nested_json_path = DATA_DIR / "dataset_nested.json"
with open(nested_json_path, "w") as f:
    json.dump(nested_data, f)

flat_toon_path = DATA_DIR / "dataset_flat.toon"
flat_toon_content = toon_cpp.encode_flat(flat_data)
with open(flat_toon_path, "w") as f:
    f.write(flat_toon_content)

nested_toon_path = DATA_DIR / "dataset_nested.toon"
nested_toon_content = toon_cpp.encode_nested(nested_data)
with open(nested_toon_path, "w") as f:
    f.write(nested_toon_content)

for path in [flat_json_path, nested_json_path, flat_toon_path, nested_toon_path]:
    if not path.exists():
        raise FileNotFoundError(f"Missing generated file: {path}")

with open(flat_json_path, "r") as f:
    if len(json.load(f)) != RECORD_COUNT:
        raise ValueError("Flat JSON record count mismatch")

with open(nested_json_path, "r") as f:
    if len(json.load(f)) != RECORD_COUNT:
        raise ValueError("Nested JSON record count mismatch")

with open(flat_toon_path, "r") as f:
    flat_header = f.readline().strip()
    if not flat_header.startswith(f"[{RECORD_COUNT}]{{"):
        raise ValueError(f"Invalid flat TOON header: {flat_header}")

with open(nested_toon_path, "r") as f:
    nested_header = f.readline().strip()
    if not nested_header.startswith(f"[{RECORD_COUNT}]{{id,name,address{{city,zip}},tags}}:"):
        raise ValueError(f"Invalid nested TOON header: {nested_header}")

print("VALIDATION SUCCESSFUL")
print("-" * 30)
for path in [flat_json_path, nested_json_path, flat_toon_path, nested_toon_path]:
    print(f"{path.name} size: {path.stat().st_size} bytes")

print("\n--- dataset_flat.toon (first 5 lines) ---")
with open(flat_toon_path, "r") as f:
    for _ in range(5):
        print(f.readline().rstrip('\n'))

print("\n--- dataset_nested.toon (first 5 lines) ---")
with open(nested_toon_path, "r") as f:
    for _ in range(5):
        print(f.readline().rstrip('\n'))

print(f"\nLoaded toon_cpp from: {toon_cpp.__file__}")
print(f"Available toon_cpp attributes: {dir(toon_cpp)}")
"""
split_dataset.py
================
Splits dataset_final.jsonl into:
  - train.jsonl   (80%)
  - val.jsonl     (20%)

Run from ~/pfe-project/agent/dataset/:
  python3 split_dataset.py
"""

import json
import random

INPUT  = "dataset_final.jsonl"
TRAIN  = "train.jsonl"
VAL    = "val.jsonl"
SEED   = 42

# Load
with open(INPUT) as f:
    data = [json.loads(line) for line in f if line.strip()]

# Shuffle
random.seed(SEED)
random.shuffle(data)

# Split 80/20
split = int(len(data) * 0.8)
train = data[:split]
val   = data[split:]

# Write
with open(TRAIN, "w") as f:
    for item in train:
        f.write(json.dumps(item) + "\n")

with open(VAL, "w") as f:
    for item in val:
        f.write(json.dumps(item) + "\n")

print(f"✅ Total   : {len(data)}")
print(f"✅ Train   : {len(train)} → {TRAIN}")
print(f"✅ Val     : {len(val)}   → {VAL}")

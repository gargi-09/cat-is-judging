import os
import json
import pandas as pd
from pathlib import Path

# Set base path
BASE_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

# 1. Inspect Excel telemetry columns & sample
excel_path = list(BASE_DIR.glob("*.xlsx"))[0]
df = pd.read_excel(excel_path, nrows=5)
print("=== AIS EXCEL SCHEMA ===")
print("Columns:", df.columns.tolist())
print("\nFirst row sample:")
print(df.iloc[0].to_dict())

# 2. Inspect a sample JSON file
json_dir = BASE_DIR / "json"
json_files = list(json_dir.glob("*.json"))
if json_files:
    print("\n=== SAMPLE JSON ANNOTATION ===")
    print("File:", json_files[0].name)
    with open(json_files[0], "r", encoding="utf-8") as f:
        sample_json = json.load(f)
    print(json.dumps(sample_json, indent=2, ensure_ascii=False)[:600] + "...")

# 3. Check PNG images count
png_dir = BASE_DIR / "png"
png_files = list(png_dir.glob("*.png"))
print(f"\nTotal image tiles found: {len(png_files)}")
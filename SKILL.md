---
name: chain-handle
description: Inspect trip-chain CSV tables, validate coordinate and timestamp fields, collapse stationary samples, generate audited WGS 84 file geodatabases with ArcPy, and provide ArcGIS Pro kind-field symbology code. Use for travel-chain or trajectory table recognition and point-feature conversion; do not use for ordinary tabular summaries or non-trajectory GIS conversion.
---

# Chain Handle

Process trip-chain tables only after inspecting their schema and data quality. Keep every input read-only.

## Workflow

1. Resolve the requested CSV file or directory and an explicit output directory. Use absolute paths.
2. Inspect the headers before creating output. Longitude, latitude, and a timestamp precise to at least the minute are required. A combined datetime column or separate date and time columns is acceptable.
3. If a required element is absent, stop for that CSV and report in Chinese:
   - all discovered headers;
   - the missing semantic fields;
   - any ambiguous matches;
   - supported aliases and how to provide `--field-map`.
4. Treat source speed and distance columns as reference attributes only. Never assume they are correct and never use them to populate computed values.
5. Run `scripts/process_trip_chain.py` with `--input` and `--output-dir`. It supervises one isolated ArcPy worker per CSV and requires an atomic completion receipt. Add `--overwrite` only when the user explicitly authorizes replacement of the resolved target GDB.
6. Report time precision, source/valid/rejected row counts, candidate/output/skipped trajectory counts, absolute GDB paths, and any ArcPy failure. Do not claim success unless the GDB exists and ArcPy inspection passes.
7. After at least one GDB is independently verified, finish the user-facing response with a section titled `ArcGIS Pro kind symbology code` (translate the heading to the user's language). Read `scripts/symbolize_kind_current_map.py` and reproduce its complete current contents in a Python code fence so the user can copy it directly into the ArcGIS Pro Python window. State that it affects point layers already loaded in the active map, selects layers by the `kind` field rather than by filename, and skips unrelated layers without stopping later layers. Do not run this script automatically: `ArcGISProject("CURRENT")` must execute inside the user's open ArcGIS Pro session, and changing the current map requires the user's deliberate paste/run action.

## Processing invariants

- Group by trajectory identifier plus local calendar date. When no trajectory identifier exists, group by source file plus date.
- Sort by parsed time and original row order.
- Collapse only consecutive points whose numeric longitude and latitude are exactly equal.
- Store the collapsed interval as `stop_time` in seconds.
- Use WGS 84 geodesic distance in meters for `real_distance`.
- Calculate `real_speed` as distance divided by the interval from the current stop end to the next point start. Leave it null when the interval is non-positive.
- Assign exactly one `start` and one `end` to every emitted trajectory; interior collapsed stops are `stop`, and other interior points are `way`.
- Skip post-collapse single-point trajectories and explain them in `processing_summary`.
- Generate file geodatabases only. Do not create Shapefiles, GeoPackages, or trajectory lines.

## Commands

Use ordinary Python only for inspection and unit tests. GDB creation requires the ArcGIS Pro Python environment that can import ArcPy.

Prefer Esri's `propy.bat`, because it activates the ArcGIS Pro environment selected in Package Manager:

```powershell
& 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat' `
  scripts\process_trip_chain.py `
  --input 'C:\path\tracks.csv' `
  --output-dir 'C:\path\results'
```

If ArcPy import or process cleanup fails, read [references/arcpy-runtime.md](references/arcpy-runtime.md) and run `scripts/diagnose_arcpy.py` through the same `propy.bat`. Preserve the native exit code. Never substitute a mock, a folder named `.gdb`, or a silent Shapefile fallback.

## Output contract

Each CSV produces one `<source>_trip_chains.gdb`. Every valid trajectory becomes one EPSG:4326 point feature class containing the original safe attributes plus `real_distance`, `real_speed`, `kind`, and `stop_time`. The GDB also contains `processing_summary` and `run_metadata` tables.

The post-processing symbology handoff is `scripts/symbolize_kind_current_map.py`. It assigns red `start`, green `end`, orange `stop`, and gray `way`/legacy `normal` symbols with a unique-value renderer. It changes display only, not feature attributes or GDB contents.

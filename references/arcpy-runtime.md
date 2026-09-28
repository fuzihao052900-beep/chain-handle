# ArcPy Native runtime recovery

Read this reference when ArcPy cannot import, exits abnormally, or reports a possible incomplete output.

## Supported launch path

Prefer Esri's `propy.bat`, which activates the ArcGIS Pro active conda environment before starting Python:

```powershell
& 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat' `
  scripts\process_trip_chain.py `
  --input 'C:\path\tracks.csv' `
  --output-dir 'C:\path\results'
```

For a custom installation, replace the ArcGIS Pro root. Do not assume that the system `python` contains ArcPy.

Esri references:

- [Python in ArcGIS Pro](https://pro.arcgis.com/en/pro-app/latest/arcpy/get-started/installing-python-for-arcgis-pro.htm)
- [Run stand-alone scripts](https://pro.arcgis.com/en/pro-app/latest/arcpy/get-started/using-conda-with-arcgis-pro.htm)

## Built-in containment

`process_trip_chain.py` is a supervisor. Each CSV uses two short-lived ArcPy workers:

1. The writer creates the GDB, closes all cursor contexts, atomically writes a UTF-8 write receipt, and exits without running fragile ArcPy interpreter-shutdown hooks.
2. A fresh verifier independently checks that the GDB exists, the feature-class count matches, and both audit tables exist. It then writes a separate verification receipt and exits the same way.

The supervisor accepts success only when both durable receipts exist. Console text alone is never enough. A crash before either receipt is a failure and reports `possible_incomplete_output`; do not delete or overwrite that path without the user's authorization. A nonzero writer exit after both receipts is reported as `partial` with an environment warning, while preserving the independently verified output.

## Diagnostic command

Run this with the same `propy.bat` that will run the conversion:

```powershell
& 'C:\Program Files\ArcGIS\Pro\bin\Python\Scripts\propy.bat' `
  scripts\diagnose_arcpy.py
```

Interpretation:

- `healthy`: ArcPy imported and the probe exited normally.
- `healthy_with_cleanup_crash`: ArcPy completed the probe and committed a receipt, but the host crashed during cleanup. The supervised worker contains this class of failure.
- `unavailable`: ArcPy never committed a receipt. Fix the environment before processing data.

## If ArcPy remains unavailable

1. Confirm ArcGIS Pro licensing for stand-alone Python: automatic sign-in, ArcGIS Pro currently open, offline authorization, or an available Concurrent Use license. Esri documents these requirements in “Python in ArcGIS Pro.”
2. In ArcGIS Pro Package Manager, confirm the active environment. Use `propy.bat`; do not hardcode a stale cloned environment.
3. Repair or update ArcGIS Pro if the active environment cannot import ArcPy.
4. Review the diagnostic JSON for System32 crypto DLL observations. Do not rename or delete DLLs automatically. Esri documents one class of import crash caused by an unexpected OpenSSL DLL loaded from System32: [Why does Python crash when importing arcpy from the Command Prompt?](https://support.esri.com/en-us/knowledge-base/why-is-python-crashing-when-i-import-arcpy-in-command-p-000022475). Escalate DLL changes to a qualified administrator because other software may depend on them.
5. Preserve the exit code, stderr, diagnostic JSON, ArcGIS Pro version, and Windows Event Viewer/crash-dump evidence when contacting Esri Support.

Never work around ArcPy failure by creating a normal directory with a `.gdb` suffix, by replacing geodesic calculations with unverified approximations, or by silently switching to Shapefile.

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from arcpy_gdb import verify_gdb_path


def main() -> None:
    parser = argparse.ArgumentParser(description="内部 ArcPy GDB 独立验证工作进程")
    parser.add_argument("--gdb", required=True, type=Path)
    parser.add_argument("--expected-groups", required=True, type=int)
    parser.add_argument("--receipt-file", required=True, type=Path)
    options = parser.parse_args()
    try:
        verification = verify_gdb_path(options.gdb, options.expected_groups)
        payload = {"status": "verified", **verification}
        receipt = options.receipt_file.resolve()
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, receipt)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        os._exit(0)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        os._exit(1)


if __name__ == "__main__":
    main()

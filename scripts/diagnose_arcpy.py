from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def classify_probe(returncode: int, receipt: dict[str, object] | None) -> str:
    if receipt and receipt.get("import_ok"):
        return "healthy" if returncode == 0 else "healthy_with_cleanup_crash"
    return "unavailable"


def _system_dll_observations() -> list[str]:
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    names = (
        "libcrypto.dll",
        "libcrypto-1_1-x64.dll",
        "libssl-1_1-x64.dll",
    )
    return [str(system32 / name) for name in names if (system32 / name).is_file()]


def _worker(receipt_path: Path) -> None:
    try:
        import arcpy

        payload = {
            "import_ok": True,
            "python": sys.executable,
            "python_version": sys.version,
            "arcgis_product": arcpy.GetInstallInfo().get("ProductName"),
            "arcgis_version": arcpy.GetInstallInfo().get("Version"),
            "license": arcpy.ProductInfo(),
            "wgs84_factory_code": arcpy.SpatialReference(4326).factoryCode,
        }
        arcpy.management.ClearWorkspaceCache()
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, receipt_path)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        os._exit(0)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        os._exit(1)


def run_probe() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="chain_handle_arcpy_probe_") as directory:
        receipt_path = Path(directory) / "probe.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            "--receipt-file",
            str(receipt_path),
        ]
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        receipt = None
        if receipt_path.is_file():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                receipt = None
        return {
            "status": classify_probe(completed.returncode, receipt),
            "worker_exit_code": completed.returncode,
            "receipt": receipt,
            "stderr": completed.stderr.strip(),
            "launcher_python": sys.executable,
            "system32_crypto_dll_observations": _system_dll_observations(),
            "note": (
                "DLL 列表仅用于交给管理员/Esri 支持诊断；本脚本不会修改、重命名或删除系统 DLL。"
            ),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断 chain_handle 的 ArcPy Native 运行环境")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--receipt-file", type=Path, help=argparse.SUPPRESS)
    options = parser.parse_args()
    if options.worker:
        if options.receipt_file is None:
            return 2
        _worker(options.receipt_file.resolve())
    report = run_probe()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"healthy", "healthy_with_cleanup_crash"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

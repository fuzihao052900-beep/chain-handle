from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Mapping, Sequence

from chain_core import InputValidationError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="识别出行链 CSV 并生成 WGS 84 文件地理数据库。"
    )
    parser.add_argument("--input", required=True, type=Path, help="CSV 文件或包含 CSV 的目录")
    parser.add_argument("--output-dir", required=True, type=Path, help="输出目录")
    parser.add_argument(
        "--field-map",
        default=None,
        help="覆盖自动字段识别的 JSON 字符串或 JSON 文件路径",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="显式允许替换同名输出 GDB"
    )
    parser.add_argument("--worker-one", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--receipt-file", type=Path, help=argparse.SUPPRESS)
    return parser


def enumerate_csvs(input_path: Path) -> list[Path]:
    path = input_path.resolve()
    if path.is_file():
        if path.suffix.casefold() != ".csv":
            raise ValueError(f"输入文件不是 CSV: {path}")
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(path)
    files = [child for child in path.iterdir() if child.is_file() and child.suffix.casefold() == ".csv"]
    return sorted(files, key=lambda item: item.name.casefold())


def output_gdb_path(input_csv: Path, output_dir: Path) -> Path:
    stem = re.sub(r"[^\w]+", "_", input_csv.stem, flags=re.UNICODE).strip("_") or "trip_chain"
    return output_dir.resolve() / f"{stem}_trip_chains.gdb"


def preflight_batch_targets(inputs: Sequence[Path], output_dir: Path) -> None:
    targets: dict[str, list[Path]] = {}
    for source in inputs:
        target = output_gdb_path(source, output_dir)
        targets.setdefault(str(target).casefold(), []).append(source)
    collisions = [sources for sources in targets.values() if len(sources) > 1]
    if collisions:
        detail = [[str(source) for source in sources] for sources in collisions]
        raise ValueError(f"批量输入存在输出名冲突，未开始处理: {detail}")


def parse_field_map(value: str | None) -> Mapping[str, str] | None:
    if value is None:
        return None
    candidate = Path(value)
    if candidate.is_file():
        payload = json.loads(candidate.read_text(encoding="utf-8-sig"))
    else:
        payload = json.loads(value)
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in payload.items()
    ):
        raise ValueError("--field-map 必须是字符串到字符串的 JSON 对象")
    return payload


def _result_dict(result) -> dict[str, object]:
    report = asdict(result)
    report["source_csv"] = str(result.source_csv)
    report["output_gdb"] = str(result.output_gdb)
    report["rejected_rows"] = dict(result.rejected_rows)
    report["status"] = "partial" if result.failures else "success"
    return report


def run_supervised(
    source: Path,
    output_dir: Path,
    overrides: Mapping[str, str] | None,
    overwrite: bool,
) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="chain_handle_receipt_") as directory:
        receipt = Path(directory) / "worker-result.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker-one",
            "--receipt-file",
            str(receipt),
            "--input",
            str(source.resolve()),
            "--output-dir",
            str(output_dir.resolve()),
        ]
        if overrides:
            command.extend(["--field-map", json.dumps(dict(overrides), ensure_ascii=False)])
        if overwrite:
            command.append("--overwrite")
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        parsed: dict[str, object] | None = None
        if receipt.is_file():
            try:
                parsed = json.loads(receipt.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                parsed = None
        if parsed and parsed.get("status") in {"success", "partial"}:
            verify_receipt = Path(directory) / "verification-result.json"
            verify_command = [
                sys.executable,
                str(Path(__file__).with_name("verify_arcpy_output.py")),
                "--gdb",
                str(parsed["output_gdb"]),
                "--expected-groups",
                str(parsed["output_groups"]),
                "--receipt-file",
                str(verify_receipt),
            ]
            verification_process = subprocess.run(
                verify_command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            verification = None
            if verify_receipt.is_file():
                try:
                    verification = json.loads(verify_receipt.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    verification = None
            if not verification or verification.get("status") != "verified":
                detail = (
                    verification_process.stderr
                    or verification_process.stdout
                    or "no diagnostic output"
                ).strip()
                return {
                    **parsed,
                    "status": "failed",
                    "error": (
                        "GDB 写入工作进程已提交回执，但独立验证工作进程未提交有效回执"
                        f"（code={verification_process.returncode}）: {detail}"
                    ),
                    "possible_incomplete_output": str(parsed["output_gdb"]),
                }
            parsed["verification"] = verification
            if completed.returncode != 0:
                parsed["status"] = "partial"
                parsed["native_exit_code"] = completed.returncode
                parsed["warning"] = "GDB 已由独立进程验证，但写入工作进程在提交回执后异常退出"
            return parsed
        detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
        return {
            "source_csv": str(source.resolve()),
            "status": "failed",
            "error": (
                f"ArcPy 工作进程未提交有效回执（code={completed.returncode}）: {detail}"
            ),
            "possible_incomplete_output": str(output_gdb_path(source, output_dir)),
        }


def _worker_main(options, overrides: Mapping[str, str] | None) -> int:
    from arcpy_gdb import write_input_gdb

    try:
        if options.receipt_file is None:
            raise ValueError("内部 ArcPy 工作进程缺少 --receipt-file")
        result = write_input_gdb(options.input, options.output_dir, overrides, options.overwrite)
        report = _result_dict(result)
        receipt = options.receipt_file.resolve()
        receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = receipt.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, default=str), encoding="utf-8"
        )
        os.replace(temporary, receipt)
        print(json.dumps(report, ensure_ascii=False, default=str), flush=True)
        os._exit(0)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        os._exit(1)


def main(argv: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(argv)
    try:
        inputs = enumerate_csvs(options.input)
        if not inputs:
            raise FileNotFoundError(f"目录中没有 CSV: {options.input.resolve()}")
        overrides = parse_field_map(options.field_map)
        preflight_batch_targets(inputs, options.output_dir)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2

    if options.worker_one:
        if len(inputs) != 1:
            print("worker requires exactly one CSV", file=sys.stderr)
            return 2
        options.input = inputs[0]
        return _worker_main(options, overrides)

    from arcpy_gdb import (
        ArcPyEnvironmentError,
        GdbWriteError,
        OutputExistsError,
        OutputSafetyError,
        write_input_gdb,
    )

    reports: list[dict[str, object]] = []
    succeeded = 0
    for source in inputs:
        try:
            report = run_supervised(source, options.output_dir, overrides, options.overwrite)
            reports.append(report)
            if report.get("status") in {"success", "partial"}:
                succeeded += 1
        except (
            InputValidationError,
            ArcPyEnvironmentError,
            OutputExistsError,
            OutputSafetyError,
            GdbWriteError,
        ) as exc:
            reports.append(
                {"source_csv": str(source.resolve()), "status": "failed", "error": str(exc)}
            )
        except Exception as exc:
            reports.append(
                {
                    "source_csv": str(source.resolve()),
                    "status": "failed",
                    "error": f"未预期错误: {type(exc).__name__}: {exc}",
                }
            )
    payload = {
        "requested": len(inputs),
        "succeeded": succeeded,
        "failed": len(inputs) - succeeded,
        "results": reports,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0 if succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())

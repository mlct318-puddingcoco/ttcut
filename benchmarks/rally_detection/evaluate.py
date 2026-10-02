#!/usr/bin/env python3
"""Command-line entry point for the rally-detection regression harness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from benchmarks.rally_detection.evaluator import evaluate_suite
else:
    from .evaluator import evaluate_suite


HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "data" / "manifest.json"
DEFAULT_BASELINE = HERE / "data" / "frozen_current_koko.json"
DEFAULT_GATES = HERE / "release_gates.json"


def _load(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _write(path: Optional[Path], payload: Mapping[str, Any]) -> None:
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(encoded)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(encoded, encoding="utf-8")


def _subset_differences(actual: Any, expected: Any, path: str = "result",
                        tolerance: float = 1e-3) -> list[str]:
    differences = []
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return [f"{path}: expected object"]
        for key, value in expected.items():
            if key not in actual:
                differences.append(f"{path}.{key}: missing")
            else:
                differences.extend(_subset_differences(
                    actual[key], value, f"{path}.{key}", tolerance))
    elif isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            differences.append(f"{path}: list length mismatch")
        else:
            for index, value in enumerate(expected):
                differences.extend(_subset_differences(
                    actual[index], value, f"{path}[{index}]", tolerance))
    elif isinstance(expected, (int, float)) and not isinstance(expected, bool):
        if not isinstance(actual, (int, float)) or abs(float(actual) - float(expected)) > tolerance:
            differences.append(f"{path}: expected {expected!r}, got {actual!r}")
    elif actual != expected:
        differences.append(f"{path}: expected {expected!r}, got {actual!r}")
    return differences


def _single_candidate(raw: Mapping[str, Any], benchmark_id: str,
                      runtime_seconds: Optional[float], coverage: str) -> Dict[str, Any]:
    predictions = raw.get("predictions", raw.get("candidates"))
    if predictions is None:
        raise ValueError("single-benchmark input must contain predictions or candidates")
    return {
        "schema_version": 1,
        "detector": raw.get("detector", "candidate"),
        "benchmarks": {
            benchmark_id: {
                "predictions": predictions,
                "runtime_seconds": runtime_seconds,
                "coverage": coverage,
            }
        },
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path,
                        help="normalized suite JSON or a single raw detector output")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--gates", type=Path, default=DEFAULT_GATES)
    parser.add_argument("--output", type=Path,
                        help="write deterministic JSON here instead of stdout")
    parser.add_argument("--benchmark-id", choices=("benchmark_01", "benchmark_02", "benchmark_03"),
                        help="evaluate one raw detector output")
    parser.add_argument("--runtime-seconds", type=float,
                        help="runtime for a single raw detector output")
    parser.add_argument("--coverage", choices=("full_source", "official_window_only"),
                        default="full_source")
    parser.add_argument("--verify-frozen", action="store_true",
                        help="compare output with the expected subset in --baseline")
    args = parser.parse_args(argv)

    manifest = _load(args.manifest)
    candidate = _load(args.candidate)
    baseline = _load(args.baseline)
    gates = _load(args.gates)
    if args.benchmark_id:
        candidate = _single_candidate(candidate, args.benchmark_id,
                                      args.runtime_seconds, args.coverage)
        wanted = args.benchmark_id
        manifest = dict(manifest)
        manifest["benchmarks"] = [entry for entry in manifest["benchmarks"]
                                  if entry["benchmark_id"] == wanted]
        baseline = None
        gates = None

    result = evaluate_suite(manifest, candidate, baseline, gates)
    _write(args.output, result)

    if args.verify_frozen:
        if baseline is None:
            parser.error("--verify-frozen requires a full three-benchmark candidate")
        differences = _subset_differences(result, baseline["expected_result"])
        if differences:
            sys.stderr.write("Frozen baseline verification failed:\n")
            sys.stderr.write("\n".join(f"- {difference}" for difference in differences) + "\n")
            return 1
        sys.stderr.write("Frozen baseline verification passed.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

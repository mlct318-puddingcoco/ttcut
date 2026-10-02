# Rally detection regression harness

This directory freezes the three manually confirmed Koko benchmarks (142 GT
rallies) and evaluates detector candidates without source videos, private
paths, Huji, or third-party Python packages. Huji remains a historical
comparator only and is not read or imported by this harness.

## Run the frozen baseline

From the repository root:

```bash
python3 -m benchmarks.rally_detection.evaluate \
  --candidate benchmarks/rally_detection/data/current_koko_predictions.json \
  --output /tmp/koko-rally-baseline.json \
  --verify-frozen
```

Run the harness tests:

```bash
python3 -m unittest discover -s benchmarks/rally_detection/tests -v
```

Confidence-only revisions can be audited against raw detector JSON without
changing the frozen matching methodology:

```bash
python3 -m benchmarks.rally_detection.analyze_confidence \
  --benchmark benchmark_01 /tmp/benchmark-01.json \
  --benchmark benchmark_02 /tmp/benchmark-02.json \
  --benchmark benchmark_03 /tmp/benchmark-03.json \
  --output /tmp/confidence-analysis.json
```

The report labels official-window candidates with the same one-to-one matcher
and includes old/new score distributions, ROC-AUC, average precision, Brier,
calibration buckets, threshold sweeps, bottom-quartile enrichment, and a
leave-one-benchmark-out formula-family robustness check. Confidence is review
metadata only; the analysis never removes candidates.

## Evaluate a new three-match candidate

Provide one compact JSON file. `coverage` is `full_source` when out-of-window
predictions are included, or `official_window_only` when they are not. The
nuisance gate is not evaluated unless benchmarks 02 and 03 both use
`full_source` coverage. Runtime is optional, but omitting it leaves the runtime
gate unevaluated.

```json
{
  "schema_version": 1,
  "detector": "my candidate",
  "benchmarks": {
    "benchmark_01": {
      "coverage": "full_source",
      "runtime_seconds": 180.0,
      "predictions": [{"start": 11.8, "end": 16.95, "confidence": 0.67}]
    },
    "benchmark_02": {
      "coverage": "full_source",
      "runtime_seconds": 185.0,
      "predictions": []
    },
    "benchmark_03": {
      "coverage": "full_source",
      "runtime_seconds": 120.0,
      "predictions": []
    }
  }
}
```

```bash
python3 -m benchmarks.rally_detection.evaluate \
  --candidate /path/to/candidate-suite.json \
  --output /tmp/candidate-results.json
```

For a raw Koko detector JSON containing a top-level `candidates` array, evaluate
one match like this:

```bash
python3 -m benchmarks.rally_detection.evaluate \
  --candidate /path/to/koko-candidates.json \
  --benchmark-id benchmark_03 \
  --runtime-seconds 120 \
  --coverage full_source \
  --output /tmp/benchmark-03-results.json
```

## Frozen methodology

- A primary edge exists when overlap is at least 0.5 seconds **or** at least
  25% of the shorter interval.
- Exact one-to-one bipartite matching maximizes TP count first, then total
  overlap.
- Precision uses only predictions overlapping the official evaluation window.
- Duration buckets are short `<=2s`, medium `>2s and <7s`, and long `>=7s`.
- Fragmentation and merges use degree `>=2` in the primary-overlap graph.
- A near miss has no primary edge and an edge-to-edge gap of at most 2 seconds.
- Boundary errors are absolute matched-pair errors, with linearly interpolated
  median and p90 values.
- Aggregate macro values average the three matches; micro values pool counts.
  The runtime factor is total supplied runtime divided by total source duration.

`data/manifest.json` contains only source basenames, timing metadata, and the
manual GT intervals. `data/current_koko_predictions.json` is the compact frozen
detector output. `data/frozen_current_koko.json` records the expected metrics
derived from the existing benchmark result artifacts. No videos, Huji data, or
machine-specific paths are stored here.

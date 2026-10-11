"""Offline, allowlisted analysis of browser comparison exports (schema 1/2).

No live endpoints, settings or hardware are accessed. Input filenames, dates,
response contents and unknown fields are never copied into the report.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import math
from pathlib import Path
import sys
from typing import Any

MAX_BYTES = 1_048_576
MAX_FILES = 8
MAX_POINTS = 600
MAX_METRIC = 1e12
MIN_SPAN_MS = 60_000
MIN_NEW_MEASUREMENTS = 30
METRICS = (
    "http_rtt_ms", "request_duration_ms", "dom_apply_ms",
    "measurement_age_upper_ms", "relay_age_ms", "upstream_rtt_ms",
    "refresh_interval_ms", "request_interval_ms", "measurement_update_interval_ms",
)
LABELS = {
    "device": {"unknown", "android", "iphone", "surface"},
    "adapter": {"unknown", "internal", "external"},
    "network": {"unknown", "mobile_router", "home_router", "local_area", "router", "hotspot", "bluetooth"},
    "motor": {"unknown", "stopped", "running"},
}
MODES = {"ble", "mock", "unknown"}
STATES = {"connected", "connecting", "surface_offline", "stale", "reconnecting", "stopped", "invalid", "unknown"}
STOP_REASONS = {"manual", "duration", "hidden", "labels_changed", "access_lost", "clock_changed", "unknown"}


class AnalysisError(ValueError):
    """A fixed, privacy-safe error code, never raw input or an OS exception."""


def _object(value: Any) -> dict:
    if not isinstance(value, dict):
        raise AnalysisError("object_required")
    return value


def _number(value: Any, *, nullable: bool = True, maximum: float = MAX_METRIC) -> float | None:
    if value is None and nullable:
        return None
    if type(value) not in (int, float) or not 0 <= value <= maximum or not math.isfinite(value):
        raise AnalysisError("invalid_numeric_value")
    return float(value)


def _integer(value: Any, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise AnalysisError("invalid_integer")
    return value


def _boolean(value: Any, *, nullable: bool = False) -> bool | None:
    if value is None and nullable:
        return None
    if type(value) is not bool:
        raise AnalysisError("invalid_boolean")
    return value


def _enum(value: Any, options: set[str], default: str = "unknown") -> str:
    value = default if value is None else value
    if not isinstance(value, str) or value not in options:
        raise AnalysisError("invalid_enum")
    return value


def _timestamp(value: Any, *, nullable: bool = False) -> datetime | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 40:
        raise AnalysisError("invalid_timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.utcoffset() is None:
            raise ValueError
        return result
    except (ValueError, OverflowError):
        raise AnalysisError("invalid_timestamp") from None


def _modes(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 3:
        raise AnalysisError("invalid_source_modes")
    modes = [_enum(item, MODES) for item in value]
    if len(set(modes)) != len(modes):
        raise AnalysisError("invalid_source_modes")
    return sorted(modes)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AnalysisError("duplicate_json_key")
        result[key] = value
    return result


def _bad_constant(_value: str) -> None:
    raise AnalysisError("non_finite_json_number")


def load_export(path: Path) -> dict:
    """Bound the read itself, including when a file grows after stat()."""
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise AnalysisError("input_too_large")
        return _object(json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_json_pairs,
                                  parse_constant=_bad_constant))
    except AnalysisError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise AnalysisError("input_unreadable_or_invalid_json") from None


def _stats(values: list[float]) -> dict:
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "p50": ordered[math.ceil(len(ordered) * .5) - 1] if ordered else None,
        "p95": ordered[math.ceil(len(ordered) * .95) - 1] if ordered else None,
        "max": ordered[-1] if ordered else None,
    }


def _metric_summary(rows: list[dict]) -> dict:
    return {name: _stats([row[name] for row in rows if row[name] is not None]) for name in METRICS}


def summarize_export(data: dict, number: int = 1) -> dict:
    """Validate known raw fields and recompute summaries; discard unknown data."""
    data = _object(data)
    schema = data.get("schema")
    if type(schema) is not int or schema not in (1, 2):
        raise AnalysisError("unsupported_phone_export_schema")
    raw_labels = _object(data.get("labels"))
    if raw_labels.get("source") != "manual":
        raise AnalysisError("unsupported_label_source")
    labels = {key: _enum(raw_labels.get(key), values) for key, values in LABELS.items()}
    if schema == 1:
        labels["device"] = "unknown"
    interval = _object(data.get("interval"))
    duration = _number(interval.get("duration_ms"))
    dropped = _integer(interval.get("dropped_observations", 0), 10_000_000)
    if "observation_limit" in interval:
        _integer(interval["observation_limit"], MAX_POINTS)
    target = _number(interval.get("target_duration_ms")) if schema == 2 else None
    stop_reason = _enum(interval.get("stop_reason"), STOP_REASONS) if schema == 2 else "unknown"
    source_rows = data.get("observations")
    if not isinstance(source_rows, list) or len(source_rows) > MAX_POINTS:
        raise AnalysisError("invalid_observation_count")
    rows = []
    seen_received = set()
    last_new_elapsed = None
    first_wall = None
    clock_inconsistent = False
    new_flag_mismatches = 0
    for original in source_rows:
        original = _object(original)
        row = {name: _number(original.get(name)) for name in METRICS}
        row["success"] = _boolean(original.get("success"))
        declared_new = _boolean(original.get("new_measurement"))
        wall = _timestamp(original.get("observed_at"))
        received = _timestamp(original.get("received_at"), nullable=True)
        first_wall = first_wall or wall
        row["elapsed_ms"] = (_number(original.get("elapsed_ms"), nullable=False) if schema == 2
                             else (wall - first_wall).total_seconds() * 1000)
        if rows and row["elapsed_ms"] < rows[-1]["elapsed_ms"]:
            if schema == 2:
                raise AnalysisError("non_monotonic_observation_time")
            clock_inconsistent = True
        if schema == 2 and duration is not None and row["elapsed_ms"] > duration + 1000:
            raise AnalysisError("observation_outside_duration")
        row["source_mode"] = _enum(original.get("source_mode"), MODES) if schema == 2 else "unknown"
        row["http_success"] = _boolean(original.get("http_success")) if schema == 2 else None
        row["json_success"] = _boolean(original.get("json_success")) if schema == 2 else None
        row["source_fresh"] = _boolean(original.get("source_fresh"), nullable=True) if schema == 2 else None
        row["source_state"] = _enum(original.get("source_state"), STATES) if schema == 2 else "unknown"
        row["poll_seconds"] = _number(original.get("poll_seconds"), maximum=10) if schema == 2 else None
        row["stale_after_seconds"] = _number(original.get("stale_after_seconds")) if schema == 2 else None
        if row["poll_seconds"] is not None and row["poll_seconds"] < 1:
            raise AnalysisError("invalid_poll_seconds")
        if row["stale_after_seconds"] is not None and row["stale_after_seconds"] <= 0:
            raise AnalysisError("invalid_stale_after_seconds")
        if schema == 2:
            if row["json_success"] and not row["http_success"]:
                raise AnalysisError("inconsistent_success_flags")
            if row["success"] and not (row["http_success"] and row["json_success"]
                                        and row["source_fresh"] is True and row["source_state"] == "connected"):
                raise AnalysisError("inconsistent_success_flags")
        if row["success"] and received is None:
            raise AnalysisError("missing_fresh_measurement_time")
        sample_id = (row["source_mode"], received)
        new = bool(row["success"] and sample_id not in seen_received)
        row["new_measurement"] = new
        new_flag_mismatches += int(declared_new != new)
        # A retained first interval may refer to a point outside this export.
        # Reconstruct intervals using only distinct successful samples in this file.
        row["measurement_update_interval_ms"] = None
        if new:
            seen_received.add(sample_id)
            if last_new_elapsed is not None and row["elapsed_ms"] >= last_new_elapsed:
                row["measurement_update_interval_ms"] = row["elapsed_ms"] - last_new_elapsed
            last_new_elapsed = row["elapsed_ms"]
        rows.append(row)

    source_modes = sorted({row["source_mode"] for row in rows})
    session_modes = source_modes
    if schema == 2:
        if _modes(data.get("source_modes")) != source_modes:
            raise AnalysisError("source_modes_mismatch")
        session_modes = _modes(data.get("session_source_modes"))
        if not set(source_modes).issubset(session_modes):
            raise AnalysisError("session_source_modes_mismatch")
    fresh_rows = [row for row in rows if row["success"]]
    failed_rows = [row for row in rows if not row["success"]]
    http_rows = [row for row in rows if row["http_success"] is True]
    telemetry_rows = [row for row in rows if row["json_success"] is True and row["source_mode"] in {"ble", "mock"}]
    fresh_modes = sorted({row["source_mode"] for row in fresh_rows})
    poll_values = sorted({row["poll_seconds"] for row in telemetry_rows if row["poll_seconds"] is not None})
    stale_values = sorted({row["stale_after_seconds"] for row in telemetry_rows if row["stale_after_seconds"] is not None})
    span = max(0, rows[-1]["elapsed_ms"] - rows[0]["elapsed_ms"]) if len(rows) > 1 else 0
    new_count = len(seen_received)
    evidence_reasons = []
    if span < MIN_SPAN_MS:
        evidence_reasons.append("observed_span_below_60_seconds")
    if new_count < MIN_NEW_MEASUREMENTS:
        evidence_reasons.append("fewer_than_30_distinct_fresh_measurements")
    control_reasons = list(evidence_reasons)
    if schema == 1:
        control_reasons.append("legacy_schema_missing_device_and_source")
    if any(value == "unknown" for value in labels.values()):
        control_reasons.append("unknown_manual_condition")
    if labels["device"] == "surface":
        control_reasons.append("surface_browser_not_physical_phone")
    if labels["network"] not in {"mobile_router", "home_router", "local_area"}:
        control_reasons.append("network_mode_not_specific")
    if fresh_modes != ["ble"] or "mock" in session_modes:
        control_reasons.append("source_not_consistently_real_ble")
    if any(row["source_mode"] == "unknown" and row["json_success"] is True for row in rows):
        control_reasons.append("decoded_response_source_unknown")
    if len(poll_values) != 1 or any(row["poll_seconds"] is None for row in telemetry_rows):
        control_reasons.append("poll_period_unknown_or_changed")
    if len(stale_values) != 1 or any(row["stale_after_seconds"] is None for row in telemetry_rows):
        control_reasons.append("freshness_threshold_unknown_or_changed")
    if dropped:
        control_reasons.append("retained_window_truncated")
    if clock_inconsistent or stop_reason == "clock_changed":
        control_reasons.append("clock_changed")
    if duration is None or duration < span:
        control_reasons.append("recording_duration_unknown_or_inconsistent")
    return {
        "id": f"recording_{number}", "input_schema": schema,
        "labels": {"source": "operator_declared", **labels},
        "source_modes": source_modes, "session_source_modes": session_modes,
        "fresh_source_modes": fresh_modes,
        "poll_seconds": poll_values[0] if len(poll_values) == 1 else None,
        "stale_after_seconds": stale_values[0] if len(stale_values) == 1 else None,
        "interval": {"duration_ms": duration, "retained_observed_span_ms": span,
                     "target_duration_ms": target, "dropped_observations": dropped, "stop_reason": stop_reason},
        "counts": {"observations": len(rows), "fresh_display_success": len(fresh_rows),
                   "not_fresh_display_success": len(failed_rows), "distinct_fresh_measurements": new_count,
                   "http_success": len(http_rows) if schema == 2 else None,
                   "http_failure": len(rows) - len(http_rows) if schema == 2 else None,
                   "http_success_without_fresh_display": sum(not row["success"] for row in http_rows) if schema == 2 else None,
                   "new_measurement_flag_mismatches": new_flag_mismatches},
        "metrics": {"fresh_display": _metric_summary(fresh_rows), "http_success": _metric_summary(http_rows),
                    "unsuccessful_display": _metric_summary(failed_rows), "all_attempts": _metric_summary(rows)},
        "quality": {"sufficient_observations": not evidence_reasons,
                    "eligible_for_condition_matched_comparison": not control_reasons,
                    "reasons": control_reasons},
    }


def compare_summaries(left: dict, right: dict, axis: str) -> dict:
    if axis not in {"network", "adapter", "device"}:
        raise AnalysisError("invalid_comparison_axis")
    changed = [key for key in LABELS if left["labels"][key] != right["labels"][key]]
    reasons = []
    if not left["quality"]["eligible_for_condition_matched_comparison"]:
        reasons.append("first_recording_not_eligible")
    if not right["quality"]["eligible_for_condition_matched_comparison"]:
        reasons.append("second_recording_not_eligible")
    if changed != [axis]:
        reasons.append("exactly_requested_factor_must_differ")
    if left["poll_seconds"] != right["poll_seconds"]:
        reasons.append("poll_period_differs")
    if left["stale_after_seconds"] != right["stale_after_seconds"]:
        reasons.append("freshness_threshold_differs")
    if left["interval"]["target_duration_ms"] != right["interval"]["target_duration_ms"]:
        reasons.append("target_duration_differs")
    a_span, b_span = (item["interval"]["retained_observed_span_ms"] for item in (left, right))
    if min(a_span, b_span) <= 0 or abs(a_span - b_span) > max(5000, max(a_span, b_span) * .1):
        reasons.append("observation_windows_not_comparable")
    differences = None
    if not reasons:
        differences = {}
        for name in METRICS:
            a, b = left["metrics"]["fresh_display"][name], right["metrics"]["fresh_display"][name]
            differences[name] = {key: b[key] - a[key] if a[key] is not None and b[key] is not None else None
                                 for key in ("p50", "p95", "max")}
    return {"first": left["id"], "second": right["id"], "axis": axis,
            "changed_manual_factors": changed,
            "status": "condition_matched_descriptive_comparison" if not reasons else "comparison_not_established",
            "reasons": reasons, "second_minus_first_ms": differences,
            "causal_improvement_established": False,
            "uncontrolled_factors": ["physical_position_and_orientation", "radio_interference", "phone_load_and_browser", "battery_state"]}


def analyze_exports(exports: list[dict], axis: str = "network") -> dict:
    if axis not in {"network", "adapter", "device"}:
        raise AnalysisError("invalid_comparison_axis")
    if not 1 <= len(exports) <= MAX_FILES:
        raise AnalysisError("input_file_count_must_be_1_to_8")
    summaries = [summarize_export(data, index + 1) for index, data in enumerate(exports)]
    return {"schema": "gtbms.phone-delivery-analysis.v1", "axis": axis,
            "percentile_method": "nearest_rank_recomputed_from_retained_raw_points",
            "thresholds": {"minimum_retained_span_ms": MIN_SPAN_MS, "minimum_distinct_fresh_measurements": MIN_NEW_MEASUREMENTS},
            "recordings": summaries,
            "comparisons": [compare_summaries(a, b, axis) for index, a in enumerate(summaries) for b in summaries[index + 1:]],
            "interpretation": ["Manual labels are operator declarations, not radio or route detection.",
                               "Fresh-display failure is not necessarily an HTTP failure.",
                               "HTTP RTT is request/JSON processing round trip, not one-way Wi-Fi latency.",
                               "DOM apply time does not measure completed screen paint.",
                               "Measurement age upper estimate is not a calibrated error bound or BMS physical sensing latency.",
                               "Repeated responses do not count as distinct fresh measurements.",
                               "A single condition-matched pair describes observations and does not establish causal improvement."]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--axis", choices=("network", "adapter", "device"), default="network")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if not 1 <= len(args.files) <= MAX_FILES:
            raise AnalysisError("input_file_count_must_be_1_to_8")
        exports = []
        for index, path in enumerate(args.files):
            try:
                exports.append(load_export(path))
            except AnalysisError as error:
                print(json.dumps({"ok": False, "error": str(error), "input_number": index + 1}), file=sys.stderr)
                return 2
        report = analyze_exports(exports, args.axis)
        # Exclusive creation is the final check: never overwrite an input/history.
        try:
            with args.output.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.write("\n")
        except FileExistsError:
            raise AnalysisError("output_already_exists") from None
        except OSError:
            raise AnalysisError("output_unwritable") from None
        print(json.dumps({"ok": True, "recordings": len(exports), "comparisons": len(report["comparisons"])}))
        return 0
    except AnalysisError as error:
        print(json.dumps({"ok": False, "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

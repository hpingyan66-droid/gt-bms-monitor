from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest

from gtbms.delivery_analysis import (
    AnalysisError, MAX_BYTES, MAX_FILES, MAX_POINTS, analyze_exports,
    load_export, main, summarize_export,
)


def exported(*, device="iphone", network="local_area", adapter="external", motor="stopped", count=61, step=2000):
    start = datetime(2026, 10, 11, tzinfo=timezone.utc)
    rows = []
    for i in range(count):
        rows.append({
            "observed_at": (start + timedelta(milliseconds=i * step)).isoformat(),
            "received_at": (start + timedelta(milliseconds=i * step)).isoformat(),
            "elapsed_ms": i * step, "success": True, "new_measurement": True,
            "http_success": True, "json_success": True, "source_mode": "ble",
            "source_fresh": True, "source_state": "connected", "poll_seconds": 2,
            "stale_after_seconds": 10, "http_rtt_ms": i + 1,
            "request_duration_ms": i + 2, "dom_apply_ms": 1,
            "measurement_age_upper_ms": 500 + i, "relay_age_ms": 100,
            "upstream_rtt_ms": 20, "refresh_interval_ms": 500,
            "request_interval_ms": 500 if i else None,
            "measurement_update_interval_ms": 999999,  # Must be independently reconstructed.
        })
    return {
        "schema": 2, "labels": {"source": "manual", "device": device, "adapter": adapter,
                                 "network": network, "motor": motor},
        "source_modes": ["ble"], "session_source_modes": ["ble"],
        "interval": {"duration_ms": max(0, (count - 1) * step), "target_duration_ms": 120000,
                     "stop_reason": "duration", "dropped_observations": 0, "observation_limit": 600},
        "metrics": {"http_rtt_ms": {"p50": -900}}, "counts": {"observations": 100000},
        "observations": rows,
    }


def make_failure(data, i=2, *, http=False, json_success=False, mode="unknown"):
    row = data["observations"][i]
    row.update(success=False, new_measurement=False, http_success=http,
               json_success=json_success, source_mode=mode, source_fresh=None,
               source_state="unknown", received_at=None, measurement_age_upper_ms=None)
    data["source_modes"] = sorted({p["source_mode"] for p in data["observations"]})
    data["session_source_modes"] = data["source_modes"][:]


def test_raw_percentiles_ignore_fabricated_summary_and_rebuild_update_intervals():
    summary = summarize_export(exported())
    assert summary["metrics"]["fresh_display"]["http_rtt_ms"] == {"count": 61, "p50": 31.0, "p95": 58.0, "max": 61.0}
    assert summary["metrics"]["fresh_display"]["measurement_update_interval_ms"] == {"count": 60, "p50": 2000.0, "p95": 2000.0, "max": 2000.0}
    assert summary["counts"]["observations"] == 61
    assert summary["quality"]["eligible_for_condition_matched_comparison"]


def test_failure_statistics_do_not_hide_http_failure_or_pollute_fresh_metrics():
    data = exported()
    make_failure(data)
    data["observations"][2]["http_rtt_ms"] = 4000
    summary = summarize_export(data)
    assert summary["counts"]["http_failure"] == 1
    assert summary["counts"]["not_fresh_display_success"] == 1
    assert summary["metrics"]["all_attempts"]["http_rtt_ms"]["max"] == 4000
    assert summary["metrics"]["fresh_display"]["http_rtt_ms"]["max"] == 61
    assert summary["quality"]["eligible_for_condition_matched_comparison"]


def test_http_success_stale_is_not_http_failure():
    data = exported()
    make_failure(data, http=True, json_success=True, mode="ble")
    data["observations"][2].update(source_fresh=False, source_state="stale", measurement_age_upper_ms=90000)
    summary = summarize_export(data)
    assert summary["counts"]["http_failure"] == 0
    assert summary["counts"]["http_success_without_fresh_display"] == 1
    assert summary["metrics"]["fresh_display"]["measurement_age_upper_ms"]["max"] == 560
    assert summary["metrics"]["unsuccessful_display"]["measurement_age_upper_ms"]["max"] == 90000


@pytest.mark.parametrize("axis,change", [("network", {"network": "home_router"}),
                                        ("adapter", {"adapter": "internal"}),
                                        ("device", {"device": "android"})])
def test_single_requested_factor_allows_only_descriptive_comparison(axis, change):
    a, b = exported(), exported(**change)
    for row in b["observations"]:
        row["http_rtt_ms"] += 10
    comparison = analyze_exports([a, b], axis)["comparisons"][0]
    assert comparison["status"] == "condition_matched_descriptive_comparison"
    assert comparison["second_minus_first_ms"]["http_rtt_ms"]["p50"] == 10
    assert comparison["causal_improvement_established"] is False


@pytest.mark.parametrize("change,axis", [({}, "network"), ({"device": "android", "network": "home_router"}, "network"),
                                       ({"adapter": "internal"}, "network"), ({"motor": "running"}, "adapter")])
def test_multiple_wrong_or_no_changed_factors_do_not_get_delta(change, axis):
    result = analyze_exports([exported(), exported(**change)], axis)["comparisons"][0]
    assert result["status"] == "comparison_not_established"
    assert result["second_minus_first_ms"] is None
    assert "exactly_requested_factor_must_differ" in result["reasons"]


@pytest.mark.parametrize("field,value", [("device", "unknown"), ("adapter", "unknown"), ("motor", "unknown"),
                                        ("network", "unknown"), ("device", "surface"), ("network", "hotspot")])
def test_unknown_or_nonspecific_conditions_never_qualify(field, value):
    data = exported()
    data["labels"][field] = value
    assert not summarize_export(data)["quality"]["eligible_for_condition_matched_comparison"]


def test_duplicates_dont_meet_minimum_distinct_measurement_requirement():
    data = exported()
    first = data["observations"][0]["received_at"]
    for row in data["observations"]:
        row["received_at"] = first
    summary = summarize_export(data)
    assert summary["counts"]["distinct_fresh_measurements"] == 1
    assert summary["counts"]["new_measurement_flag_mismatches"] == 60
    assert "fewer_than_30_distinct_fresh_measurements" in summary["quality"]["reasons"]


def test_nonconsecutive_duplicate_measurement_is_not_new():
    data = exported()
    data["observations"][3]["received_at"] = data["observations"][0]["received_at"]
    summary = summarize_export(data)
    assert summary["counts"]["distinct_fresh_measurements"] == 60
    assert summary["metrics"]["fresh_display"]["measurement_update_interval_ms"]["max"] == 4000


def test_short_window_not_faked_by_long_declared_duration():
    data = exported(count=31, step=500)
    data["interval"]["duration_ms"] = 120000
    summary = summarize_export(data)
    assert not summary["quality"]["sufficient_observations"]
    assert summary["interval"]["retained_observed_span_ms"] == 15000


@pytest.mark.parametrize("mode", ["mock", "unknown"])
def test_unverified_or_mock_fresh_source_never_real_comparison(mode):
    data = exported()
    for row in data["observations"]:
        row["source_mode"] = mode
    data["source_modes"] = data["session_source_modes"] = [mode]
    assert not summarize_export(data)["quality"]["eligible_for_condition_matched_comparison"]


def test_mixed_session_cannot_be_hidden_by_truncation():
    data = exported()
    data["session_source_modes"] = ["ble", "mock"]
    summary = summarize_export(data)
    assert "source_not_consistently_real_ble" in summary["quality"]["reasons"]
    data["interval"]["dropped_observations"] = 1
    assert "retained_window_truncated" in summarize_export(data)["quality"]["reasons"]


def test_decoded_unknown_response_source_prevents_controlled_comparison():
    data = exported()
    make_failure(data, http=True, json_success=True)
    assert "decoded_response_source_unknown" in summarize_export(data)["quality"]["reasons"]


@pytest.mark.parametrize("field", ["poll_seconds", "stale_after_seconds"])
def test_unknown_or_variable_poll_and_stale_threshold(field):
    for value in (None, 3):
        data = exported()
        data["observations"][2][field] = value
        assert not summarize_export(data)["quality"]["eligible_for_condition_matched_comparison"]


@pytest.mark.parametrize("field", ["poll_seconds", "stale_after_seconds"])
def test_transient_threshold_change_in_stale_response_is_not_hidden(field):
    data = exported()
    make_failure(data, http=True, json_success=True, mode="ble")
    data["observations"][2].update(source_fresh=False, source_state="stale")
    data["observations"][2][field] = 3
    assert not summarize_export(data)["quality"]["eligible_for_condition_matched_comparison"]


def test_schema_two_session_source_evidence_is_required():
    data = exported()
    del data["session_source_modes"]
    with pytest.raises(AnalysisError, match="invalid_source_modes"):
        summarize_export(data)


def test_same_timestamp_different_source_counts_like_frontend_but_is_not_comparable():
    data = exported()
    data["observations"][1]["received_at"] = data["observations"][0]["received_at"]
    data["observations"][1]["source_mode"] = "mock"
    data["source_modes"] = data["session_source_modes"] = ["ble", "mock"]
    summary = summarize_export(data)
    assert summary["counts"]["distinct_fresh_measurements"] == 61
    assert summary["counts"]["new_measurement_flag_mismatches"] == 0
    assert not summary["quality"]["eligible_for_condition_matched_comparison"]


@pytest.mark.parametrize("field,value,reason", [("poll_seconds", 3, "poll_period_differs"),
                                                ("stale_after_seconds", 15, "freshness_threshold_differs")])
def test_different_periods_across_recordings_are_not_comparable(field, value, reason):
    a, b = exported(), exported(network="home_router")
    for row in b["observations"]:
        row[field] = value
    result = analyze_exports([a, b])["comparisons"][0]
    assert reason in result["reasons"]


def test_different_window_lengths_reject_comparison():
    a, b = exported(), exported(network="home_router", count=41)
    assert "observation_windows_not_comparable" in analyze_exports([a, b])["comparisons"][0]["reasons"]


def test_clock_change_rejects_comparison():
    data = exported()
    data["interval"]["stop_reason"] = "clock_changed"
    assert "clock_changed" in summarize_export(data)["quality"]["reasons"]


def test_legacy_export_reads_without_inventing_device_source_or_http_results():
    data = exported()
    data["schema"] = 1
    for row in data["observations"]:
        for key in ("elapsed_ms", "source_mode", "http_success", "json_success", "poll_seconds", "stale_after_seconds"):
            del row[key]
    summary = summarize_export(data)
    assert summary["labels"]["device"] == "unknown"
    assert summary["fresh_source_modes"] == ["unknown"]
    assert summary["counts"]["http_success"] is None
    assert not summary["quality"]["eligible_for_condition_matched_comparison"]


@pytest.mark.parametrize("value", [True, False, -1, float("nan"), float("inf"), "42", 10**400, 1e13])
def test_invalid_metrics_rejected_even_when_not_a_successful_display(value):
    data = exported()
    make_failure(data)
    data["observations"][2]["relay_age_ms"] = value
    with pytest.raises(AnalysisError, match="invalid_numeric_value"):
        summarize_export(data)


def test_null_or_missing_metrics_remain_unavailable_not_zero():
    data = exported(count=1)
    data["observations"][0]["http_rtt_ms"] = None
    del data["observations"][0]["relay_age_ms"]
    summary = summarize_export(data)
    for name in ("http_rtt_ms", "relay_age_ms"):
        assert summary["metrics"]["fresh_display"][name] == {"count": 0, "p50": None, "p95": None, "max": None}


@pytest.mark.parametrize("schema", [True, "gtbms.surface-delivery.v1", "2", 3, None])
def test_only_phone_schema_versions_accepted(schema):
    data = exported()
    data["schema"] = schema
    with pytest.raises(AnalysisError, match="unsupported_phone_export_schema"):
        summarize_export(data)


@pytest.mark.parametrize("mutation", [
    lambda d: d["observations"][0].update(success=1),
    lambda d: d["observations"][0].update(http_success=False),
    lambda d: d["observations"][0].update(received_at=None),
    lambda d: d["observations"][0].update(observed_at="a private URL"),
    lambda d: d["observations"][0].update(observed_at="2026-10-11T00:00:00"),
    lambda d: d["observations"][2].update(elapsed_ms=0),
    lambda d: d["observations"][-1].update(elapsed_ms=999999),
    lambda d: d.update(source_modes=["mock"]),
    lambda d: d.update(session_source_modes=[]),
    lambda d: d.update(source_modes=["ble", "ble"]),
    lambda d: d["interval"].update(dropped_observations=True),
    lambda d: d["labels"].update(network="private SSID"),
])
def test_malformed_known_fields_fail_closed_without_value_echo(mutation):
    data = exported()
    mutation(data)
    with pytest.raises(AnalysisError) as result:
        summarize_export(data)
    assert "private" not in str(result.value)


def test_unknown_fields_and_dates_are_never_copied_into_report():
    data = exported()
    data.update(token="TOP-SECRET", url="http://private-host/", filepath="C:\\private\\user.json")
    data["labels"]["ssid"] = "HIDDEN-SSID"
    data["observations"][0]["payload"] = {"gps": "LOCATION"}
    report = json.dumps(analyze_exports([data]))
    for private in ("TOP-SECRET", "private-host", "private", "HIDDEN-SSID", "LOCATION", "2026-10-11"):
        assert private not in report


def test_point_and_file_count_bounds():
    data = exported()
    data["observations"] = [data["observations"][0]] * (MAX_POINTS + 1)
    with pytest.raises(AnalysisError, match="invalid_observation_count"):
        summarize_export(data)
    for records in ([], [exported()] * (MAX_FILES + 1)):
        with pytest.raises(AnalysisError, match="input_file_count"):
            analyze_exports(records)


@pytest.mark.parametrize("raw", [b'{"schema": NaN}', b'{"schema": 1, "schema": 2}', b'\xff', b'{'])
def test_strict_json_loading(raw, tmp_path):
    path = tmp_path / "private-secret-name.json"
    path.write_bytes(raw)
    with pytest.raises(AnalysisError) as error:
        load_export(path)
    assert str(path) not in str(error.value)


def test_json_size_bound(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (MAX_BYTES + 1))
    with pytest.raises(AnalysisError, match="input_too_large"):
        load_export(path)


def test_cli_summary_and_exclusive_output(tmp_path, capsys):
    source = tmp_path / "secret-person-name.json"
    destination = tmp_path / "result.json"
    source.write_text(json.dumps(exported()), encoding="utf-8")
    assert main([str(source), "--output", str(destination)]) == 0
    result = destination.read_bytes()
    assert "secret-person" not in result.decode()
    assert main([str(source), "--output", str(destination)]) == 2
    assert destination.read_bytes() == result
    output = capsys.readouterr()
    assert "output_already_exists" in output.err
    assert str(tmp_path) not in output.err + output.out


def test_cli_never_overwrites_input(tmp_path):
    path = tmp_path / "original.json"
    path.write_text(json.dumps(exported()), encoding="utf-8")
    original = path.read_bytes()
    assert main([str(path), "--output", str(path)]) == 2
    assert path.read_bytes() == original


def test_cli_input_error_identifies_only_ordinal(tmp_path, capsys):
    path = tmp_path / "secret-name.json"
    assert main([str(path), "--output", str(tmp_path / "out.json")]) == 2
    error = json.loads(capsys.readouterr().err)
    assert error == {"ok": False, "error": "input_unreadable_or_invalid_json", "input_number": 1}
    assert not (tmp_path / "out.json").exists()


def test_empty_window_is_valid_but_insufficient():
    data = exported(count=0)
    data["source_modes"] = data["session_source_modes"] = []
    summary = summarize_export(data)
    assert summary["counts"]["observations"] == 0
    assert not summary["quality"]["sufficient_observations"]


def test_summary_validation_does_not_mutate_input():
    data = exported()
    original = deepcopy(data)
    analyze_exports([data])
    assert data == original

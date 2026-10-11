from datetime import timedelta

import pytest

from conftest import timeline
from gtbms.analysis import energy_summary, gps_summary, predict, signed_energy, RideService
from gtbms.models import stamp,utcnow


def summarize(samples,max_gap=10):
    rows=[s.to_dict() for s in samples]
    return energy_summary(rows,rows[0]["received_at"],rows[-1]["received_at"],max_gap)


def test_constant_power_variable_intervals():
    points=timeline(3,2)
    points[-1].received_at=stamp(utcnow()+timedelta(seconds=1))
    # Build a precise 2s then 3s series.
    from gtbms.models import parse_time
    points[-1].received_at=stamp(parse_time(points[0].received_at)+timedelta(seconds=5))
    points[-1].monotonic_s=1005
    result=summarize(points)
    assert result["discharge_wh"] == pytest.approx(600*5/3600)
    assert result["coverage"] == 1


def test_zero_crossing_separates_charge():
    assert signed_energy(100,-100,3600) == (25,25)


def test_charge_is_separate():
    result=summarize(timeline(3,2,raw_current_a=10))
    assert result["discharge_wh"] == 0
    assert result["charge_wh"] == pytest.approx(600*4/3600)


@pytest.mark.parametrize("change", ["gap","epoch","unconfirmed","clock","quality"])
def test_unreliable_intervals_excluded(change):
    points=timeline(2,2)
    if change=="gap": points=timeline(2,100)
    if change=="epoch": points[1].connection_id="epoch-2"
    if change=="unconfirmed": points[1].current_direction="unconfirmed"
    if change=="clock": points[1].monotonic_s+=100
    if change=="quality": points[1].quality_flags=["fault"]
    result=summarize(points)
    assert result["covered_seconds"] == 0
    assert result["net_wh"] == 0
    assert result["reliability"] == "算出不能"


def test_stop_consumption_not_assumed_without_gps():
    result=summarize(timeline(2,2))
    assert result["stationary_discharge_wh"]==0
    assert result["moving_discharge_wh"]==0
    assert result["unclassified_discharge_wh"]>0


def test_gps_time_synchronization():
    points=timeline(2,2)
    rows=[s.to_dict() for s in points]
    gps=[{"measured_at":s.received_at,"speed_kmh":0,"accuracy_m":5} for s in points]
    result=energy_summary(rows,points[0].received_at,points[-1].received_at,10,gps)
    assert result["stationary_discharge_wh"]>0


def good_trip(n=0):
    return {"ended_at":"2026-01-01", "distance_km":10,"wh_per_km":20+n,"average_speed_kmh":18,
            "energy":{"coverage":1,"duration_seconds":1000},"prediction_error_wh_per_km":1}


def test_prediction_needs_real_history(config):
    assert predict(config,[],100)["full_range_km"] is None


def test_reference_capacity_and_reserve(config):
    p=predict(config,[good_trip()],50)
    assert p["full_range_km"]==pytest.approx(2960/20)
    assert p["remaining_range_km"]==pytest.approx(148/2)
    assert p["safe_remaining_range_km"]<p["remaining_range_km"]
    assert "参考値" in p["capacity_basis"]
    assert p["sample_trips"]==1


def test_measured_capacity_and_error_evaluation(config):
    config.measured_usable_wh=2500
    p=predict(config,[good_trip(i) for i in range(10)],0)
    assert p["reliability"]=="高い"
    assert p["mae_wh_per_km"]==1
    assert p["safe_remaining_range_km"]==0
    assert p["remaining_range_km"]==0


def test_no_training_partial_or_charge_session(config):
    t=good_trip();t["energy"]["coverage"]=.5
    assert predict(config,[t],100)["sample_trips"]==0
    t=good_trip();t["wh_per_km"]=-1
    assert predict(config,[t],100)["sample_trips"]==0


def test_missing_soc_never_uses_old_soc(config):
    p=predict(config,[good_trip()],None)
    assert p["remaining_range_km"] is None
    assert p["full_range_km"]>0


def test_gps_jumps_not_distance():
    now=utcnow()
    gps=[{"latitude":35,"longitude":139,"measured_at":stamp(now)},
         {"latitude":40,"longitude":140,"measured_at":stamp(now+timedelta(seconds=2))}]
    result=gps_summary(gps)
    assert result["gps_distance_km"] is None
    assert result["gps_excluded_segments"]==1


def test_session_manual_distance_and_correction(store):
    rides=RideService(store)
    trip=rides.start(20,80)
    with pytest.raises(ValueError): rides.start()
    finished=rides.stop(12)
    assert finished["id"]==trip["id"] and finished["distance_source"]=="manual"
    assert finished["energy"]["reliability"]=="算出不能"
    updated=rides.set_distance(trip["id"],15)
    assert updated["distance_km"]==15
    assert updated["wh_per_km"] is None


def test_partial_gps_ends_never_train_full_trip_energy(store, config, monkeypatch):
    start = utcnow()-timedelta(seconds=120)
    end = start+timedelta(seconds=120)
    trip = {"id": "partial-gps", "started_at": stamp(start), "ended_at": None,
            "baseline_wh_per_km": None, "max_gap_seconds": 10}
    store.save_trip(trip)
    for sample in timeline(61, 2, start=start):
        store.save_sample(sample)
    rides = RideService(store)
    for seconds, longitude in ((30, 139), (40, 139.001)):
        rides.save_gps(trip["id"], {"measured_at": stamp(start+timedelta(seconds=seconds)),
                                  "latitude": 35, "longitude": longitude, "accuracy_m": 5})
    monkeypatch.setattr("gtbms.analysis.stamp", lambda: stamp(end))
    result = rides.stop(None)
    assert result["energy"]["coverage"] == 1
    assert result["gps_excluded_segments"] == 0
    assert result["gps_coverage"] == pytest.approx(10/120)
    assert result["gps_missing_seconds"] == 110
    assert result["distance_quality"] == "partial_gps"
    assert predict(config, [result], 80)["sample_trips"] == 0
    corrected = rides.set_distance(trip["id"], 1)
    assert corrected["distance_quality"] == "manual"
    assert predict(config, [corrected], 80)["sample_trips"] == 1


def test_gps_unknown_accuracy_does_not_assert_distance_or_movement():
    samples = [s.to_dict() for s in timeline(2)]
    points = [{"measured_at": row["received_at"], "latitude": 35, "longitude": 139,
               "speed_kmh": 0, "accuracy_m": None} for row in samples]
    result = gps_summary(points, samples[0]["received_at"], samples[-1]["received_at"])
    assert result["gps_distance_km"] is None
    assert result["gps_coverage"] == 0
    energy = energy_summary(samples, samples[0]["received_at"], samples[-1]["received_at"], 10, points)
    assert energy["stationary_discharge_wh"] == 0
    assert energy["unclassified_discharge_wh"] > 0


def test_gps_needs_an_altitude_pair_to_report_elevation_change():
    now = utcnow()
    points = [{"measured_at": stamp(now), "latitude": 35, "longitude": 139,
               "accuracy_m": 5, "altitude_m": 100},
              {"measured_at": stamp(now+timedelta(seconds=10)), "latitude": 35,
               "longitude": 139.001, "accuracy_m": 5, "altitude_m": None}]
    result = gps_summary(points)
    assert result["gps_valid_segments"] == 1
    assert result["ascent_m"] is None and result["descent_m"] is None


def test_closed_trip_rejects_late_gps_without_stale_summary(store):
    rides = RideService(store)
    trip = rides.start()
    point = {"measured_at": stamp(), "latitude": 35, "longitude": 139, "accuracy_m": 5}
    finished = rides.stop(1)
    with pytest.raises(ValueError, match="終了済み"):
        rides.save_gps(trip["id"], point)
    assert store.gps_points(trip["id"]) == []
    assert store.trip(trip["id"])["revision_id"] == finished["revision_id"]


def test_parallel_stop_finalizes_once(store, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor, TimeoutError

    rides = RideService(store)
    trip = rides.start()
    original = store.open_trip
    entered, release = threading.Event(), threading.Event()

    def pause_first_snapshot():
        snapshot = original()
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return snapshot

    monkeypatch.setattr(store, "open_trip", pause_first_snapshot)
    with ThreadPoolExecutor(2) as executor:
        first = executor.submit(rides.stop, 10)
        assert entered.wait(5)
        second = executor.submit(rides.stop, 20)
        try:
            with pytest.raises(TimeoutError):
                second.result(timeout=.05)
        finally:
            release.set()
        assert first.result(timeout=5)["distance_km"] == 10
        with pytest.raises(ValueError, match="開始中"):
            second.result(timeout=5)
    assert store.trip(trip["id"])["distance_km"] == 10
    assert len(store.predictions()) == 1


def test_prediction_large_observed_error_reduces_confidence(config):
    config.measured_usable_wh = 2500
    trips = [good_trip() for _ in range(10)]
    for trip in trips:
        trip["prediction_error_wh_per_km"] = 1000
    result = predict(config, trips, 80)
    assert result["relative_mae"] == 50
    assert result["reliability"] == "低い"


def test_legacy_gps_without_coverage_is_not_assumed_complete(config):
    trip = good_trip()
    trip.update(distance_source="gps", distance_quality="gps", gps_excluded_segments=0)
    assert predict(config, [trip], 80)["sample_trips"] == 0
    trip["gps_coverage"] = .95
    assert predict(config, [trip], 80)["sample_trips"] == 1


def test_unclassified_charge_cannot_inflate_range_from_legacy_trip(config):
    trip = good_trip()
    trip.update(wh_per_km=1, baseline_wh_per_km=25, prediction_error_wh_per_km=-24)
    trip["energy"].update(discharge_wh=300, charge_wh=290, net_wh=10, covered_seconds=1000)
    prediction = predict(config, [trip], 100)
    assert prediction["mean_wh_per_km"] == 30
    assert prediction["latest_wh_per_km"] == 30
    assert prediction["full_range_km"] == pytest.approx(2960/30)
    assert prediction["mae_wh_per_km"] == 5
    assert prediction["conditions"][0]["mean_wh_per_km"] == 30
    assert "放電Wh/距離" in prediction["consumption_basis"]


def test_ride_preserves_net_consumption_and_forecasts_without_charge_credit():
    trip = good_trip()
    trip.update(baseline_wh_per_km=25)
    trip["energy"].update(discharge_wh=300, charge_wh=290, net_wh=10, covered_seconds=1000)
    RideService.calculate_metrics(trip)
    assert trip["wh_per_km"] == 1
    assert trip["forecast_wh_per_km"] == 30
    assert trip["prediction_error_wh_per_km"] == 5
    trip["distance_km"] = 20
    RideService.calculate_metrics(trip)
    assert trip["wh_per_km"] == .5
    assert trip["forecast_wh_per_km"] == 15
    assert trip["prediction_error_wh_per_km"] == -10


def test_forecast_consumption_never_below_net_and_legacy_error_not_mixed(config):
    trip = good_trip()
    trip["energy"].update(discharge_wh=300)
    # The old imported error refers to 20 Wh/km, not the newly derived 30 Wh/km.
    result = predict(config, [trip], 100)
    assert result["mean_wh_per_km"] == 30
    assert result["error_samples"] == 0
    trip["wh_per_km"] = 35
    assert predict(config, [trip], 100)["mean_wh_per_km"] == 35

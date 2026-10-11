from __future__ import annotations

import bisect
import math
import statistics
import threading
from functools import wraps
from uuid import uuid4

from .config import Config
from .models import parse_time, stamp, utcnow
from .storage import Store


def signed_energy(p0: float, p1: float, seconds: float) -> tuple[float, float]:
    """Integrate linear power, separating positive discharge and negative charge."""
    if p0 * p1 < 0:
        first = seconds * abs(p0)/(abs(p0)+abs(p1))
        a, b = signed_energy(p0, 0, first), signed_energy(0, p1, seconds-first)
        return a[0]+b[0], a[1]+b[1]
    wh = (p0+p1)*.5*seconds/3600
    return max(wh, 0), max(-wh, 0)


def energy_summary(samples: list[dict], start: str, end: str, max_gap: float,
                   gps: list[dict] | None = None) -> dict:
    begin, finish = parse_time(start), parse_time(end)
    duration = max(0, (finish-begin).total_seconds())
    discharge, charge, covered = 0., 0., 0.
    categories = {"moving": 0., "stationary": 0., "unknown": 0.}
    gps = sorted(gps or [], key=lambda p: p["measured_at"])
    gps_times = [parse_time(p["measured_at"]).timestamp() for p in gps]

    def movement(t):
        idx = bisect.bisect_right(gps_times, t)-1
        if (idx >= 0 and t-gps_times[idx] <= 5 and gps[idx].get("speed_kmh") is not None
                and gps[idx].get("accuracy_m") is not None and gps[idx]["accuracy_m"] <= 50):
            return "moving" if gps[idx]["speed_kmh"] > 2 else "stationary"
        return "unknown"

    for a, b in zip(samples, samples[1:]):
        ta, tb = parse_time(a["received_at"]), parse_time(b["received_at"])
        dt = (tb-ta).total_seconds()
        if dt <= 0 or dt > max_gap or a.get("power_w") is None or b.get("power_w") is None:
            continue
        if a.get("quality_flags") or b.get("quality_flags"):
            continue
        if not a.get("connection_id") or a["connection_id"] != b.get("connection_id"):
            continue
        if a.get("current_direction") != b.get("current_direction"):
            continue
        monotonic_dt = b.get("monotonic_s", 0)-a.get("monotonic_s", 0)
        if monotonic_dt <= 0 or abs(monotonic_dt-dt) > max(.5, dt*.1):
            continue  # Clock jump or process restart: do not integrate it.
        left, right = max(ta, begin), min(tb, finish)
        if right <= left:
            continue
        span = (right-left).total_seconds()
        p0, p1 = a["power_w"], b["power_w"]
        slope = (p1-p0)/dt
        lo, hi = p0+slope*(left-ta).total_seconds(), p0+slope*(right-ta).total_seconds()
        d, c = signed_energy(lo, hi, span)
        discharge += d
        charge += c
        covered += span
        ca, cb = movement(left.timestamp()), movement(right.timestamp())
        categories[ca if ca == cb else "unknown"] += d
    coverage = min(1, covered/duration) if duration else 0
    return {"discharge_wh": discharge, "charge_wh": charge, "net_wh": discharge-charge,
            "covered_seconds": covered, "duration_seconds": duration, "coverage": coverage,
            "missing_seconds": max(0, duration-covered), "moving_discharge_wh": categories["moving"],
            "stationary_discharge_wh": categories["stationary"], "unclassified_discharge_wh": categories["unknown"],
            "charge_origin": "未判別（充電器／回生）", "energy_basis": "電圧×符号確認済み電流の積算",
            "reliability": "十分" if coverage >= .9 else "低い" if covered else "算出不能"}


def haversine(a: dict, b: dict) -> float:
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dlat = lat2-lat1
    dlon = math.radians(b["longitude"]-a["longitude"])
    v = math.sin(dlat/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2
    return 6371 * 2 * math.asin(min(1, math.sqrt(v)))


def gps_summary(points: list[dict], start: str | None = None, end: str | None = None) -> dict:
    """Describe observed GPS segments; session boundaries expose unobserved ends."""
    distance, ascent, descent, valid, excluded = 0., 0., 0., 0, 0
    covered, altitude_pairs = 0., 0
    points = sorted(points, key=lambda point: parse_time(point["measured_at"]))
    begin, finish = parse_time(start) if start else None, parse_time(end) if end else None
    duration = max(0., (finish-begin).total_seconds()) if begin is not None and finish is not None else None
    speeds = []
    for a, b in zip(points, points[1:]):
        dt = (parse_time(b["measured_at"])-parse_time(a["measured_at"])).total_seconds()
        km = haversine(a, b)
        if (dt <= 0 or dt > 30 or km/dt*3600 > 100
                or any(p.get("accuracy_m") is None or p["accuracy_m"] > 50 for p in (a, b))):
            excluded += 1
            continue
        distance += km
        valid += 1
        if begin is not None and finish is not None:
            left = max(begin, parse_time(a["measured_at"]))
            right = min(finish, parse_time(b["measured_at"]))
            covered += max(0., (right-left).total_seconds())
        speeds.append(km/dt*3600)
        if a.get("altitude_m") is not None and b.get("altitude_m") is not None:
            altitude_pairs += 1
            diff = b["altitude_m"]-a["altitude_m"]
            ascent += max(diff, 0)
            descent += max(-diff, 0)
    return {"gps_distance_km": distance if valid else None, "max_speed_kmh": max(speeds) if speeds else None,
            "ascent_m": ascent if altitude_pairs else None,
            "descent_m": descent if altitude_pairs else None,
            "gps_valid_segments": valid, "gps_excluded_segments": excluded,
            "gps_coverage": min(1., covered/duration) if duration else 0. if duration == 0 else None,
            "gps_covered_seconds": covered if duration is not None else None,
            "gps_missing_seconds": max(0., duration-covered) if duration is not None else None,
            "gps_quality_basis": "精度50m以内・30秒以内の位置区間。取得されていない区間の距離は不明"}


def forecast_consumption(trip: dict) -> float | None:
    """Do not treat unclassified charging as guaranteed regenerative savings."""
    net = trip.get("wh_per_km") or 0
    distance = trip.get("distance_km")
    discharge = trip.get("energy", {}).get("discharge_wh")
    if distance and discharge is not None:
        return max(discharge/distance, net)
    return max(trip.get("forecast_wh_per_km") or 0, net) or None


def forecast_error(trip: dict) -> float | None:
    value = forecast_consumption(trip)
    baseline = trip.get("baseline_wh_per_km")
    if value is not None and baseline is not None:
        return value-baseline
    # Historical imports without a baseline may already supply an error. Legacy
    # net-consumption errors cannot evaluate a changed discharge-based measure.
    if (value is not None and trip.get("wh_per_km") is not None
            and not math.isclose(value, trip["wh_per_km"])):
        return None
    return trip.get("prediction_error_wh_per_km")


def predict(config: Config, trips: list[dict], soc: float | None) -> dict:
    usable = config.measured_usable_wh or config.nominal_voltage_v * config.configured_capacity_ah
    basis = "実測使用可能電力量（利用者設定）" if config.measured_usable_wh else "設定容量×公称電圧の参考値"
    good = [t for t in trips if t.get("ended_at") and (forecast_consumption(t) or 0) > 0
            and t.get("energy", {}).get("coverage", 0) >= .9
            and t["energy"]["duration_seconds"] >= 30 and t.get("distance_km", 0) >= .05
            and t.get("distance_quality") != "partial_gps"
            and (t.get("distance_source") != "gps"
                 or ((t.get("gps_coverage") or 0) >= .9 and not t.get("gps_excluded_segments")))][:20]
    result = {"sample_trips": len(good), "usable_energy_wh": usable, "capacity_basis": basis,
              "full_range_km": None, "remaining_range_km": None, "safe_full_range_km": None,
              "safe_remaining_range_km": None, "mean_wh_per_km": None, "latest_wh_per_km": None,
              "reliability": "データ不足", "mae_wh_per_km": None, "error_samples": 0,
              "relative_mae": None,
              "consumption_basis": "予測は放電Wh/距離。由来未判別の充電を航続距離へ加算しない保守的推定",
              "conditions": [], "soc_basis": "SOC比例の推定（残容量の実測ではない）"}
    if not good:
        return result
    weights = [(0.9**i)*min(t["distance_km"], 100) for i, t in enumerate(good)]
    values = [forecast_consumption(t) for t in good]
    mean = sum(v*w for v,w in zip(values, weights))/sum(weights)
    sorted_values = sorted(values)
    cautious = max(mean*1.15, sorted_values[math.ceil(len(values)*.9)-1])
    errors = [abs(error) for t in good if (error := forecast_error(t)) is not None]
    cv = statistics.pstdev(values)/mean
    relative_mae = statistics.mean(errors)/mean if errors else None
    # Heuristic labels, not calibrated confidence intervals. Large observed errors
    # cannot become high confidence merely by collecting more predictions.
    confidence = ("高い" if len(good) >= 10 and len(errors) >= 5 and cv <= .2 and relative_mae <= .2
                  else "中程度" if len(good) >= 5 and len(errors) >= 3 and cv <= .5 and relative_mae <= .35
                  else "低い")
    if not config.measured_usable_wh:
        confidence += "／容量は参考値"
    for label, predicate in (("低速（平均20km/h未満）", lambda t: t.get("average_speed_kmh", 0) < 20),
                             ("高速（平均20km/h以上）", lambda t: t.get("average_speed_kmh", 0) >= 20),
                             ("低温（外気温10℃未満）", lambda t: t.get("ambient_c") is not None and t["ambient_c"] < 10),
                             ("外気温10℃以上", lambda t: t.get("ambient_c") is not None and t["ambient_c"] >= 10)):
        group = [t for t in good if predicate(t)]
        result["conditions"].append({"condition": label, "trips": len(group),
                                     "mean_wh_per_km": statistics.mean(forecast_consumption(t) for t in group) if group else None})
    result.update(mean_wh_per_km=mean, latest_wh_per_km=values[0], variability_cv=cv,
                  full_range_km=usable/mean, safe_full_range_km=usable*(1-config.reserve_fraction)/cautious,
                  remaining_range_km=usable*soc/100/mean if soc is not None and 0 <= soc <= 100 else None,
                  safe_remaining_range_km=usable*max(0, soc/100-config.reserve_fraction)/cautious if soc is not None and 0 <= soc <= 100 else None,
                  reliability=confidence, mae_wh_per_km=statistics.mean(errors) if errors else None,
                  error_samples=len(errors), relative_mae=relative_mae)
    return result


def serialize_ride_change(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapped


class RideService:
    def __init__(self, store: Store, latest_provider=None):
        self.store = store
        self.config = store.config
        self.latest_provider = latest_provider
        self.lock = threading.RLock()

    def fresh_latest(self):
        if self.latest_provider is not None:
            return self.latest_provider()
        sample = self.store.latest()
        if sample and 0 <= (utcnow()-parse_time(sample["received_at"])).total_seconds() <= self.config.stale_seconds:
            return sample
        return None

    @serialize_ride_change
    def start(self, ambient_c=None, payload_kg=None):
        if self.store.open_trip():
            raise ValueError("走行セッションが既に開始されています")
        latest = self.fresh_latest()
        baseline = predict(self.config, self.store.trips(), latest["soc_pct"] if latest else None)
        trip = {"id": str(uuid4()), "battery_id": self.config.key, "source": self.config.mode,
                "started_at": stamp(), "ended_at": None, "start_soc_pct": latest["soc_pct"] if latest else None,
                "start_ntc_c": latest["temperatures_c"] if latest else None,
                "ambient_c": ambient_c, "payload_kg": payload_kg,
                "baseline_wh_per_km": baseline["mean_wh_per_km"], "max_gap_seconds": self.config.max_gap_seconds}
        self.store.save_trip(trip)
        self.store.event("trip_started")
        return trip

    @serialize_ride_change
    def stop(self, distance_km: float | None):
        trip = self.store.open_trip()
        if trip is None:
            raise ValueError("開始中の走行セッションがありません")
        end = stamp()
        samples = self.store.samples(start=trip["started_at"], end=end)
        points = self.store.gps_points(trip["id"])
        energy = energy_summary(samples, trip["started_at"], end, trip["max_gap_seconds"], points)
        gps = gps_summary(points, trip["started_at"], end)
        distance = distance_km if distance_km is not None else gps["gps_distance_km"]
        latest = self.fresh_latest()
        trip.update(ended_at=end, end_soc_pct=latest["soc_pct"] if latest else None,
                    end_ntc_c=latest["temperatures_c"] if latest else None,
                    mean_ntc_max_c=statistics.mean(max(s["temperatures_c"]) for s in samples if s["temperatures_c"]) if any(s["temperatures_c"] for s in samples) else None,
                    distance_km=distance, distance_source="manual" if distance_km is not None else "gps" if distance is not None else "unavailable",
                    distance_quality=("manual" if distance_km is not None else "unavailable" if distance is None
                                      else "partial_gps" if gps["gps_excluded_segments"] or gps["gps_coverage"] < .9 else "gps"),
                    energy=energy, **gps)
        self.calculate_metrics(trip)
        self.store.save_trip(trip)
        self.store.event("trip_finished")
        self.store.prediction(predict(self.config, self.store.trips(), latest["soc_pct"] if latest else None))
        return trip

    @staticmethod
    def calculate_metrics(trip):
        distance = trip.get("distance_km")
        energy = trip["energy"]
        trip["average_speed_kmh"] = distance/energy["duration_seconds"]*3600 if distance is not None and energy["duration_seconds"] else None
        trip["wh_per_km"] = energy["net_wh"]/distance if distance and energy["covered_seconds"] and energy["net_wh"] > 0 else None
        trip["forecast_wh_per_km"] = (max(energy["discharge_wh"]/distance, trip["wh_per_km"] or 0)
                                      if distance and energy["covered_seconds"] and energy["discharge_wh"] > 0 else None)
        trip["prediction_error_wh_per_km"] = (trip["forecast_wh_per_km"]-trip["baseline_wh_per_km"]
                                               if trip["forecast_wh_per_km"] is not None and trip.get("baseline_wh_per_km") is not None else None)

    @serialize_ride_change
    def set_distance(self, trip_id, distance_km):
        trip = self.store.trip(trip_id)
        if not trip.get("ended_at"):
            raise ValueError("終了後のセッションに距離を入力してください")
        trip.update(distance_km=distance_km, distance_source="manual", distance_quality="manual")
        self.calculate_metrics(trip)
        self.store.save_trip(trip)
        return trip

    @serialize_ride_change
    def save_gps(self, trip_id: str, point: dict):
        if self.store.trip(trip_id).get("ended_at"):
            raise ValueError("終了済みの走行セッションへGPSを追加できません")
        self.store.save_gps(trip_id, point)

"""Thermal pacing: pace_delay table + platform sensor behavior."""

from server.features import monitoring


def test_pace_floor_when_cool_or_sensorless():
    assert monitoring.pace_delay(None) == monitoring.PACE_FLOOR_SECONDS
    assert monitoring.pace_delay(60.0) == monitoring.PACE_FLOOR_SECONDS
    assert monitoring.pace_delay(79.9) == monitoring.PACE_FLOOR_SECONDS
    assert monitoring.pace_delay(80.0) == monitoring.PACE_FLOOR_SECONDS


def test_pace_scales_linearly_then_caps():
    assert monitoring.pace_delay(84.0) == monitoring.PACE_FLOOR_SECONDS + 8.0
    assert monitoring.pace_delay(88.0) == monitoring.PACE_FLOOR_SECONDS + 16.0
    assert monitoring.pace_delay(95.0) == monitoring.PACE_MAX_SECONDS
    assert monitoring.pace_delay(200.0) == monitoring.PACE_MAX_SECONDS


def test_platform_temp_missing_sensor_returns_none(monkeypatch, tmp_path):
    monkeypatch.setattr(monitoring, "PLATFORM_TEMP_PATH", str(tmp_path / "nope"))
    assert monitoring.get_platform_temp() is None
    # ... and pacing degrades to the silent floor, never raises.
    assert monitoring.pace_delay(monitoring.get_platform_temp()) == monitoring.PACE_FLOOR_SECONDS


def test_platform_temp_reads_millidegree_file(monkeypatch, tmp_path):
    f = tmp_path / "temp"
    f.write_text("82340\n")
    monkeypatch.setattr(monitoring, "PLATFORM_TEMP_PATH", str(f))
    assert monitoring.get_platform_temp() == 82.34


def test_platform_temp_garbage_returns_none(monkeypatch, tmp_path):
    f = tmp_path / "temp"
    f.write_text("not-a-temp\n")
    monkeypatch.setattr(monitoring, "PLATFORM_TEMP_PATH", str(f))
    assert monitoring.get_platform_temp() is None


def test_live_sensor_reports_sane_value():
    # Read-only: on boxes without acpitz this asserts the None path.
    t = monitoring.get_platform_temp()
    assert t is None or (0.0 < t < 125.0)

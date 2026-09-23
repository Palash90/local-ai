"""Thermal latch: hysteresis transitions + idle load-shedding (mocked M)."""

import threading
import types

from server.features import monitoring as mon
from server.features import state


def _M(temp, **over):
    base = dict(
        get_gpu_temp=lambda: temp,
        get_ram_usage=lambda: 10.0,
        _data_lock=threading.Lock(),
        _queue_locks={"gpu": threading.Lock()},
        _current_task_ids={"gpu": None},
        _gpu_temp=None,
        _overheated=False,
        _ram_evacuating=False,
        _image_active=False,
        TEMP_THRESHOLD_ON=90,
        TEMP_THRESHOLD_OFF=75,
        RAM_EVAC_THRESHOLD=95,
        model_status="chat_loaded",
        unload_llama_model=lambda mode: calls.append(("unload", mode)),
        free_comfyui_vram=lambda: calls.append(("free",)),
        _evacuate_ram=lambda: calls.append(("evac",)),
    )
    base.update(over)
    return types.SimpleNamespace(**base)


calls = []


def _run(m):
    global calls
    calls = []
    prev = state._Registry.entrypoint
    state.register_entrypoint(m)
    try:
        mon._thermal_step()
    finally:
        state.register_entrypoint(prev)
    return m


def test_cool_stays_clear():
    m = _run(_M(60))
    assert m._overheated is False and m._gpu_temp == 60
    assert calls == []


def test_overheat_latch_and_unload_when_idle():
    m = _run(_M(95))
    assert m._overheated is True
    assert ("unload", "gpu") in calls


def test_hysteresis_holds_between_thresholds():
    m = _M(80, _overheated=True)
    _run(m)
    assert m._overheated is True  # 80 < 90 but > 75: stays latched
    assert ("unload", "gpu") in calls  # latched + idle still sheds load


def test_resume_at_off_threshold():
    m = _run(_M(70, _overheated=True))
    assert m._overheated is False


def test_busy_lane_not_unloaded():
    m = _M(95, _current_task_ids={"gpu": "task-1"})
    _run(m)
    assert m._overheated is True
    assert calls == []  # busy round must never be surprised


def test_image_active_frees_comfyui():
    m = _M(95, model_status="unloaded", _image_active=True)
    _run(m)
    assert ("free",) in calls


def test_none_temp_resumes_but_sheds_nothing_decisive():
    m = _M(None, _overheated=True, model_status="unloaded")
    _run(m)
    assert m._overheated is False

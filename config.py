"""
Shared mutable config and live telemetry.
Both the audio loop and the web UI read/write this module.
"""

import threading

_lock = threading.Lock()

# ── Tunable parameters (all accessible via web UI) ─────────────────────────
params = {
    # Motor power
    "motor_max":               0.4,
    # Audio smoothing
    "smoothing":               0.7,
    "decay":                   0.05,
    # Beat detection
    "beat_threshold":          1.8,
    "beat_hold_frames":        6,
    # Direction
    "direction_flip_beats":    2,
    "tempo_change_threshold":  0.12,
    # State motor
    "state_angle":             90,
    # Per-band floors (noise gate) and scales (amplification)
    "bass_floor":              0.04,
    "bass_scale":              3.0,
    "mid_floor":               0.02,
    "mid_scale":               3.0,
    "melody_floor":            0.005,
    "melody_scale":            10.0,
}

# ── Live telemetry (written by audio loop, read by web UI) ─────────────────
_telemetry = {
    "levels":    {},   # {"A": 0.4, "B": 0.2, "D": 0.8}
    "direction": 1,
    "is_beat":   False,
    "bpm":       0.0,
}


def get_all() -> dict:
    with _lock:
        return dict(params)


def get(key):
    with _lock:
        return params[key]


def update(updates: dict):
    with _lock:
        for k, v in updates.items():
            if k in params:
                params[k] = type(params[k])(v)


def set_telemetry(data: dict):
    with _lock:
        _telemetry.update(data)


def get_telemetry() -> dict:
    with _lock:
        t = dict(_telemetry)
        t["levels"] = dict(_telemetry["levels"])
        return t

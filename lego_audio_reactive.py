#!/usr/bin/env python3
"""
LEGO Technic Audio-Reactive Motor Controller
Motors respond in real time to music/sound captured from the microphone.

Usage:
    .venv/bin/python3 lego_audio_reactive.py

Controls:
    ESC / Ctrl+C — stop
    Web UI       — http://localhost:7777
"""

import sys
import tty
import select
import termios
import threading
import time
import numpy as np
import sounddevice as sd
from pylgbst.hub import Hub
from pylgbst.peripherals import Motor
from pylgbst.comms.cbleak import BleakDriver
import config as cfg
import web_ui

# ── Static (not hot-reloadable) ────────────────────────────────────────────
SAMPLE_RATE      = 44100
CHUNK_SIZE       = 2048        # ~46ms per frame
HUB_NAME         = "Technic Hub"
STATE_MOTOR_PORT = "C"
BASS_RANGE       = (40, 200)
MID_RANGE        = (300, 2000)
MELODY_RANGE     = (1000, 8000)
# Per-motor signal override: port letter → "bass" | "mid" | "melody"
MOTOR_SIGNAL     = {"D": "melody"}
# ──────────────────────────────────────────────────────────────────────────

_stop = threading.Event()


# ── ESC key listener ───────────────────────────────────────────────────────

def _keyboard_thread():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not _stop.is_set():
            if select.select([sys.stdin], [], [], 0.1)[0]:
                if sys.stdin.read(1) == "\x1b":
                    print("\nESC — stopping…")
                    _stop.set()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ── Audio analysis ─────────────────────────────────────────────────────────

def _band_energy(fft_mag, freqs, low, high):
    mask = (freqs >= low) & (freqs <= high)
    band = fft_mag[mask]
    return float(np.sqrt(np.mean(band ** 2))) if len(band) else 0.0


def _to_power(energy, floor, scale):
    if energy < floor:
        return 0.0
    return min(1.0, (energy - floor) * scale)


# ── Hub connection ─────────────────────────────────────────────────────────

def connect_hub():
    print(f"Searching for '{HUB_NAME}'…  (press green button, LED should blink)")
    try:
        hub = Hub(connection=BleakDriver(hub_name=HUB_NAME))
    except Exception as e:
        print(f"Connection failed: {e}")
        return None, [], None

    print("Connected! Waiting for motors…")
    for _ in range(100):
        if any(isinstance(d, Motor) for d in hub.peripherals.values()):
            break
        time.sleep(0.1)

    motor_ports = sorted(p for p, d in hub.peripherals.items() if isinstance(d, Motor))
    port_letter = {port: chr(65 + i) for i, port in enumerate(motor_ports)}

    print("  Port map:")
    for port in motor_ports:
        print(f"    port {port} → {port_letter[port]}  ({type(hub.peripherals[port]).__name__})")

    state_motor  = None
    drive_motors = []
    for port in motor_ports:
        device = hub.peripherals[port]
        letter = port_letter[port]
        if STATE_MOTOR_PORT and letter == STATE_MOTOR_PORT.upper():
            state_motor = device
            print(f"  Motor {letter} → state switcher")
        else:
            drive_motors.append((letter, device))
            print(f"  Motor {letter} → drive")

    if not drive_motors and not state_motor:
        print("No motors found.")
        return hub, [], None

    return hub, drive_motors, state_motor


# ── State motor toggle ─────────────────────────────────────────────────────

def _toggle_state(motor, state_pos):
    power = 0.4 * state_pos[0]
    state_pos[0] *= -1
    angle = cfg.get("state_angle")

    def _run():
        try:
            motor.start_power(power)
            time.sleep(abs(angle) / 360.0 * 1.5)   # scale duration to angle
            motor.start_power(0)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


# ── Main ───────────────────────────────────────────────────────────────────

def run():
    web_ui.start()

    hub, motors, state_motor = connect_hub()
    if not motors and not state_motor:
        return

    labels  = [l for l, _ in motors]
    devices = [m for _, m in motors]
    signals = [MOTOR_SIGNAL.get(l, "bass" if i % 2 == 0 else "mid")
               for i, l in enumerate(labels)]
    print(f"  Driving: " + ", ".join(f"{l}={s}" for l, s in zip(labels, signals)))

    # Seed telemetry so web UI builds motor meters immediately (before audio starts)
    cfg.set_telemetry({"levels": {l: 0.0 for l in labels}, "direction": 1,
                       "is_beat": False, "bpm": 0.0})

    threading.Thread(target=_keyboard_thread, daemon=True).start()

    # Audio state
    smooth  = {"bass": 0.0, "mid": 0.0, "melody": 0.0}
    recent_energies  = []
    beat_hold        = 0
    direction        = 1
    beat_count       = 0
    last_beat_time   = None
    beat_intervals   = []
    state_pos        = [1]

    def audio_callback(indata, frames, time_info, status):
        nonlocal beat_hold, direction, beat_count, last_beat_time

        if _stop.is_set():
            return

        # Read all tunable params once per frame (one lock acquire)
        c = cfg.get_all()

        mono     = indata[:, 0] if indata.ndim > 1 else indata.flatten()
        windowed = mono * np.hanning(len(mono))
        fft      = np.abs(np.fft.rfft(windowed))
        freqs    = np.fft.rfftfreq(len(mono), d=1.0 / SAMPLE_RATE)

        raw = {
            "bass":   _band_energy(fft, freqs, *BASS_RANGE),
            "mid":    _band_energy(fft, freqs, *MID_RANGE),
            "melody": _band_energy(fft, freqs, *MELODY_RANGE),
        }
        total = _band_energy(fft, freqs, 20, 20000)

        # Beat detection
        recent_energies.append(total)
        if len(recent_energies) > 20:
            recent_energies.pop(0)
        avg      = np.mean(recent_energies) if recent_energies else total
        is_beat  = (total > avg * c["beat_threshold"]) and (total > c["bass_floor"] * 2)

        # Silence snap vs. attack/decay smoothing
        floors = {"bass": c["bass_floor"], "mid": c["mid_floor"], "melody": c["melody_floor"]}
        if all(raw[b] < floors[b] for b in floors) and beat_hold == 0:
            for b in smooth:
                smooth[b] = 0.0
        else:
            for b in smooth:
                alpha = c["smoothing"] if raw[b] >= smooth[b] else c["decay"]
                smooth[b] = alpha * smooth[b] + (1 - alpha) * raw[b]

        scales = {"bass": c["bass_scale"], "mid": c["mid_scale"], "melody": c["melody_scale"]}
        powers_map = {b: _to_power(smooth[b], floors[b], scales[b]) for b in smooth}

        # Beat burst + direction logic
        if is_beat:
            beat_hold = max(beat_hold, int(c["beat_hold_frames"]))

            now = time.time()
            if last_beat_time is not None:
                interval = now - last_beat_time
                if 0.2 < interval < 3.0:
                    beat_intervals.append(interval)
                    if len(beat_intervals) > 8:
                        beat_intervals.pop(0)
                    if len(beat_intervals) >= 3:
                        avg_i = np.mean(beat_intervals[:-1])
                        if abs(interval - avg_i) / avg_i > c["tempo_change_threshold"]:
                            direction  *= -1
                            beat_count  = 0
                            if state_motor:
                                _toggle_state(state_motor, state_pos)
            last_beat_time = now
            beat_count += 1
            if beat_count >= int(c["direction_flip_beats"]):
                direction  *= -1
                beat_count  = 0
                if state_motor:
                    _toggle_state(state_motor, state_pos)

        if beat_hold > 0:
            powers_map["bass"] = min(1.0, powers_map["bass"] + 0.4)
            beat_hold -= 1

        motor_powers = [powers_map[sig] for sig in signals]

        for motor, pwr in zip(devices, motor_powers):
            try:
                motor.start_speed(pwr * c["motor_max"] * direction)
            except Exception:
                pass

        # Update telemetry for web UI
        bpm = (60.0 / np.mean(beat_intervals)) if len(beat_intervals) >= 2 else 0.0
        cfg.set_telemetry({
            "levels":    {l: p for l, p in zip(labels, motor_powers)},
            "direction": direction,
            "is_beat":   is_beat,
            "bpm":       round(bpm, 1),
        })

        dir_sym    = "▶" if direction == 1 else "◀"
        status_str = "  ".join(f"{l}={p:4.0%}" for l, p in zip(labels, motor_powers))
        if is_beat:
            print(f"♩ BEAT {dir_sym} │ {status_str}")
        else:
            print(f"       {dir_sym} │ {status_str}", end="\r")

    print("Listening… (ESC to stop)")
    try:
        with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                            blocksize=CHUNK_SIZE, callback=audio_callback):
            _stop.wait()
    except KeyboardInterrupt:
        _stop.set()

    for motor in devices:
        try:
            motor.start_speed(0)
        except Exception:
            pass
    if state_motor:
        try:
            state_motor.start_power(0)
        except Exception:
            pass
    if hub:
        hub.connection.disconnect()
    print("Done.")


if __name__ == "__main__":
    run()

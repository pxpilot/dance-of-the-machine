#!/usr/bin/env python3
"""
LEGO Technic Audio-Reactive Motor Controller
Motors respond in real time to music/sound captured from the microphone.

Usage:
    .venv/bin/python3 lego_audio_reactive.py

Controls:
    ESC  — stop
    Ctrl+C — also stops
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

# ── Tuning ─────────────────────────────────────────────────────────────────
SAMPLE_RATE   = 44100
CHUNK_SIZE    = 2048        # ~46ms per frame
SMOOTHING     = 0.3         # 0=instant, 1=frozen
MOTOR_MAX     = 0.8         # max motor power (0.0–1.0)
BASS_FLOOR    = 0.01        # silence threshold
BEAT_THRESHOLD = 1.8        # onset energy multiplier to call a beat
BEAT_HOLD_FRAMES = 6        # frames to sustain beat burst
BASS_RANGE    = (40, 200)   # Hz → Motor A speed
MID_RANGE     = (300, 2000) # Hz → Motor B speed

# BLE name the hub advertises — change if connection fails
HUB_NAME = "Technic Hub"
# ──────────────────────────────────────────────────────────────────────────

_stop = threading.Event()


# ── ESC key listener ───────────────────────────────────────────────────────

def _keyboard_thread():
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not _stop.is_set():
            if select.select([sys.stdin], [], [], 0.1)[0]:
                ch = sys.stdin.read(1)
                if ch == '\x1b':
                    print("\nESC — stopping…")
                    _stop.set()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


# ── Audio analysis ─────────────────────────────────────────────────────────

def _band_energy(fft_mag, freqs, low, high):
    mask = (freqs >= low) & (freqs <= high)
    band = fft_mag[mask]
    return float(np.sqrt(np.mean(band ** 2))) if len(band) else 0.0


def _energy_to_power(energy, floor, scale=4.0):
    if energy < floor:
        return 0.0
    return min(1.0, (energy - floor) * scale)


# ── Hub connection ─────────────────────────────────────────────────────────

def connect_hub():
    print(f"Searching for '{HUB_NAME}'…  (press green button on hub, LED should blink)")
    try:
        driver = BleakDriver(hub_name=HUB_NAME)
        hub = Hub(connection=driver)
    except Exception as e:
        print(f"Connection failed: {e}")
        print("Tip: try changing HUB_NAME at the top of the script")
        return None, [],

    print("Connected! Waiting for motors to attach…")
    for _ in range(100):  # up to 10 s
        motors = [p for p in hub.peripherals.values() if isinstance(p, Motor)]
        if motors:
            break
        time.sleep(0.1)

    motors = [p for p in hub.peripherals.values() if isinstance(p, Motor)]
    if not motors:
        print("No motors found — check that motors are plugged into the hub.")
        print(f"Peripherals detected: {hub.peripherals}")
        return hub, []

    for i, m in enumerate(motors):
        print(f"  Motor {chr(65+i)} ready")
    return hub, motors


# ── Main loop ──────────────────────────────────────────────────────────────

def run():
    hub, motors = connect_hub()
    if not motors:
        return

    # Assign each motor a signal: even index → bass, odd index → mid
    motor_signals = ["bass" if i % 2 == 0 else "mid" for i in range(len(motors))]
    labels = [chr(65 + i) for i in range(len(motors))]
    print(f"  Driving {len(motors)} motors: " + ", ".join(
        f"{l}={s}" for l, s in zip(labels, motor_signals)
    ))

    # Start ESC listener
    threading.Thread(target=_keyboard_thread, daemon=True).start()

    smooth_bass = 0.0
    smooth_mid  = 0.0
    recent_energies = []
    beat_hold = 0

    def audio_callback(indata, frames, time_info, status):
        nonlocal smooth_bass, smooth_mid, beat_hold, recent_energies

        if _stop.is_set():
            return

        mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
        windowed = mono * np.hanning(len(mono))
        fft  = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(mono), d=1.0 / SAMPLE_RATE)

        bass  = _band_energy(fft, freqs, *BASS_RANGE)
        mid   = _band_energy(fft, freqs, *MID_RANGE)
        total = _band_energy(fft, freqs, 20, 20000)

        # Beat onset detection
        recent_energies.append(total)
        if len(recent_energies) > 20:
            recent_energies.pop(0)
        avg = np.mean(recent_energies) if recent_energies else total
        is_beat = (total > avg * BEAT_THRESHOLD) and (total > BASS_FLOOR * 2)

        # Smooth + map to power.
        # If the raw frame is silent, snap smoothing to zero immediately
        # so motors stop without the slow exponential decay tail.
        if bass < BASS_FLOOR and mid < BASS_FLOOR * 0.5 and beat_hold == 0:
            smooth_bass = 0.0
            smooth_mid  = 0.0
        else:
            smooth_bass = SMOOTHING * smooth_bass + (1 - SMOOTHING) * bass
            smooth_mid  = SMOOTHING * smooth_mid  + (1 - SMOOTHING) * mid

        power_bass = _energy_to_power(smooth_bass, BASS_FLOOR)
        power_mid  = _energy_to_power(smooth_mid,  BASS_FLOOR * 0.5)

        if is_beat:
            beat_hold = beat_hold if beat_hold > BEAT_HOLD_FRAMES else BEAT_HOLD_FRAMES
        if beat_hold > 0:
            power_bass = min(1.0, power_bass + 0.4)
            beat_hold -= 1

        powers = [power_bass if sig == "bass" else power_mid for sig in motor_signals]

        # Send to all motors (start_speed is non-blocking)
        for motor, pwr in zip(motors, powers):
            try:
                motor.start_speed(pwr * MOTOR_MAX)
            except Exception:
                pass

        status_str = "  ".join(f"{l}={p:4.0%}" for l, p in zip(labels, powers))
        if is_beat:
            print(f"♩ BEAT  │ {status_str}")
        else:
            print(f"        │ {status_str}", end="\r")

    print("Listening… (ESC to stop)")
    try:
        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            blocksize=CHUNK_SIZE,
            callback=audio_callback,
        ):
            _stop.wait()
    except KeyboardInterrupt:
        _stop.set()

    # Clean stop
    for motor in motors:
        try:
            motor.start_speed(0)
        except Exception:
            pass
    if hub:
        hub.connection.disconnect()
    print("Done.")


if __name__ == "__main__":
    run()

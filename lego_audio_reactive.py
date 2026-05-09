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
SMOOTHING     = 0.7         # attack smoothing: 0=instant, 1=frozen (rise time)
DECAY         = 0.05        # decay smoothing: lower = faster stop after sound ends (0.05 ≈ 2 frames)
MOTOR_MAX     = 0.4         # max motor power (0.0–1.0)
BASS_FLOOR    = 0.01        # silence threshold
BEAT_THRESHOLD = 1.8        # onset energy multiplier to call a beat
BEAT_HOLD_FRAMES = 6        # frames to sustain beat burst
BASS_RANGE    = (40, 200)   # Hz → Motor A speed
MID_RANGE     = (300, 2000) # Hz → Motor B speed

DIRECTION_FLIP_BEATS  = 2   # flip direction every N beats
TEMPO_CHANGE_THRESHOLD = 0.12  # fractional BPM shift that triggers an immediate flip (0.12 = 12%)

# State-switching motor — set to 'A', 'B', 'C', or 'D' (or None to disable).
# This motor uses angled position commands instead of continuous speed.
# It toggles between +STATE_ANGLE and -STATE_ANGLE degrees on every direction flip.
STATE_MOTOR_PORT  = 'D'    # port letter of the gear/clutch/state motor
STATE_ANGLE       = 90     # degrees to rotate each toggle

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
        return None, [], None

    print("Connected! Waiting for motors to attach…")
    for _ in range(100):  # up to 10 s
        motors = [p for p in hub.peripherals.values() if isinstance(p, Motor)]
        if motors:
            break
        time.sleep(0.1)

    # Port number → letter (Technic Hub: ports 0-3 = A-D)
    port_map = {port: chr(65 + port) for port in hub.peripherals}
    state_motor = None
    drive_motors = []

    for port, device in hub.peripherals.items():
        if not isinstance(device, Motor):
            continue
        letter = port_map.get(port, f"?{port}")
        if STATE_MOTOR_PORT and letter == STATE_MOTOR_PORT.upper():
            state_motor = device
            print(f"  Motor {letter} → state switcher (angled control)")
        else:
            drive_motors.append(device)
            print(f"  Motor {letter} → drive (speed control)")

    if not drive_motors and not state_motor:
        print("No motors found — check that motors are plugged into the hub.")
        print(f"Peripherals detected: {hub.peripherals}")
        return hub, [], None

    return hub, drive_motors, state_motor


# ── Main loop ──────────────────────────────────────────────────────────────

def _toggle_state_motor(motor, state_pos):
    """Move state motor to +STATE_ANGLE or -STATE_ANGLE, non-blocking via thread."""
    angle = STATE_ANGLE if state_pos[0] == 1 else -STATE_ANGLE
    state_pos[0] *= -1  # flip for next call
    threading.Thread(
        target=lambda: motor.angled(angle, speed_primary=0.3),
        daemon=True,
    ).start()


def run():
    hub, motors, state_motor = connect_hub()
    if not motors and not state_motor:
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
    recent_energies  = []
    beat_hold        = 0
    direction        = 1       # +1 or -1, applied to all motors
    beat_count       = 0       # beats since last direction flip
    last_beat_time   = None
    beat_intervals   = []      # rolling window of inter-beat intervals for BPM tracking
    state_pos        = [1]     # mutable so _toggle_state_motor can flip it (1 or -1)

    def audio_callback(indata, frames, time_info, status):
        nonlocal smooth_bass, smooth_mid, beat_hold, recent_energies
        nonlocal direction, beat_count, last_beat_time, beat_intervals

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
        # Attack (rising): use SMOOTHING. Decay (falling): use DECAY for fast stop.
        if bass < BASS_FLOOR and mid < BASS_FLOOR * 0.5 and beat_hold == 0:
            smooth_bass = 0.0
            smooth_mid  = 0.0
        else:
            alpha_bass = SMOOTHING if bass >= smooth_bass else DECAY
            alpha_mid  = SMOOTHING if mid  >= smooth_mid  else DECAY
            smooth_bass = alpha_bass * smooth_bass + (1 - alpha_bass) * bass
            smooth_mid  = alpha_mid  * smooth_mid  + (1 - alpha_mid)  * mid

        power_bass = _energy_to_power(smooth_bass, BASS_FLOOR)
        power_mid  = _energy_to_power(smooth_mid,  BASS_FLOOR * 0.5)

        if is_beat:
            beat_hold = beat_hold if beat_hold > BEAT_HOLD_FRAMES else BEAT_HOLD_FRAMES

            # ── Direction logic ───────────────────────────────────────────
            now = time.time()
            if last_beat_time is not None:
                interval = now - last_beat_time
                if 0.2 < interval < 3.0:  # ignore spurious gaps
                    beat_intervals.append(interval)
                    if len(beat_intervals) > 8:
                        beat_intervals.pop(0)

                    # Detect sudden tempo shift vs. rolling average
                    if len(beat_intervals) >= 3:
                        avg_interval = np.mean(beat_intervals[:-1])
                        tempo_shift = abs(interval - avg_interval) / avg_interval
                        if tempo_shift > TEMPO_CHANGE_THRESHOLD:
                            direction *= -1
                            beat_count = 0  # reset periodic counter too
                            if state_motor:
                                _toggle_state_motor(state_motor, state_pos)

            last_beat_time = now
            beat_count += 1
            if beat_count >= DIRECTION_FLIP_BEATS:
                direction *= -1
                beat_count = 0
                if state_motor:
                    _toggle_state_motor(state_motor, state_pos)
            # ─────────────────────────────────────────────────────────────

        if beat_hold > 0:
            power_bass = min(1.0, power_bass + 0.4)
            beat_hold -= 1

        powers = [power_bass if sig == "bass" else power_mid for sig in motor_signals]

        # Send to all motors (start_speed is non-blocking, sign = direction)
        for motor, pwr in zip(motors, powers):
            try:
                motor.start_speed(pwr * MOTOR_MAX * direction)
            except Exception:
                pass

        dir_sym = "▶" if direction == 1 else "◀"
        status_str = "  ".join(f"{l}={p:4.0%}" for l, p in zip(labels, powers))
        if is_beat:
            print(f"♩ BEAT {dir_sym} │ {status_str}")
        else:
            print(f"       {dir_sym} │ {status_str}", end="\r")

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

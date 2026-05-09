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
BEAT_THRESHOLD = 1.8        # onset energy multiplier to call a beat
BEAT_HOLD_FRAMES = 6        # frames to sustain beat burst
BASS_RANGE    = (40, 200)     # Hz — kick, bass guitar
MID_RANGE     = (300, 2000)   # Hz — snare, chords, vocals
MELODY_RANGE  = (1000, 8000)  # Hz — lead melody, synth, high strings

# Per-band noise floor and amplification scale.
# Raise BASS_FLOOR if motor A runs without sound (ambient noise / motor vibration pickup).
# Lower MELODY_FLOOR / raise MELODY_SCALE if motor D doesn't respond.
BASS_FLOOR    = 0.04   # ambient noise gate for bass — raise if A runs in silence
MID_FLOOR     = 0.02   # ambient noise gate for mid
MELODY_FLOOR  = 0.005  # melody has much lower raw energy — needs a lower threshold
BASS_SCALE    = 3.0    # amplification after the floor
MID_SCALE     = 3.0
MELODY_SCALE  = 10.0   # boost melody signal to compensate for lower HF energy

# Per-motor signal override. Keys are port letters; values are "bass", "mid", or "melody".
# Motors not listed fall back to the default even/odd bass-mid alternation.
MOTOR_SIGNAL  = {'D': 'melody'}

DIRECTION_FLIP_BEATS  = 2   # flip direction every N beats
TEMPO_CHANGE_THRESHOLD = 0.12  # fractional BPM shift that triggers an immediate flip (0.12 = 12%)

# State-switching motor — set to 'A', 'B', 'C', or 'D' (or None to disable).
# This motor uses angled position commands instead of continuous speed.
# It toggles between +STATE_ANGLE and -STATE_ANGLE degrees on every direction flip.
STATE_MOTOR_PORT  = 'C'    # port letter of the gear/clutch/state motor
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

    # Print raw port assignments so we can verify the A-D mapping
    print("  Raw port map:")
    motor_ports = sorted(p for p, d in hub.peripherals.items() if isinstance(d, Motor))
    for i, port in enumerate(motor_ports):
        print(f"    port {port} → letter {chr(65 + i)} (device: {type(hub.peripherals[port]).__name__})")

    # Map port number → letter by sorted order (first motor port = A, second = B, …)
    port_letter = {port: chr(65 + i) for i, port in enumerate(motor_ports)}
    state_motor = None
    drive_motors = []

    for port in motor_ports:
        device = hub.peripherals[port]
        letter = port_letter[port]
        if STATE_MOTOR_PORT and letter == STATE_MOTOR_PORT.upper():
            state_motor = device
            print(f"  Motor {letter} (port {port}) → state switcher (timed control)")
        else:
            drive_motors.append((letter, device))  # keep letter alongside motor
            print(f"  Motor {letter} (port {port}) → drive (speed control)")

    if not drive_motors and not state_motor:
        print("No motors found — check that motors are plugged into the hub.")
        print(f"Peripherals detected: {hub.peripherals}")
        return hub, [], None

    return hub, drive_motors, state_motor


# ── Main loop ──────────────────────────────────────────────────────────────

def _toggle_state_motor(motor, state_pos):
    """Toggle state motor via start_power() + sleep + stop — works on all motor types."""
    power = 0.4 * state_pos[0]   # alternates +0.4 / -0.4
    state_pos[0] *= -1

    def _run():
        try:
            motor.start_power(power)
            time.sleep(0.4)
            motor.start_power(0)
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def run():
    hub, motors, state_motor = connect_hub()
    if not motors and not state_motor:
        return

    # Unpack real port letters kept alongside each motor
    labels  = [l for l, _ in motors]
    devices = [m for _, m in motors]
    # Default: even index → bass, odd index → mid; MOTOR_SIGNAL overrides per letter
    motor_signals = [
        MOTOR_SIGNAL.get(l, "bass" if i % 2 == 0 else "mid")
        for i, l in enumerate(labels)
    ]
    print(f"  Driving {len(devices)} motors: " + ", ".join(
        f"{l}={s}" for l, s in zip(labels, motor_signals)
    ))

    # Start ESC listener
    threading.Thread(target=_keyboard_thread, daemon=True).start()

    smooth_bass   = 0.0
    smooth_mid    = 0.0
    smooth_melody = 0.0
    recent_energies  = []
    beat_hold        = 0
    direction        = 1       # +1 or -1, applied to all motors
    beat_count       = 0       # beats since last direction flip
    last_beat_time   = None
    beat_intervals   = []      # rolling window of inter-beat intervals for BPM tracking
    state_pos        = [1]     # mutable so _toggle_state_motor can flip it (1 or -1)

    def audio_callback(indata, frames, time_info, status):
        nonlocal smooth_bass, smooth_mid, smooth_melody, beat_hold, recent_energies
        nonlocal direction, beat_count, last_beat_time, beat_intervals

        if _stop.is_set():
            return

        mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
        windowed = mono * np.hanning(len(mono))
        fft  = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(mono), d=1.0 / SAMPLE_RATE)

        bass   = _band_energy(fft, freqs, *BASS_RANGE)
        mid    = _band_energy(fft, freqs, *MID_RANGE)
        melody = _band_energy(fft, freqs, *MELODY_RANGE)
        total  = _band_energy(fft, freqs, 20, 20000)

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
        if bass < BASS_FLOOR and mid < MID_FLOOR and melody < MELODY_FLOOR and beat_hold == 0:
            smooth_bass   = 0.0
            smooth_mid    = 0.0
            smooth_melody = 0.0
        else:
            alpha_bass   = SMOOTHING if bass   >= smooth_bass   else DECAY
            alpha_mid    = SMOOTHING if mid    >= smooth_mid    else DECAY
            alpha_melody = SMOOTHING if melody >= smooth_melody else DECAY
            smooth_bass   = alpha_bass   * smooth_bass   + (1 - alpha_bass)   * bass
            smooth_mid    = alpha_mid    * smooth_mid    + (1 - alpha_mid)    * mid
            smooth_melody = alpha_melody * smooth_melody + (1 - alpha_melody) * melody

        power_bass   = _energy_to_power(smooth_bass,   BASS_FLOOR,   BASS_SCALE)
        power_mid    = _energy_to_power(smooth_mid,    MID_FLOOR,    MID_SCALE)
        power_melody = _energy_to_power(smooth_melody, MELODY_FLOOR, MELODY_SCALE)

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

        sig_map = {"bass": power_bass, "mid": power_mid, "melody": power_melody}
        powers = [sig_map[sig] for sig in motor_signals]

        # Send to all motors (start_speed is non-blocking, sign = direction)
        for motor, pwr in zip(devices, powers):
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

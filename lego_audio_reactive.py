#!/usr/bin/env python3
"""
LEGO Technic Audio-Reactive Motor Controller
Motors respond in real time to music/sound captured from the microphone.

Usage:
    python3 lego_audio_reactive.py

Controls:
    Ctrl+C to stop
"""

import asyncio
import numpy as np
import sounddevice as sd
from pylgbst.hub import SmartHub
from pylgbst.peripherals import EncodedMotor
from pylgbst.comms.cbleak import BleakDriver

# ── Tuning parameters ──────────────────────────────────────────────────────
SAMPLE_RATE = 44100
CHUNK_SIZE = 2048        # ~46ms per frame at 44100Hz
SMOOTHING = 0.3          # 0=instant, 1=frozen — higher = smoother motor response
MOTOR_MAX = 80           # max motor power (0–100); keep <100 for safety
BASS_FLOOR = 0.01        # ignore bass below this amplitude (silence threshold)
BEAT_THRESHOLD = 1.8     # onset energy must be this x average to trigger beat burst
BEAT_HOLD_FRAMES = 6     # frames to hold burst after a beat

# Frequency band ranges (Hz) — maps to motor behaviors
BASS_RANGE = (40, 200)       # kick drum, bass → Motor A speed
MID_RANGE = (300, 2000)      # snare, vocals → Motor B speed
HIGH_RANGE = (4000, 12000)   # hi-hats, cymbals → direction modifier
# ──────────────────────────────────────────────────────────────────────────


def band_energy(fft_magnitudes, freqs, low_hz, high_hz):
    """RMS energy within a frequency band."""
    mask = (freqs >= low_hz) & (freqs <= high_hz)
    band = fft_magnitudes[mask]
    return float(np.sqrt(np.mean(band ** 2))) if len(band) > 0 else 0.0


def energy_to_power(energy, floor, scale=4.0):
    """Map raw energy to 0–1 motor power, ignoring noise below floor."""
    if energy < floor:
        return 0.0
    return min(1.0, (energy - floor) * scale)


class AudioReactiveController:
    def __init__(self):
        self.motor_a = None
        self.motor_b = None
        self.smooth_bass = 0.0
        self.smooth_mid = 0.0
        self.recent_energies = []   # for beat onset detection
        self.beat_hold = 0
        self.running = False
        self.audio_queue = asyncio.Queue(maxsize=8)

    def audio_callback(self, indata, frames, time, status):
        """Called by sounddevice on each audio chunk — runs in a separate thread."""
        if self.running:
            mono = indata[:, 0] if indata.ndim > 1 else indata.flatten()
            try:
                self.audio_queue.put_nowait(mono.copy())
            except asyncio.QueueFull:
                pass  # drop frame rather than block audio thread

    def analyse_chunk(self, chunk):
        """Returns (bass_power, mid_power, is_beat) for one audio frame."""
        windowed = chunk * np.hanning(len(chunk))
        fft = np.abs(np.fft.rfft(windowed))
        freqs = np.fft.rfftfreq(len(chunk), d=1.0 / SAMPLE_RATE)

        bass = band_energy(fft, freqs, *BASS_RANGE)
        mid = band_energy(fft, freqs, *MID_RANGE)
        total = band_energy(fft, freqs, 20, 20000)

        # Beat onset: total energy significantly above recent average
        self.recent_energies.append(total)
        if len(self.recent_energies) > 20:
            self.recent_energies.pop(0)
        avg = np.mean(self.recent_energies) if self.recent_energies else total
        is_beat = (total > avg * BEAT_THRESHOLD) and (total > BASS_FLOOR * 2)

        return bass, mid, is_beat

    def compute_motor_commands(self, bass, mid, is_beat):
        """Smooth energies and map to motor power values (-1.0 to 1.0)."""
        # Exponential moving average smoothing
        self.smooth_bass = SMOOTHING * self.smooth_bass + (1 - SMOOTHING) * bass
        self.smooth_mid = SMOOTHING * self.smooth_mid + (1 - SMOOTHING) * mid

        power_a = energy_to_power(self.smooth_bass, BASS_FLOOR)
        power_b = energy_to_power(self.smooth_mid, BASS_FLOOR * 0.5)

        # Beat burst: briefly push motor A to full
        if is_beat:
            self.beat_hold = BEAT_HOLD_FRAMES
        if self.beat_hold > 0:
            power_a = min(1.0, power_a + 0.4)
            self.beat_hold -= 1

        return power_a, power_b

    async def run_motor(self, motor, power_float):
        """Set motor speed, clamped to MOTOR_MAX."""
        if motor is None:
            return
        pct = int(power_float * MOTOR_MAX)
        try:
            await asyncio.to_thread(motor.timed, 0.1, pct / 100.0)
        except Exception:
            pass  # hub disconnected or motor stalled

    async def process_audio(self):
        """Main async loop: drain the audio queue and drive motors."""
        print("Listening… make some noise! (Ctrl+C to stop)")
        while self.running:
            try:
                chunk = await asyncio.wait_for(self.audio_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            bass, mid, is_beat = self.analyse_chunk(chunk)
            power_a, power_b = self.compute_motor_commands(bass, mid, is_beat)

            if is_beat:
                print(f"♩ BEAT  │ A={power_a:.0%}  B={power_b:.0%}")
            else:
                print(f"       │ A={power_a:.0%}  B={power_b:.0%}", end="\r")

            await asyncio.gather(
                self.run_motor(self.motor_a, power_a),
                self.run_motor(self.motor_b, power_b),
            )

    async def connect_hub(self):
        """Connect to the LEGO hub and discover motors."""
        print("Connecting to LEGO hub (make sure it's powered on)…")
        hub = SmartHub(connection=BleakDriver())
        await asyncio.sleep(2)  # give BLE time to enumerate devices

        # Assign motors from whatever ports are active
        for port_id, device in hub.peripherals.items():
            if isinstance(device, EncodedMotor):
                if self.motor_a is None:
                    self.motor_a = device
                    print(f"  Motor A → port {port_id}")
                elif self.motor_b is None:
                    self.motor_b = device
                    print(f"  Motor B → port {port_id}")

        if self.motor_a is None:
            print("  No motors found — check hub connections and try again.")
        return hub

    async def main(self):
        hub = await self.connect_hub()
        self.running = True

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            blocksize=CHUNK_SIZE,
            callback=self.audio_callback,
        ):
            try:
                await self.process_audio()
            except KeyboardInterrupt:
                print("\nStopping…")
            finally:
                self.running = False
                # Stop motors cleanly
                for motor in [self.motor_a, self.motor_b]:
                    if motor:
                        try:
                            motor.stop()
                        except Exception:
                            pass
                hub.disconnect()
                print("Disconnected.")


if __name__ == "__main__":
    controller = AudioReactiveController()
    asyncio.run(controller.main())

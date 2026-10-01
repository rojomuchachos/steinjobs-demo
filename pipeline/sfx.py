"""Offline-rendered slam samples — the synth's realism ceiling was the problem.

The in-page WebAudio chain built tones from raw oscillators, and raw saws
read as MIDI no matter how they're clipped. What reads as a *guitar* is a
plucked-string physical model: Karplus-Strong — a noise burst circulating
through a damped delay line — picks up the inharmonic attack and decay of a
real string for free. Rendered here once in pure stdlib Python (no numpy in
this repo, by design) into short WAVs the page fetches and plays.

`ensure()` writes data/sfx/*.wav if missing; the app serves them at /sfx/.
Pitch variation happens in the browser via playbackRate, so one chug sample
covers every note and the slam pitch-bomb is a rate ramp.
"""

from __future__ import annotations

import math
import random
import struct
import wave
from pathlib import Path

SR = 44100
DIR = Path(__file__).resolve().parent.parent / "data" / "sfx"
NAMES = ("chug", "kick", "snare", "crash")


def _biquad_lp(xs: list[float], f0: float, q: float = 0.8) -> list[float]:
    """RBJ lowpass — the 'cabinet' that keeps distortion from fizzing."""
    w0 = 2 * math.pi * f0 / SR
    alpha = math.sin(w0) / (2 * q)
    cw = math.cos(w0)
    b0, b1, b2 = (1 - cw) / 2, 1 - cw, (1 - cw) / 2
    a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0
    out, x1 = [0.0] * len(xs), 0.0
    x2 = y1 = y2 = 0.0
    for i, x in enumerate(xs):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1, y2, y1 = x1, x, y1, y
        out[i] = y
    return out


def _hp1(xs: list[float], f0: float) -> list[float]:
    """One-pole highpass, good enough for cymbal bodies."""
    rc = 1 / (2 * math.pi * f0)
    dt = 1 / SR
    a = rc / (rc + dt)
    out, prev_x, prev_y = [0.0] * len(xs), 0.0, 0.0
    for i, x in enumerate(xs):
        y = a * (prev_y + x - prev_x)
        prev_x, prev_y = x, y
        out[i] = y
    return out


def _ks_string(f: float, dur: float, damp: float = 0.994) -> list[float]:
    """Karplus-Strong pluck: noise burst around a damped delay loop."""
    n = max(2, int(SR / f))
    rng = random.Random(2026)  # deterministic — same file every build
    buf = [rng.uniform(-1, 1) for _ in range(n)]
    total = int(SR * dur)
    out = [0.0] * total
    for i in range(total):
        j = i % n
        out[i] = buf[j]
        buf[j] = damp * 0.5 * (buf[j] + buf[(j + 1) % n])
    return out


def _chug() -> list[float]:
    # two detuned strings, palm-mute damping, hard clip, cab, gate shut
    dur = 0.42
    a = _ks_string(55.0, dur, damp=0.990)
    b = _ks_string(55.55, dur, damp=0.989)
    mix = [(x + y) * 0.9 for x, y in zip(a, b)]
    driven = [math.tanh(9 * x) for x in mix]
    cab = _biquad_lp(driven, 3400, 0.9)
    out = []
    for i, x in enumerate(cab):
        t = i / SR
        env = 1.0 if t < 0.16 else max(0.0, 1 - (t - 0.16) / 0.2)  # the mute
        out.append(x * env * 0.85)
    return out


def _kick() -> list[float]:
    dur, out = 0.16, []
    phase = 0.0
    rng = random.Random(7)
    for i in range(int(SR * dur)):
        t = i / SR
        f = 35 + 125 * math.exp(-t * 38)          # 160 → 35 sweep
        phase += 2 * math.pi * f / SR
        body = math.sin(phase) * math.exp(-t * 22)
        click = rng.uniform(-1, 1) * math.exp(-t * 700) * 0.6
        out.append(math.tanh(2.2 * (body + click)) * 0.95)
    return out


def _snare() -> list[float]:
    dur = 0.16
    rng = random.Random(11)
    noise = [rng.uniform(-1, 1) for _ in range(int(SR * dur))]
    crack = _biquad_lp(_hp1(noise, 1400), 7500, 0.7)
    out, phase = [], 0.0
    for i, x in enumerate(crack):
        t = i / SR
        f = 120 + 70 * math.exp(-t * 40)
        phase += 2 * math.pi * f / SR
        shell = math.sin(phase) * math.exp(-t * 45) * 0.7
        out.append(math.tanh(1.6 * (x * math.exp(-t * 26) + shell)) * 0.8)
    return out


def _crash() -> list[float]:
    dur = 1.0
    rng = random.Random(13)
    noise = [rng.uniform(-1, 1) for _ in range(int(SR * dur))]
    body = _hp1(_hp1(noise, 3800), 3800)
    out = []
    for i, x in enumerate(body):
        t = i / SR
        shimmer = 1 + 0.25 * math.sin(2 * math.pi * 9 * t)
        out.append(x * math.exp(-t * 4.2) * shimmer * 0.55)
    return out


def _write(path: Path, samples: list[float]) -> None:
    frames = b"".join(
        struct.pack("<h", max(-32767, min(32767, int(s * 32767)))) for s in samples
    )
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(frames)


def ensure() -> Path:
    """Render any missing sample. Cheap no-op once the files exist."""
    DIR.mkdir(parents=True, exist_ok=True)
    makers = {"chug": _chug, "kick": _kick, "snare": _snare, "crash": _crash}
    for name, fn in makers.items():
        p = DIR / f"{name}.wav"
        if not p.exists():
            _write(p, fn())
    return DIR

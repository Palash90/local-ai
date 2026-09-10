"""Minimal stdlib MIDI writer (Type-0, one merged track). No external deps.

Each parsed lane gets its own MIDI channel; a drums lane is forced onto channel
9 (GM percussion). Lane volume is emitted as CC7 so instruments are mixed
independently, and note velocity carries a light performance curve (downbeat
accent, softer off-beats and chord interiors).
"""

import struct

DRUM_CHANNEL = 9


def _varlen(n):
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


def _assign_channels(sections):
    """Give each lane a distinct channel; drums -> 9, others skip 9."""
    chans, next_ch = {}, 0
    for si, sec in enumerate(sections):
        if sec.get("drum"):
            chans[si] = DRUM_CHANNEL
        else:
            while next_ch == DRUM_CHANNEL:
                next_ch += 1
            chans[si] = next_ch % 16
            next_ch += 1
    return chans


def _energy_vel(vel, energy):
    """Scale a base velocity by the song-form energy of the bar (0..1)."""
    if energy is None:
        return vel
    e = max(0.0, min(1.0, energy))
    return int(round(vel * (0.55 + 0.45 * e)))


def build_midi(sections, tempo=120, ppq=480, tail_beats=3):
    us_per_q = int(60_000_000 / tempo)
    chans = _assign_channels(sections)
    evs = []  # (tick, priority, kind, data)
    max_tick = 0
    for si, sec in enumerate(sections):
        ch = chans[si]
        vol = max(0, min(127, int(round(sec.get("vol", 100) * 127 / 100))))
        evs.append((0, 0, "chvol", (ch, vol)))
        evs.append((0, 0, "cc11", (ch, 127)))          # expression (set by energy)
        if not sec.get("drum"):
            evs.append((0, 1, "prog", (ch, sec.get("program", 0))))
        drum = sec.get("drum")
        prev = None
        for e in sec["events"]:
            start = int(e["start"] * ppq)
            dur = max(1, int(e["dur"] * ppq))
            energy = e.get("energy")
            # apply the bar's energy as expression on the channel at onset
            if energy is not None and not drum:
                evs.append((start, 0, "cc11", (ch, max(0, min(127, int(round(energy * 127)))))))
            mids = e.get("pitches") or ([e["midi"]] if "midi" in e else [])
            beat = e["start"]
            if abs(beat - round(beat)) < 0.01 and int(round(beat)) % 4 == 0:
                vel = 108
            elif abs(beat - round(beat)) < 0.01:
                vel = 95
            else:
                vel = 82
            if e.get("vel") is not None:
                vel = e["vel"]
            else:
                vel = _energy_vel(vel, energy)
            # legato: let melodic notes breathe (release just before next onset)
            for ni, p in enumerate(mids):
                v = vel if ni == 0 else max(55, vel - 12 * ni)
                if e["type"] == "chord":
                    v = max(55, v - 8)
                v = max(1, min(127, v))
                off = start + dur
                if not drum and dur > ppq // 2:
                    off = start + int(dur * 0.92)      # articulation gap
                if drum:
                    off = start + min(dur, int(0.12 * ppq))
                evs.append((start, 3, "on", (ch, p, v)))
                evs.append((off, 2, "off", (ch, p, 0)))
                max_tick = max(max_tick, start, off)
    # A silent late control event extends the render so the last chord's
    # release + hall reverb rings out instead of being chopped abruptly.
    tail_tick = max_tick + int(tail_beats * ppq)
    evs.append((tail_tick, 0, "cc11", (0, 127)))
    evs.sort(key=lambda x: (x[0], x[1]))
    track = b"\x00\xff\x51\x03" + struct.pack(">I", us_per_q)[1:]
    last = 0
    for tick, _prio, kind, d in evs:
        track += _varlen(max(0, tick - last))
        last = tick
        if kind == "chvol":
            track += bytes((0xB0 | d[0], 7, d[1]))
        elif kind == "cc11":
            track += bytes((0xB0 | d[0], 11, d[1]))
        elif kind == "prog":
            track += bytes((0xC0 | d[0], d[1]))
        elif kind == "on":
            track += bytes((0x90 | d[0], d[1], d[2]))
        else:
            track += bytes((0x80 | d[0], d[1], d[2]))
    track += b"\x00\xff\x2f\x00"
    return (b"MThd\x00\x00\x00\x06\x00\x00\x00\x01"
            + struct.pack(">H", ppq) + b"MTrk" + struct.pack(">I", len(track)) + track)

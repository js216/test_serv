# SPDX-License-Identifier: MIT
# scope.py --- Oscilloscope trace capture via VISA
# Copyright (c) 2026 Jakob Kastelic

import csv
import io

import config
from plugin import DevicePlugin, Op


def _lazy_pyvisa():
    import pyvisa
    return pyvisa


def _lazy_numpy():
    import numpy as np
    return np


def _parse_val(s):
    return float(''.join(c for c in s if (c.isdigit() or c in ".-+eE")))


def _read_channel(inst, ch):
    np = _lazy_numpy()
    inst.write(f"WAV:SOUR {ch}")
    raw = inst.query_binary_values(
        "WAV:DATA?", datatype="B", container=bytes)
    wave = np.frombuffer(raw, dtype=np.uint8)
    vdiv = _parse_val(inst.query(f"{ch}:VDIV?"))
    ofst = _parse_val(inst.query(f"{ch}:OFST?"))
    sara = _parse_val(inst.query("SARA?"))
    v = (wave - 128) * (vdiv / 25.0) - ofst
    t = np.arange(len(v)) / sara
    return t, v


def _traces_to_csv(traces, max_points=5000):
    first = next(iter(traces))
    t = traces[first][0]
    chans = list(traces.keys())
    vs = [traces[c][1] for c in chans]
    n = min([len(t)] + [len(v) for v in vs])
    step = max(1, n // max_points)
    t = t[::step]
    vs = [v[::step] for v in vs]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["t"] + chans)
    for i in range(len(t)):
        w.writerow([t[i]] + [vs[j][i] for j in range(len(chans))])
    return buf.getvalue()


class ScopeHandle:
    def __init__(self, resource):
        self.resource = resource
        self._rm = None
        self._inst = None

    def open(self):
        pyvisa = _lazy_pyvisa()
        self._rm = pyvisa.ResourceManager()
        self._inst = self._rm.open_resource(self.resource)

    def close(self):
        try:
            if self._inst is not None:
                self._inst.close()
        except Exception:
            pass
        try:
            if self._rm is not None:
                self._rm.close()
        except Exception:
            pass
        self._inst = None
        self._rm = None


def _summarize_traces(traces, signal_cfg):
    """For each channel that has a config entry, count active-going
    and inactive-going transitions and compute active-duty fraction.

    signal_cfg is {channel: {name, active_below}}. A sample is
    'active' when its voltage is below active_below. Returns a list
    of dicts ready for logging.
    """
    out = []
    for ch, cfg in sorted(signal_cfg.items()):
        if ch not in traces:
            continue
        _t, v = traces[ch]
        thresh = float(cfg["active_below"])
        name = cfg.get("name", ch)
        n_total = 0
        n_active = 0
        enter = 0
        leave = 0
        prev = None
        for sample in v:
            active = float(sample) < thresh
            if prev is not None and active != prev:
                if active:
                    enter += 1
                else:
                    leave += 1
            prev = active
            n_total += 1
            if active:
                n_active += 1
        duty = (100.0 * n_active / n_total) if n_total else 0.0
        out.append({
            "chan": ch,
            "name": name,
            "went_active": enter,
            "went_inactive": leave,
            "duty_pct": duty,
            "samples": n_total,
        })
    return out


def _op_capture(session, h, args):
    chans_raw = args["chans"]
    chans = tuple(c for c in chans_raw.split(",") if c)
    h._inst.write("STOP")
    h._inst.write("WAV:FORM BYTE")
    h._inst.write("WAV:MODE RAW")
    h._inst.write("WAV:POIN MAX")
    traces = {}
    for ch in chans:
        traces[ch] = _read_channel(h._inst, ch)
    h._inst.write("RUN")
    h._inst.write("TRIG:MODE AUTO")
    csv_text = _traces_to_csv(traces)
    session.stream("scope.csv").append(csv_text.encode())

    # Signal-level analysis per config.json scope.signals table.
    signal_cfg = config.section("scope").get("signals") or {}
    if signal_cfg:
        summary = _summarize_traces(traces, signal_cfg)
        for row in summary:
            session.log_event(
                "SCOPE", "scope:capture",
                f"{row['chan']} {row['name']}: "
                f"went_active={row['went_active']} "
                f"went_inactive={row['went_inactive']}  "
                f"duty={row['duty_pct']:5.1f}% "
                f"samples={row['samples']}")
        # Machine-readable copy in a stream for offline analysis.
        import json
        session.stream("scope.summary").append(
            (json.dumps(summary, indent=2) + "\n").encode())


class ScopePlugin(DevicePlugin):
    name = "scope"
    doc = "Oscilloscope (VISA): trigger + capture a set of channels."

    ops = {
        "capture": Op(
            args={"chans": "str"},
            doc=("Capture listed channels (e.g. chans=\"C1,C2\") as "
                 "CSV into scope.csv stream. If config.json has a "
                 "scope.signals table mapping channel -> {name, "
                 "active_below}, scope:capture also logs per-channel "
                 "transition counts + active duty in timeline.log "
                 "(e.g. C2 DSP_FAULT: went_active=0 ...) and writes "
                 "the full summary as JSON to the scope.summary "
                 "stream. GET /scope/signals returns the current map."),
            run=_op_capture),
    }

    def probe(self):
        try:
            pyvisa = _lazy_pyvisa()
        except ImportError:
            return []
        try:
            resources = set(pyvisa.ResourceManager().list_resources())
        except Exception:
            return []
        out = []
        for inst in config.instances(self.name):
            res = inst.get("resource")
            if not res or res not in resources:
                continue
            out.append({
                "id": inst.get("id", "0"),
                "resource": res,
                "expected_idn": inst.get("expected_idn"),
            })
        return out

    def open(self, spec):
        h = ScopeHandle(resource=spec["resource"])
        h.open()
        # Verify IDN string if the config pinned one. The scope responds to
        # the standard *IDN? SCPI query with "vendor,model,serial,fw".
        expected = spec.get("expected_idn")
        if expected:
            try:
                idn = h._inst.query("*IDN?").strip()
            except Exception as e:
                h.close()
                raise RuntimeError(f"scope *IDN? failed: {e}")
            if expected not in idn:
                h.close()
                raise RuntimeError(
                    f"scope identity mismatch: expected {expected!r} in "
                    f"*IDN?, got {idn!r}"
                )
            h._identity_verified = True
        return h

    def close(self, handle):
        handle.close()

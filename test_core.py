# SPDX-License-Identifier: MIT
# test_core.py --- Stdlib smoke test: parse, run, artefact shape
# Copyright (c) 2026 Jakob Kastelic

import hashlib
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import tarfile
import threading
import time

import plan
import report
import server
import session as session_module
from plugin import DevicePlugin, Op
from registry import DeviceRegistry
from session import Session, bench_id, pack_artefact


# --- fake plugin ---------------------------------------------------------

def _noop(session, h, args):
    session.stream("fake.log").append(
        f"noop k={args.get('k', '?')}\n".encode())


def _emit(session, h, args):
    session.stream(args["stream"]).append(args["data"].encode())


def _fail(session, h, args):
    raise RuntimeError("intentional failure")


class FakeHandle:
    pass


class FakePlugin(DevicePlugin):
    name = "fake"
    doc = "in-memory device used by the smoke test"
    ops = {
        "noop": Op(args={"k": "int"}, doc="no-op with an int arg",
                   run=_noop),
        "emit": Op(args={"stream": "ident", "data": "str"},
                   doc="append text to the given stream", run=_emit),
        "fail": Op(args={}, doc="raise, should land in errors.log",
                   run=_fail),
    }

    def __init__(self):
        self.opens = 0
        self.closes = 0

    def probe(self):
        return [{"id": "0"}]

    def open(self, spec):
        self.opens += 1
        return FakeHandle()

    def close(self, handle):
        self.closes += 1


# --- tests ---------------------------------------------------------------

def test_parse_basic():
    text = """
    # comment
    fake:noop k=42
    fake:emit stream=fake.log data="hello"
    inventory
    delay ms=1
    mark tag=done
    """
    ops = plan.parse_text(text)
    assert len(ops) == 5, [o.verb for o in ops]
    assert ops[0].device == "fake" and ops[0].verb == "noop"
    assert ops[0].args["k"].as_int() == 42
    assert ops[1].args["data"].as_str() == "hello"
    assert ops[2].verb == "inventory"
    assert ops[3].verb == "delay"
    assert ops[4].verb == "mark"


def test_required_device_refs_preserves_concrete_instances():
    parsed = plan.load_tar(plan.pack_tar(
        "mp135.custom:uart_open\n"
        "msc.custom:write image=@x.bin\n"
        "dsp:reset\n", {"x.bin": b"x"}))
    assert plan.required_devices(parsed) == {"mp135", "msc", "dsp"}
    assert plan.required_device_refs(parsed) == {
        "mp135.custom", "msc.custom", "dsp"}


def test_blob_ref_missing_rejected():
    text = "fake:emit stream=s data=@missing\n"
    # parser doesn't know about blobs yet, so this is a Value("blob",...)
    ops = plan.parse_text(text)
    assert ops[0].args["data"].kind == "blob"
    # load_tar enforces the reference
    buf = plan.pack_tar(text, {})
    try:
        plan.load_tar(buf)
    except plan.PlanError as e:
        assert "@missing" in str(e), e
    else:
        raise AssertionError("expected PlanError for missing blob")


def test_unknown_verb_rejected():
    try:
        plan.parse_text("bogus_verb\n")
    except plan.PlanError as e:
        assert "unknown verb" in str(e), e
    else:
        raise AssertionError("expected PlanError")


def test_pack_and_load_roundtrip():
    text = 'fake:emit stream=s data="hi"\n'
    blobs = {"foo.ldr": b"FFFF\n0001\n"}
    tar_bytes = plan.pack_tar(text, blobs)
    pf = plan.load_tar(tar_bytes)
    assert len(pf.ops) == 1
    assert pf.blobs["foo.ldr"] == b"FFFF\n0001\n"


def test_session_runs_and_artefact_has_expected_shape():
    text = """
    fake:noop k=7
    fake:emit stream=foo data="hello\\n"
    fake:fail
    mark tag=after_fail
    """
    tar_in = plan.pack_tar(text, {})
    parsed = plan.load_tar(tar_in)

    plugins = {"fake": FakePlugin()}
    reg = DeviceRegistry(plugins)
    reg.refresh()

    session = Session(reg, parsed)
    session.run_all(plugins)

    tar_out, manifest_text = pack_artefact(session)
    manifest = json.loads(manifest_text)
    assert manifest["n_ops"] == 4
    assert manifest["n_errors"] >= 1
    # n_errors > 0 must surface as status="errors" -- the dashboard
    # and any aggregator scripting on manifest.status depend on the
    # discriminator; without this assertion a regression that always
    # emits "ok" would slip through silently.
    assert manifest["status"] == "errors", manifest

    tf = tarfile.open(fileobj=io.BytesIO(tar_out), mode="r:")
    members = set(tf.getnames())
    expected = {"manifest.json", "timeline.log", "ops.jsonl",
                "errors.log", "streams/foo.bin",
                "streams/fake.log.bin"}
    missing = expected - members
    assert not missing, f"missing from artefact tar: {missing}"

    ops_jsonl = tf.extractfile("ops.jsonl").read().decode().strip().splitlines()
    recs = [json.loads(l) for l in ops_jsonl]
    statuses = [r["status"] for r in recs]
    assert statuses == ["ok", "ok", "error", "ok"], statuses

    reg.close_all()


def test_session_closes_touched_handles_at_job_end():
    text = 'fake:emit stream=s data="hi"\n'
    parsed = plan.load_tar(plan.pack_tar(text, {}))
    fake = FakePlugin()
    plugins = {"fake": fake}
    reg = DeviceRegistry(plugins)
    reg.refresh()
    key = reg.resolve("fake")

    session = Session(reg, parsed)
    session.run_all(plugins)

    assert fake.opens == 1
    assert fake.closes == 1
    assert key not in reg.cache

    reg.close_all()


def test_session_logs_device_use_and_release_for_jobs_only():
    with tempfile.TemporaryDirectory() as tmp:
        old_log = session_module.DEVICES_LOG
        session_module.DEVICES_LOG = os.path.join(tmp, "devices.log")
        try:
            parsed = plan.load_tar(plan.pack_tar('fake:noop k=1\n', {}))
            plugins = {"fake": FakePlugin()}
            reg = DeviceRegistry(plugins)
            reg.refresh()

            sess = Session(reg, parsed)
            sess.plan_digest = "0" * 64
            sess.run_all(plugins)

            with open(session_module.DEVICES_LOG, encoding="utf-8") as f:
                recs = [json.loads(line) for line in f if line.strip()]
            assert [r["event"] for r in recs] == ["use", "release"], recs
            assert [r["device"] for r in recs] == ["fake.0", "fake.0"], recs
            assert recs[1]["t"] >= recs[0]["t"], recs
            assert recs[0]["session"] == sess.session_id, recs[0]

            reg.close_all()
        finally:
            session_module.DEVICES_LOG = old_log


def test_lease_only_plan_does_not_count_as_device_usage():
    from plugins.lease import LeasePlugin
    with tempfile.TemporaryDirectory() as tmp:
        old_log = session_module.DEVICES_LOG
        session_module.DEVICES_LOG = os.path.join(tmp, "devices.log")
        try:
            parsed = plan.load_tar(plan.pack_tar(
                'lease:claim devices="fake.0" duration_s=60\n', {}))
            plugins = {"fake": FakePlugin(), "lease": LeasePlugin()}
            reg = DeviceRegistry(plugins)
            reg.refresh()

            sess = Session(reg, parsed)
            sess.run_all(plugins)

            assert not os.path.exists(session_module.DEVICES_LOG)
            reg.close_all()
        finally:
            session_module.DEVICES_LOG = old_log


def test_direct_session_without_plan_digest_does_not_log_device_usage():
    with tempfile.TemporaryDirectory() as tmp:
        old_log = session_module.DEVICES_LOG
        session_module.DEVICES_LOG = os.path.join(tmp, "devices.log")
        try:
            parsed = plan.load_tar(plan.pack_tar('fake:noop k=1\n', {}))
            plugins = {"fake": FakePlugin()}
            reg = DeviceRegistry(plugins)
            reg.refresh()

            sess = Session(reg, parsed)
            sess.run_all(plugins)

            assert not os.path.exists(session_module.DEVICES_LOG)
            reg.close_all()
        finally:
            session_module.DEVICES_LOG = old_log


def test_report_computes_duty_from_device_log_events():
    events = [
        {"t": 10.0, "event": "use", "device": "fake.0",
         "session": "s1", "plan_digest": "p1"},
        {"t": 15.0, "event": "release", "device": "fake.0",
         "session": "s1", "plan_digest": "p1"},
        {"t": 20.0, "event": "use", "device": "fake.0",
         "session": "s2", "plan_digest": "p2"},
        {"t": 30.0, "event": "release", "device": "fake.0",
         "session": "s2", "plan_digest": "p2"},
        {"t": 25.0, "event": "use", "device": "dsp.main",
         "session": "s3", "plan_digest": "p3"},
        {"t": 35.0, "event": "release", "device": "dsp.main",
         "session": "s3", "plan_digest": "p3"},
    ]
    summary = report.compute_duty(events)
    assert summary["window_s"] == 25.0, summary
    assert summary["stats"]["fake.0"]["busy_s"] == 15.0, summary
    assert summary["stats"]["fake.0"]["intervals"] == 2, summary
    assert summary["stats"]["dsp.main"]["busy_s"] == 10.0, summary


def test_report_pairs_by_session_for_back_to_back_device_jobs():
    events = [
        {"t": 10.0, "event": "use", "device": "fake.0",
         "session": "old", "plan_digest": "p1"},
        {"t": 20.0, "event": "release", "device": "fake.0",
         "session": "old", "plan_digest": "p1"},
        {"t": 20.0, "event": "use", "device": "fake.0",
         "session": "new", "plan_digest": "p2"},
        {"t": 30.0, "event": "release", "device": "fake.0",
         "session": "new", "plan_digest": "p2"},
    ]
    summary = report.compute_duty(events)
    assert summary["stats"]["fake.0"]["busy_s"] == 20.0, summary
    assert summary["stats"]["fake.0"]["intervals"] == 2, summary


def test_report_closes_stale_session_at_next_device_use():
    events = [
        {"t": 10.0, "event": "use", "device": "fake.0",
         "session": "crashed", "plan_digest": "p1"},
        {"t": 20.0, "event": "use", "device": "fake.0",
         "session": "next", "plan_digest": "p2"},
        {"t": 30.0, "event": "release", "device": "fake.0",
         "session": "next", "plan_digest": "p2"},
    ]
    summary = report.compute_duty(events)
    st = summary["stats"]["fake.0"]
    assert st["busy_s"] == 20.0, summary
    assert st["intervals"] == 2, summary
    assert st["stale_sessions"] == ["crashed"], summary


def test_report_keeps_log_order_for_equal_timestamps():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "devices.log")
        recs = [
            {"t": 20.0, "event": "use", "device": "fake.0",
             "session": "same", "plan_digest": "p1"},
            {"t": 20.0, "event": "release", "device": "fake.0",
             "session": "same", "plan_digest": "p1"},
        ]
        with open(path, "w", encoding="utf-8") as f:
            for rec in recs:
                f.write(json.dumps(rec) + "\n")
        loaded = report.load_events(path)
        assert [r["event"] for r in loaded] == ["use", "release"], loaded
        summary = report.compute_duty(loaded, now=1000.0)
        assert summary["stats"]["fake.0"]["busy_s"] == 0.0, summary
        assert not summary["stats"]["fake.0"]["active"], summary


def test_report_prints_highest_duty_first():
    summary = {
        "start": 10.0,
        "end": 110.0,
        "window_s": 100.0,
        "stats": {
            "low.0": {
                "busy_s": 10.0,
                "intervals": 1,
                "active": [],
                "stale_sessions": [],
            },
            "high.0": {
                "busy_s": 90.0,
                "intervals": 1,
                "active": [],
                "stale_sessions": [],
            },
        },
    }
    old_stdout = sys.stdout
    out = io.StringIO()
    try:
        sys.stdout = out
        report.print_report(summary)
    finally:
        sys.stdout = old_stdout
    lines = out.getvalue().splitlines()
    assert lines[2].startswith("high.0"), lines
    assert lines[3].startswith("low.0"), lines


def test_inventory_returns_devices_and_ops_streams():
    parsed = plan.load_tar(plan.pack_tar("inventory\n", {}))
    fake = FakePlugin()
    plugins = {"fake": fake}
    reg = DeviceRegistry(plugins)
    reg.refresh()

    session = Session(reg, parsed)
    session.run_all(plugins)

    # inventory now stores directly on the session; pack_artefact
    # emits as bench.{devices,ops}.json files in the tar.
    devices = session.bench_devices
    ops = session.bench_ops
    assert devices is not None and ops is not None
    assert devices[0]["id"] == "fake.0"
    assert "mark" in ops["_control"]["ops"]
    assert "fake" in ops
    assert "emit" in ops["fake"]["ops"]

    reg.close_all()


def test_server_rest_queue_helpers():
    with tempfile.TemporaryDirectory() as tmp:
        old_dirs = (
            server.INPUTS, server.OUTPUTS, server.DONE,
            server.STATUS, server.RELEASE, server.SWEEP,
        )
        server.INPUTS = os.path.join(tmp, "inputs")
        server.OUTPUTS = os.path.join(tmp, "outputs")
        server.DONE = os.path.join(tmp, "done")
        server.STATUS = os.path.join(tmp, "status")
        server.RELEASE = os.path.join(tmp, "release")
        server.SWEEP = os.path.join(tmp, "sweep")
        for d in old_dirs:
            assert d
        for d in (server.INPUTS, server.OUTPUTS, server.DONE,
                  server.STATUS, server.RELEASE, server.SWEEP):
            os.makedirs(d, mode=0o700, exist_ok=True)

        try:
            body = plan.pack_tar("mark tag=rest\n", {})
            digest, status = server.queue_job(body, {"runtime": "1"})
            assert status == "queued"
            assert os.path.exists(
                os.path.join(server.INPUTS, f"{digest}.plan"))
            with open(os.path.join(server.INPUTS, f"{digest}.plan.meta")) as f:
                assert f.read() == "runtime=1\n"

            with open(os.path.join(server.OUTPUTS, f"{digest}.txt"),
                      "wb") as f:
                f.write(b'{"status":"ok"}\n')
            assert server.parse_output_name(f"{digest}.txt") == (
                digest, ".txt")
            assert server.delete_outputs(digest) == 1
        finally:
            (server.INPUTS, server.OUTPUTS, server.DONE,
             server.STATUS, server.RELEASE, server.SWEEP) = old_dirs


def test_request_path_strips_query_for_static_assets():
    assert server._request_path("/web/style.css?v=20260505-2") == (
        "web/style.css")
    assert server._request_path("/web/app.js?cache=bust") == "web/app.js"
    assert server._request_path("//[::1") is None


def test_static_assets_accept_query_and_disable_cache():
    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = httpd.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        try:
            conn.request("GET", "/web/style.css?v=20260505-2")
            resp = conn.getresponse()
            body = resp.read()
        finally:
            conn.close()
        assert resp.status == 200, resp.status
        assert resp.getheader("Content-Type").startswith("text/css")
        assert resp.getheader("Cache-Control") == "no-store"
        assert body.startswith(b"/* SPDX-License-Identifier: MIT */")
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_queue_job_rejects_resubmit_while_inflight():
    """Round-9 Q2: queue_job must refuse a digest that's already
    mid-flight (in DONE/.plan or in inflight.json). Without this gate
    a doubled-up submit would dispatch two sessions for the same
    digest; the second one's _active_sessions[digest] = session2 would
    clobber the first, and two real bench runs collapse into one
    observable artefact at upload time.
    """
    with tempfile.TemporaryDirectory() as tmp:
        old_dirs = (server.INPUTS, server.OUTPUTS, server.DONE,
                    server.STATUS, server.RELEASE, server.SWEEP)
        server.INPUTS = os.path.join(tmp, "inputs")
        server.OUTPUTS = os.path.join(tmp, "outputs")
        server.DONE = os.path.join(tmp, "done")
        server.STATUS = os.path.join(tmp, "status")
        server.RELEASE = os.path.join(tmp, "release")
        server.SWEEP = os.path.join(tmp, "sweep")
        for d in (server.INPUTS, server.OUTPUTS, server.DONE,
                  server.STATUS, server.RELEASE, server.SWEEP):
            os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            body = plan.pack_tar("mark tag=q2\n", {})
            digest = hashlib.sha256(body).hexdigest()
            # (1) DONE/.plan present -> "duplicate"
            with open(os.path.join(server.DONE, f"{digest}.plan"),
                      "wb") as f:
                f.write(b"x")
            d2, status = server.queue_job(body)
            assert d2 == digest and status == "duplicate", (d2, status)
            os.unlink(os.path.join(server.DONE, f"{digest}.plan"))

            # (2) inflight.json lists digest -> "duplicate"
            with open(os.path.join(server.STATUS, "inflight.json"),
                      "wb") as f:
                f.write(json.dumps([{"digest": digest}]).encode())
            d2, status = server.queue_job(body)
            assert d2 == digest and status == "duplicate", (d2, status)
            os.unlink(os.path.join(server.STATUS, "inflight.json"))

            # (3) clean state -> queued
            d2, status = server.queue_job(body)
            assert d2 == digest and status == "queued", (d2, status)
        finally:
            (server.INPUTS, server.OUTPUTS, server.DONE,
             server.STATUS, server.RELEASE, server.SWEEP) = old_dirs


def test_lazy_handle_cache_and_release():
    plugins = {"fake": FakePlugin()}
    reg = DeviceRegistry(plugins)
    reg.refresh()
    key = reg.resolve("fake")
    with reg.acquire(key) as h:
        assert h is not None
    # cached after release; refs back to 0.
    assert key in reg.cache
    assert reg.cache[key][1] == 0
    # explicit release closes it.
    ok = reg.release_now(key)
    assert ok
    assert key not in reg.cache
    # acquiring again re-opens.
    with reg.acquire(key):
        assert key in reg.cache
    reg.close_all()


def test_bounded_sizes():
    huge = "fake:noop k=0\n" * 10000   # > MAX_OPS
    try:
        plan.parse_text(huge)
    except plan.PlanError as e:
        assert "too many ops" in str(e), e
    else:
        raise AssertionError("expected PlanError for op count")


def test_stop_session_clean_termination():
    """uart_expect end_session=true -> StopSession -> clean stop, no error.

    Regression: round 2 made signal_early_done raise StopSession;
    round 3 found _run_one's `except Exception` was swallowing it
    and recording a fake error. Run a fake op that signals; check
    that ops_log records ok status and errors[] stays empty.
    """
    def _stop_op(session, h, args):
        session.signal_early_done("test reason")

    class StopPlugin(FakePlugin):
        name = "stop"
        ops = {
            "stop": Op(args={}, doc="signal early-done", run=_stop_op),
            "should_not_run": Op(args={}, doc="must not execute",
                                 run=_fail),
        }

    parsed = plan.load_tar(plan.pack_tar(
        "stop:stop\nstop:should_not_run\n", {}))
    plugins = {"stop": StopPlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()
    session = Session(reg, parsed)
    session.run_all(plugins)
    assert len(session.ops_log) == 1, [r["verb"] for r in session.ops_log]
    assert session.ops_log[0]["status"] == "ok"
    assert not session.errors, session.errors
    reg.close_all()


def test_cancel_propagates_to_session():
    """signal_cancel during a `delay` should wake immediately.

    Regression class: round 1 found cancel_event was missing.
    A 2s delay should abort within ~50ms once signal_cancel fires.
    """
    import threading as _th
    parsed = plan.load_tar(plan.pack_tar("delay ms=2000\n", {}))
    reg = DeviceRegistry({}); reg.refresh()
    session = Session(reg, parsed)
    fired_at = [None]

    def _cancel_soon():
        time.sleep(0.05)
        fired_at[0] = time.monotonic()
        session.signal_cancel()

    _th.Thread(target=_cancel_soon, daemon=True).start()
    t0 = time.monotonic()
    session.run_all({})
    elapsed = time.monotonic() - t0
    assert elapsed < 0.5, f"delay didn't abort on cancel ({elapsed:.2f}s)"
    assert session.canceled
    # Round-10 S3: a canceled run must surface as manifest.status
    # "canceled", not "errors". The README and dashboard both
    # documented this status; before this fix the discriminator only
    # produced ok/inert/errors and a script keying off "canceled"
    # never fired.
    _, mtxt = pack_artefact(session)
    assert json.loads(mtxt)["status"] == "canceled", mtxt
    reg.close_all()


def test_signal_cancel_sigkills_session_subprocs():
    """A DELETE /jobs/<digest> against a session that's running a
    subprocess (cubeprog flash, ssh:exec, ...) should SIGKILL that
    subprocess immediately rather than waiting on the op's
    200ms-poll loop. Without this, the operator sees "delivered
    but not honored" -- the cancel marker arrives, signal_cancel
    runs, but the subprocess keeps going for seconds-to-minutes.
    """
    import subprocess as _sp
    import poller
    parsed = plan.load_tar(plan.pack_tar("delay ms=1\n", {}))
    reg = DeviceRegistry({}); reg.refresh()
    s1 = Session(reg, parsed)
    s2 = Session(reg, parsed)
    # Spawn one subproc owned by each session, plus one unowned.
    p1 = _sp.Popen(["sleep", "60"])
    p2 = _sp.Popen(["sleep", "60"])
    p_other = _sp.Popen(["sleep", "60"])
    poller.register_subprocess(p1, session=s1)
    poller.register_subprocess(p2, session=s2)
    poller.register_subprocess(p_other, session=None)
    try:
        s1.signal_cancel()
        # p1 should die almost immediately; p2 and p_other untouched.
        rc1 = p1.wait(timeout=2.0)
        assert rc1 != 0, "p1 should have been killed by signal_cancel"
        assert p2.poll() is None, (
            "another session's subproc must not be killed when s1 is "
            "canceled")
        assert p_other.poll() is None, (
            "unowned subproc must not be killed by a session cancel")
    finally:
        for p in (p1, p2, p_other):
            try:
                if p.poll() is None:
                    p.kill()
                    p.wait(timeout=2.0)
            except Exception:
                pass
            poller.unregister_subprocess(p)
    reg.close_all()


def test_session_watchdog_hard_exits_when_wedged():
    """Round-14 AA-CRIT1 / BB-CRIT1: when a session is wedged past
    runtime_s + WATCHDOG_HARD_GRACE_S (e.g. an op that doesn't honor
    cancel because it's stuck in a C-level USB syscall), the watchdog
    must call os._exit(2) so run_poller.sh respawns the poller. This
    is the ONLY escape for a session hung in third-party C code.

    The first cut of this code (commit 9aca226) used `sys.stderr` but
    forgot `import sys`, so the watchdog raised NameError silently in
    the daemon thread and os._exit(2) was unreachable -- the bench
    stayed wedged forever. The watchdog must not be allowed to
    silently fail.
    """
    import session as _session

    # Spy os._exit so the test runner doesn't actually die.
    exit_calls = []
    real_exit = os._exit
    os._exit = lambda code: exit_calls.append(code)

    # Shrink the grace windows so the test takes <1s instead of 90+.
    saved_soft = _session.WATCHDOG_SOFT_GRACE_S
    saved_hard = _session.WATCHDOG_HARD_GRACE_S
    _session.WATCHDOG_SOFT_GRACE_S = 0.05
    _session.WATCHDOG_HARD_GRACE_S = 0.20

    def _hang(session, h, args):
        # Sleep way past runtime + hard grace. time.sleep releases
        # the GIL the same way a C-level USB syscall would, so this
        # exercises the watchdog's _done_event.wait timeout path
        # faithfully.
        time.sleep(5.0)

    class HangPlugin(FakePlugin):
        name = "hang"
        ops = {"hang": Op(args={}, doc="sleep through cancel",
                          run=_hang)}

    parsed = plan.load_tar(plan.pack_tar("hang:hang\n", {}))
    plugins = {"hang": HangPlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()
    sess = Session(reg, parsed, runtime_s=0.05)

    # Drive the session in a thread so we can return when the
    # watchdog fires (the watchdog calls our os._exit spy, but the
    # _hang thread keeps sleeping; we don't wait for it).
    t = threading.Thread(target=lambda: sess.run_all(plugins),
                         daemon=True)
    t.start()
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not exit_calls:
        time.sleep(0.05)
    try:
        assert exit_calls == [2], (
            f"watchdog must call os._exit(2); got {exit_calls!r}. "
            f"This regressed because someone removed `import sys` "
            f"or otherwise broke the watchdog body silently.")
    finally:
        os._exit = real_exit
        _session.WATCHDOG_SOFT_GRACE_S = saved_soft
        _session.WATCHDOG_HARD_GRACE_S = saved_hard
        reg.close_all()


def test_acquire_open_timeout_quarantines_and_doesnt_wedge():
    """Round-13 Y-CRIT1: pl.open() in _Acquire.__enter__ must have
    a wall-clock cap; a hung open used to hold dev_lock + a worker
    slot indefinitely with no path to recovery (the runtime_s
    deadline can't fire because the session never reaches its next
    op boundary).

    A hung open should: (a) return control quickly (BusyError),
    (b) quarantine the key so subsequent acquires fast-fail
    instead of also hanging.
    """
    import registry as _reg

    class HungOpenPlugin(DevicePlugin):
        name = "hungopen"
        ops = {"tick": Op(args={}, doc="", run=_noop)}

        def probe(self):
            return [{"id": "0"}]

        def open(self, spec):
            time.sleep(_reg.OPEN_TIMEOUT_S * 3)
            return type("H", (), {})()

        def close(self, h):
            pass

    plugins = {"hungopen": HungOpenPlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()
    saved = _reg.OPEN_TIMEOUT_S
    _reg.OPEN_TIMEOUT_S = 0.5
    try:
        from plugin import BusyError
        t0 = time.monotonic()
        try:
            with reg.acquire("hungopen.0"):
                raise AssertionError("acquire of hung-open should not return")
        except BusyError as e:
            elapsed = time.monotonic() - t0
            assert "timed out" in str(e) or "quarantined" in str(e), e
            assert elapsed < 2.0, f"acquire blocked {elapsed:.2f}s on hung open"
        # Subsequent acquire must fast-fail (quarantined).
        t1 = time.monotonic()
        try:
            with reg.acquire("hungopen.0"):
                raise AssertionError("quarantined key must refuse acquire")
        except BusyError as e:
            elapsed2 = time.monotonic() - t1
            assert "quarantined" in str(e), e
            assert elapsed2 < 0.2, (
                f"quarantined acquire took {elapsed2:.3f}s; should be ~instant")
    finally:
        _reg.OPEN_TIMEOUT_S = saved
        reg.close_all()


def test_refresh_survives_a_hung_probe():
    """A plugin's probe() that hangs in a C call (e.g. ftd2xx's
    getDeviceInfoDetail wedged on a USB blip) must NOT block the
    main loop. registry.refresh() runs on the poller's main thread
    and a hung probe used to wedge the entire bench (no pickups,
    no cancels, no inflight publishes for 21+ hours).

    refresh() now caps each probe call at PROBE_TIMEOUT_S; a hung
    probe is logged + skipped so the rest of the plugins still
    contribute their specs and the tick completes.
    """
    import registry as _reg

    class HungPlugin(DevicePlugin):
        name = "hung"

        def probe(self):
            # Mimic a C-level wedge by sleeping much longer than the
            # cap. Test must not block on this.
            time.sleep(_reg.PROBE_TIMEOUT_S * 3)
            return [{"id": "0"}]

        def open(self, spec):
            return type("H", (), {})()

        def close(self, h):
            pass

    plugins = {"hung": HungPlugin(), "fake": FakePlugin()}
    reg = DeviceRegistry(plugins)
    # Shrink the cap for the test so it actually finishes.
    saved = _reg.PROBE_TIMEOUT_S
    _reg.PROBE_TIMEOUT_S = 0.5
    try:
        t0 = time.monotonic()
        reg.refresh()
        elapsed = time.monotonic() - t0
        # Refresh must return promptly, not block on the hung probe.
        assert elapsed < 2.0, f"refresh blocked {elapsed:.2f}s on hung probe"
        # The healthy plugin's specs must still land.
        assert "fake.0" in reg.specs, reg.specs
        # The hung plugin contributes nothing this tick.
        assert "hung.0" not in reg.specs, reg.specs
    finally:
        _reg.PROBE_TIMEOUT_S = saved
        reg.close_all()


def test_hung_probe_keeps_last_good_specs():
    """A timed-out probe is not proof that devices vanished. Keep the
    last good plugin specs so a wedged fpga.probe() cannot erase
    fpga.* from /devices while the OS still enumerates the hardware.
    A later successful empty probe is still allowed to remove them.
    """
    import registry as _reg

    class FlakyProbePlugin(DevicePlugin):
        name = "flaky"

        def __init__(self):
            self.mode = "present"

        def probe(self):
            if self.mode == "hang":
                time.sleep(_reg.PROBE_TIMEOUT_S * 3)
                return [{"id": "0"}]
            if self.mode == "absent":
                return []
            return [{"id": "0"}]

        def open(self, spec):
            return type("H", (), {})()

        def close(self, h):
            pass

    flaky = FlakyProbePlugin()
    reg = DeviceRegistry({"flaky": flaky})
    saved = _reg.PROBE_TIMEOUT_S
    _reg.PROBE_TIMEOUT_S = 0.2
    try:
        reg.refresh()
        assert "flaky.0" in reg.specs, reg.specs
        flaky.mode = "hang"
        reg.refresh()
        assert "flaky.0" in reg.specs, (
            "timed-out probe must retain the last good spec")
        time.sleep(_reg.PROBE_TIMEOUT_S * 3.5)
        flaky.mode = "absent"
        reg.refresh()
        assert "flaky.0" not in reg.specs, (
            "successful empty probe must still evict absent devices")
    finally:
        _reg.PROBE_TIMEOUT_S = saved
        reg.close_all()


def test_ftd2xx_enumeration_timeout_stays_out_of_process():
    from plugins import _usb
    saved_run = _usb.subprocess.run
    try:
        def _timeout(*args, **kwargs):
            raise _usb.subprocess.TimeoutExpired(args[0], kwargs["timeout"])
        _usb.subprocess.run = _timeout
        assert _usb.ftd2xx_devices() is None
    finally:
        _usb.subprocess.run = saved_run


def test_fpga_program_cancel_kills_helper_process():
    from plugins import fpga

    class FakeProc:
        def __init__(self):
            self.killed = False

        def poll(self):
            return None

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            return -9

    proc = FakeProc()
    saved_popen = fpga.subprocess.Popen
    try:
        fpga.subprocess.Popen = lambda *a, **kw: proc
        sess = type("S", (), {"runtime_s": 1.0, "canceled": True})()
        try:
            fpga._program_flash_subprocess(b"x", "desc", None, sess)
            raise AssertionError("canceled helper should raise")
        except RuntimeError as e:
            assert "canceled" in str(e), e
        assert proc.killed, "canceled program did not kill helper"
    finally:
        fpga.subprocess.Popen = saved_popen


def test_refresh_does_not_evict_pinned():
    """If a session has acquired a device and refresh runs while
    that device is transiently absent from probe(), the spec must
    NOT be evicted -- the session's next op on the same key would
    otherwise hit LookupError.

    Round 3 F3: pinned_specs gate. Round 4 H2: Counter not set.
    """
    fake = FakePlugin()
    plugins = {"fake": fake}
    reg = DeviceRegistry(plugins); reg.refresh()
    key = reg.resolve("fake")
    # Eagerly pin the spec (mimics what session.run_all does).
    reg.pinned_specs[key] += 1
    try:
        # Now make probe() return [] -- device "vanished".
        fake.probe = lambda: []
        reg.refresh()
        # Spec must still be there because pinned.
        assert key in reg.specs, "pinned spec was evicted"
    finally:
        reg.pinned_specs.pop(key, None)
    # Once unpinned, refresh evicts cleanly.
    reg.refresh()
    assert key not in reg.specs


def test_dispatch_rejects_garbage_plan():
    """The poller's _dispatch path must produce a failure artefact
    when load_tar can't parse the body. Round 3 found a regression
    here (walk(op.body) AttributeError) that no test caught.
    """
    import poller
    body = b"not-a-tar-body" + b"\x00" * 600
    with tempfile.TemporaryDirectory() as tmp:
        old_pending = poller.PENDING
        poller.PENDING = os.path.join(tmp, "pending")
        try:
            poller._spool_artefact("a"*64, b"sentinel-body")  # warm-up
            # Now send garbage to _failure_artefact via _dispatch's
            # parse-error path. Direct call:
            tar = poller._failure_artefact("a"*64, "plan parse failed: x")
            with tarfile.open(fileobj=io.BytesIO(tar), mode="r") as tf:
                names = tf.getnames()
                assert "manifest.json" in names
                assert "errors.log" in names
                m = json.loads(tf.extractfile("manifest.json").read())
                assert m["status"] == "failed"
                assert "message" in m
        finally:
            poller.PENDING = old_pending


def test_spool_unique_per_attempt():
    """Two _spool_artefact calls for the same digest must produce
    distinct files so an old upload's late unlink can't delete the
    new one (round 4 H1)."""
    import poller
    with tempfile.TemporaryDirectory() as tmp:
        old_pending = poller.PENDING
        poller.PENDING = os.path.join(tmp, "pending")
        try:
            digest = "a" * 64
            p1 = poller._spool_artefact(digest, b"first")
            p2 = poller._spool_artefact(digest, b"second")
            assert p1 != p2, "spool paths collided"
            assert os.path.exists(p1) and os.path.exists(p2)
        finally:
            poller.PENDING = old_pending


def test_pending_upload_drain_kick_is_nonblocking():
    """A stuck tunnel upload retry must not stop the poll loop."""
    import poller
    started = threading.Event()
    release = threading.Event()
    calls = []
    saved = poller._drain_pending_uploads

    def _slow(timeout_s=None):
        calls.append(timeout_s)
        started.set()
        release.wait(timeout=2.0)

    poller._drain_pending_uploads = _slow
    try:
        t0 = time.monotonic()
        assert poller._kick_pending_upload_drain(timeout_s=1.0)
        assert started.wait(timeout=1.0), "drain thread did not start"
        assert time.monotonic() - t0 < 0.5
        assert not poller._kick_pending_upload_drain(timeout_s=1.0)
    finally:
        release.set()
        poller._drain_pending_uploads = saved
        # Wait briefly for the drain lock to be released so later
        # tests/importers do not inherit a locked singleton.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if poller._pending_drain_lock.acquire(blocking=False):
                poller._pending_drain_lock.release()
                break
            time.sleep(0.01)
    assert calls == [1.0], calls


def test_supervisor_acquires_poller_lock_before_spawning_worker():
    import inspect
    import poller
    src = inspect.getsource(poller._supervise)
    makedirs_pos = src.find("os.makedirs(STATE_DIR")
    acquire_pos = src.find("_acquire_poller_lock()")
    popen_pos = src.find("subprocess.Popen")
    assert makedirs_pos != -1, (
        "supervisor must create STATE_DIR before locking")
    assert acquire_pos != -1, "supervisor must own poller.lock"
    assert popen_pos != -1, "test assumes supervisor still spawns child"
    assert makedirs_pos < acquire_pos, (
        "supervisor must create STATE_DIR before opening poller.lock")
    assert acquire_pos < popen_pos, (
        "poller.lock must be acquired before spawning worker so an "
        "outer while/timeout wrapper cannot start duplicate supervisors")
    assert "_LOCK_HELD_ENV" in src, (
        "worker must inherit lock-held marker and skip reacquiring")


def test_refused_spool_409_is_backed_off():
    import poller
    import urllib.error

    with tempfile.TemporaryDirectory() as tmp:
        old_pending = poller.PENDING
        old_post = poller._post
        old_retry = dict(poller._refused_retry_after)
        poller.PENDING = os.path.join(tmp, "pending")
        poller._refused_retry_after.clear()
        calls = []

        def _refuse(url, body, timeout=None):
            calls.append(url)
            raise urllib.error.HTTPError(
                url, 409, "Conflict", {}, io.BytesIO(b""))

        poller._post = _refuse
        try:
            digest = "a" * 64
            poller._spool_artefact(digest, b"body")
            poller._drain_pending_uploads(timeout_s=1.0)
            poller._drain_pending_uploads(timeout_s=1.0)
            assert len(calls) == 1, calls
            assert poller._refused_retry_after, "409 did not set backoff"
        finally:
            poller.PENDING = old_pending
            poller._post = old_post
            poller._refused_retry_after.clear()
            poller._refused_retry_after.update(old_retry)


def test_dsp_boot_requires_timeout_and_kills_hung_helper():
    import subprocess
    from plugins import dsp

    assert dsp.DspPlugin.ops["boot"].args == {
        "ldr": "blob", "timeout_ms": "int"}
    assert "timeout_ms is mandatory" in dsp.DspPlugin.ops["boot"].doc

    class FakeProc:
        returncode = None
        killed = False
        stdin = io.StringIO()

        def communicate(self, timeout=None):
            if self.killed:
                self.returncode = -9
                return "", "native spin"
            raise subprocess.TimeoutExpired(["fake"], timeout)

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.returncode = -9
            return self.returncode

        def poll(self):
            return self.returncode

    class FakeSession:
        canceled = False
        events = []
        cancel_lock = threading.Lock()

        def bail_if_canceled(self, where):
            if self.canceled:
                raise RuntimeError(f"{where} canceled")

        def log_event(self, *args):
            self.events.append(args)

    saved_popen = dsp.subprocess.Popen
    proc = FakeProc()
    dsp.subprocess.Popen = lambda *a, **k: proc
    try:
        try:
            dsp._boot_subprocess(FakeSession(), "FT4222 A", b"x" * 1024,
                                 timeout_s=0.01)
            raise AssertionError("hung helper should time out")
        except TimeoutError as e:
            assert "dsp:boot timed out" in str(e), e
            assert proc.killed, "hung helper was not killed"
    finally:
        dsp.subprocess.Popen = saved_popen


def test_dsp_boot_cancel_race_reports_cancel():
    import subprocess
    from plugins import dsp

    class FakeProc:
        returncode = -9
        stdin = io.StringIO()

        def communicate(self, timeout=None):
            return "", ""

        def kill(self):
            pass

        def poll(self):
            return self.returncode

    class FakeSession:
        canceled = True
        cancel_lock = threading.Lock()

        def bail_if_canceled(self, where):
            if self.canceled:
                raise RuntimeError(f"{where} canceled")

    saved_popen = dsp.subprocess.Popen
    dsp.subprocess.Popen = lambda *a, **k: FakeProc()
    try:
        try:
            dsp._boot_subprocess(FakeSession(), "FT4222 A", b"x" * 1024,
                                 timeout_s=1.0)
            raise AssertionError("canceled helper should raise")
        except RuntimeError as e:
            assert "canceled" in str(e), e
    finally:
        dsp.subprocess.Popen = saved_popen


def test_dsp_boot_already_canceled_does_not_spawn_helper():
    from plugins import dsp

    class FakeSession:
        canceled = True
        cancel_lock = threading.Lock()

        def bail_if_canceled(self, where):
            if self.canceled:
                raise RuntimeError(f"{where} canceled")

    calls = []
    saved_popen = dsp.subprocess.Popen
    dsp.subprocess.Popen = lambda *a, **k: calls.append((a, k))
    try:
        try:
            dsp._boot_subprocess(FakeSession(), "FT4222 A", b"x" * 1024,
                                 timeout_s=1.0)
            raise AssertionError("already-canceled boot should raise")
        except RuntimeError as e:
            assert "canceled" in str(e), e
        assert calls == [], "helper spawned for already-canceled session"
    finally:
        dsp.subprocess.Popen = saved_popen


def test_msc_generated_writes_are_reproducible():
    from plugins import msc
    from plugins._prbs import prbs_xorshift32

    class FakeSession:
        canceled = False
        events = []
        checks = []
        streams = {}

        def bail_if_canceled(self, where):
            if self.canceled:
                raise RuntimeError(f"{where} canceled")

        def log_event(self, *args):
            self.events.append(args)

        def record_check(self, *args):
            self.checks.append(args)

        def stream(self, name):
            class S:
                def __init__(self):
                    self.data = b""

                def append(self, data):
                    self.data += data

            return self.streams.setdefault(name, S())

    with tempfile.NamedTemporaryFile() as f:
        f.write(b"\xff" * 4096)
        f.flush()
        handle = msc.MscHandle(f.name, block_size=512)
        session = FakeSession()
        old_chunk = msc.CHUNK_BYTES
        saved_read = msc.os.read
        msc.CHUNK_BYTES = 7
        try:
            msc._op_write_zeroes(
                session, handle, {"n": 16, "offset_lba": 1})
            msc._op_write_prbs(
                session, handle,
                {"seed": 0x12345678, "n": 32, "offset_lba": 2})
            # Simulate a block device / file returning short reads;
            # generated verify must accumulate, not fail immediately.
            msc.os.read = lambda fd, n: saved_read(fd, min(n, 3))
            msc._op_verify_zeroes(
                session, handle, {"n": 16, "offset_lba": 1})
            msc._op_verify_prbs(
                session, handle,
                {"seed": 0x12345678, "n": 32, "offset_lba": 2})
        finally:
            msc.os.read = saved_read
            msc.CHUNK_BYTES = old_chunk
        with open(f.name, "rb") as r:
            r.seek(512)
            assert r.read(16) == b"\0" * 16
            assert r.read(16) == b"\xff" * 16
            r.seek(1024)
            got = r.read(32)
        assert got == prbs_xorshift32(0x12345678, 32), got
        statuses = [c[3] for c in session.checks]
        assert statuses == ["hit", "hit"], session.checks

        with open(f.name, "r+b") as w:
            w.seek(1024 + 5)
            b = w.read(1)
            w.seek(1024 + 5)
            w.write(bytes([b[0] ^ 0xff]))
        try:
            msc._op_verify_prbs(
                session, handle,
                {"seed": 0x12345678, "n": 32, "offset_lba": 2,
                 "min_rate_Bps": 10**18})
            raise AssertionError("corrupt PRBS should fail verify")
        except ValueError as e:
            assert "msc:verify_prbs mismatch" in str(e), e
        assert session.checks[-1][3] == "miss", session.checks[-1]
        assert b"MISMATCH" in session.streams["msc.verify_mismatch"].data


def test_msc_write_prbs_inventory_documents_reproduction():
    from plugins import msc

    for name in ("write_prbs", "verify_prbs"):
        op = msc.MscPlugin.ops[name]
        assert op.args == {"seed": "int", "n": "int"}
        assert "xorshift32" in op.doc
        assert "identical seed, n, and offset_lba" in op.doc
    assert "verify_zeroes" in msc.MscPlugin.ops


def test_supervisor_heartbeat_stale_uses_monotonic_age():
    import poller
    stale, age = poller._heartbeat_stale(1000.0, now=1119.0)
    assert not stale, (stale, age)
    stale, age = poller._heartbeat_stale(1000.0, now=1121.0)
    assert stale, (stale, age)


def test_watchdog_log_records_before_and_after_process_snapshots():
    import watchdog

    class Result:
        returncode = 0

    with tempfile.TemporaryDirectory() as tmp:
        old_hb = watchdog.HEARTBEAT
        old_log = watchdog.WATCHDOG_LOG
        watchdog.HEARTBEAT = os.path.join(tmp, "heartbeat.log")
        watchdog.WATCHDOG_LOG = os.path.join(tmp, "watchdog.log")
        try:
            with open(watchdog.HEARTBEAT, "w", encoding="utf-8") as f:
                f.write("2026-05-05T15:49:30\n")
            watchdog._append_watchdog_log(
                age=340.0,
                detail="age=340s",
                pkill_result=Result(),
                processes_before="old-poller pid=1",
                processes_after="new-poller pid=2",
                processes_settled="<none>",
                settle_elapsed_s=0.2)
            with open(watchdog.WATCHDOG_LOG, encoding="utf-8") as f:
                body = f.read()
            assert "processes_before_kill:\nold-poller pid=1" in body
            assert ("processes_after_kill_immediate:\n"
                    "new-poller pid=2") in body
            assert "processes_after_kill_settled (0.2s):\n<none>" in body
        finally:
            watchdog.HEARTBEAT = old_hb
            watchdog.WATCHDOG_LOG = old_log


def test_lease_lifecycle():
    """Full lease flow: claim -> resume from another session ->
    blocks_acquire rejects an unrelated session -> release.

    This is the regression that brings back the old `lease` subsystem
    we cut in round 2 (D1) -- the test plus the example plan plus
    the README section are the three guards against it being cut
    again on the same "no users" reasoning.
    """
    from plugins.lease import LeasePlugin
    fake = FakePlugin()
    plugins = {"fake": fake, "lease": LeasePlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()

    # 1. First plan claims fake.0 for 60 s.
    p1 = plan.load_tar(plan.pack_tar(
        'lease:claim devices="fake.0" duration_s=60\n'
        'fake:noop k=1\n', {}))
    s1 = Session(reg, p1)
    s1.run_all(plugins)
    token = s1.lease_token
    assert token is not None and len(token) == 16, token
    assert not s1.errors, s1.errors
    # Token lands in manifest.lease_token (not in any stream/event)
    # so a tunnel-side observer can't read it from /inflight while
    # the session is live -- only whoever fetches the artefact tar
    # sees it.
    _, m1 = pack_artefact(s1)
    m1 = json.loads(m1)
    assert m1.get("lease_token") == token, m1
    # And not in lease.token stream (which no longer exists) or in
    # any timeline-event message.
    assert "lease.token" not in s1.streams
    for ev in s1.events:
        assert token not in ev["msg"], ev

    # 2. Other agent (no token) is fast-rejected.
    p2 = plan.load_tar(plan.pack_tar('fake:noop k=2\n', {}))
    s2 = Session(reg, p2)
    s2.run_all(plugins)
    assert s2.errors, "lease should have blocked the unleased session"
    assert "leased to" in s2.errors[0], s2.errors[0]
    # fake.opens count didn't increment for s2.
    opens_before_resume = fake.opens

    # 3. Resume from a follow-up plan with the right token: works.
    p3 = plan.load_tar(plan.pack_tar(
        f'lease:resume token="{token}"\n'
        'fake:noop k=3\n', {}))
    s3 = Session(reg, p3)
    s3.run_all(plugins)
    assert not s3.errors, s3.errors
    assert fake.opens > opens_before_resume

    # 4. Wrong token: rejected.
    p4 = plan.load_tar(plan.pack_tar(
        'lease:resume token="not-a-real-token"\n'
        'fake:noop k=4\n', {}))
    s4 = Session(reg, p4)
    s4.run_all(plugins)
    assert s4.errors and "lease" in s4.errors[0].lower(), s4.errors

    # 5. Release lets unleased sessions through again.
    p5 = plan.load_tar(plan.pack_tar(
        f'lease:resume token="{token}"\n'
        f'lease:release token="{token}"\n', {}))
    s5 = Session(reg, p5)
    s5.run_all(plugins)
    assert not s5.errors, s5.errors

    p6 = plan.load_tar(plan.pack_tar('fake:noop k=6\n', {}))
    s6 = Session(reg, p6)
    s6.run_all(plugins)
    assert not s6.errors, ("after release, unleased session should run; "
                           f"got {s6.errors}")

    reg.close_all()


def test_lease_release_honors_explicit_token_without_resume():
    """Round-12 W1: a one-line plan `lease:release token="..."` (no
    preceding lease:resume) must actually drop the lease. The op
    receives args["token"] decoded to a plain str by decode_args;
    the previous helper looked for a Value.raw attribute and silently
    fell back to session.lease_token (None for a fresh session) --
    so the operator saw "no-op" while the lease lived on until expiry.
    """
    from plugins.lease import LeasePlugin
    plugins = {"fake": FakePlugin(), "lease": LeasePlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()
    token = reg.lease_claim(["fake.0"], 60)
    assert token in reg.leases
    parsed = plan.load_tar(plan.pack_tar(
        f'lease:release token="{token}"\n', {}))
    s = Session(reg, parsed)
    s.run_all(plugins)
    assert not s.errors, s.errors
    assert token not in reg.leases, (
        "explicit-token release must drop the lease even with no "
        "preceding lease:resume")
    reg.close_all()


def test_expect_lands_in_manifest():
    """`expect "<claim>"` must surface in manifest.expectations[].
    A plan that records `expect` but never runs a *machine-checkable*
    assertion (no *_expect, no scope summary) is "inert" -- it survived
    but proved nothing. The new status discriminator catches this.
    """
    parsed = plan.load_tar(plan.pack_tar(
        'expect "DUT boots in 30s"\nfake:noop k=1\n', {}))
    plugins = {"fake": FakePlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()
    session = Session(reg, parsed)
    session.run_all(plugins)
    assert session.expectations == ["DUT boots in 30s"]
    _, mtxt = pack_artefact(session)
    m = json.loads(mtxt)
    assert m["expectations"] == ["DUT boots in 30s"]
    # No checks ran (only expect + noop), so status is "inert" not "ok".
    assert m["status"] == "inert", m["status"]
    assert m["checks"] == []
    # New manifest fields surface.
    assert m["run_id"], m
    assert "files" in m
    assert "blob_digests" in m
    reg.close_all()


def test_stream_truncation_marker_survives_multiple_cap_hits():
    """L1 regression: the previous Stream impl stored the truncation
    marker as a record at index 0 on first cap-hit and silently
    popped it on the second cap-hit. snapshot_bytes() / snapshot_-
    timestamped() must still produce the marker after multiple
    eviction cycles.
    """
    from session import Stream, STREAM_MAX_BYTES
    s = Stream("x", t0=time.monotonic())
    chunk = b"a" * (STREAM_MAX_BYTES // 4)
    # Fill past the cap so eviction starts.
    for _ in range(8):
        s.append(chunk)
    snap1 = s.snapshot_bytes()
    assert b"STREAM TRUNCATED" in snap1, "marker missing after 1st cycle"
    # Fill again past the cap; the marker would be the oldest record
    # in the previous impl and would be silently popped.
    for _ in range(8):
        s.append(chunk)
    snap2 = s.snapshot_bytes()
    assert b"STREAM TRUNCATED" in snap2, (
        "marker silently dropped on second cap-hit cycle (L1 regression)")
    # Same for the timestamped snapshot (used by render_timeline).
    recs = s.snapshot_timestamped()
    assert any(b"STREAM TRUNCATED" in r[1] for r in recs), recs


def test_stream_contains_bytes_incremental_and_cross_record():
    from session import Stream
    s = Stream("x", t0=time.monotonic())
    cursor = {}
    s.append(b"abc")
    assert not s.contains_bytes(b"cdef", cursor)
    # Match spans the previous call's retained tail and this append.
    s.append(b"def")
    assert s.contains_bytes(b"cdef", cursor)
    # A later search with a new cursor still sees historical data.
    assert s.contains_bytes(b"abcdef", {})


def test_stream_contains_bytes_resets_after_truncation():
    import session as _session
    from session import Stream

    old_max = _session.STREAM_MAX_BYTES
    _session.STREAM_MAX_BYTES = 8
    try:
        s = Stream("x", t0=time.monotonic())
        cursor = {}
        s.append(b"aaaa")
        assert not s.contains_bytes(b"zz", cursor)
        # This append evicts the first record. The cursor's numeric
        # index would otherwise point past the retained records and
        # skip the new data.
        s.append(b"bbbbbbbb")
        s.append(b"zz")
        assert s.contains_bytes(b"zz", cursor)
    finally:
        _session.STREAM_MAX_BYTES = old_max


def test_lease_claim_rejects_lease_pseudo_device():
    """L3 regression: an agent could claim `lease._default` (the
    lease plugin's own probe pseudo-device) and lock the lease
    subsystem bench-wide, since every other session's lease op
    would acquire lease._default and hit the lease check."""
    from plugins.lease import LeasePlugin
    plugins = {"lease": LeasePlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()
    try:
        reg.lease_claim(["lease._default"], 60)
    except Exception as e:
        assert "lease plugin" in str(e) or "pseudo" in str(e), str(e)
    else:
        raise AssertionError(
            "lease_claim must reject lease._default (L3 regression)")
    reg.close_all()


def test_failure_artefact_carries_identity_fields():
    """L8 regression: _failure_artefact's manifest must carry
    run_id, plan_digest, code_digest so an aggregator scripting on
    those fields doesn't see null on failed runs."""
    import poller
    tar = poller._failure_artefact("a" * 64, "test failure")
    with tarfile.open(fileobj=io.BytesIO(tar), mode="r") as tf:
        m = json.loads(tf.extractfile("manifest.json").read())
    assert m["status"] == "failed"
    assert m["plan_digest"] == "a" * 64, m
    assert m["run_id"] and m["run_id"].startswith("sess-"), m
    assert m["code_digest"] is not None, m
    assert m["blob_digests"] == {}, m


def test_prune_skips_inflight_digests():
    """Reviewer P / round-8 P1 + round-12-extra: clear-stale must not
    unlink DONE/.plan of:
      a) digests the poller's published inflight.json says are live, OR
      b) digests whose DONE/.plan was created less than PRUNE_MIN_AGE_S
         seconds ago (covers the publish-lag window before the first
         inflight push reaches the server -- a 105ms lease op finishes
         long before STATUS/inflight.json catches up).
    Old, non-inflight DONE/.plan files do get pruned.
    """
    with tempfile.TemporaryDirectory() as tmp:
        old_dirs = (server.INPUTS, server.OUTPUTS, server.DONE,
                    server.STATUS, server.RELEASE, server.SWEEP)
        server.INPUTS = os.path.join(tmp, "inputs")
        server.OUTPUTS = os.path.join(tmp, "outputs")
        server.DONE = os.path.join(tmp, "done")
        server.STATUS = os.path.join(tmp, "status")
        server.RELEASE = os.path.join(tmp, "release")
        server.SWEEP = os.path.join(tmp, "sweep")
        for d in (server.INPUTS, server.OUTPUTS, server.DONE,
                  server.STATUS, server.RELEASE, server.SWEEP):
            os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            live = "a" * 64    # in inflight, old mtime
            stale = "b" * 64   # not in inflight, old mtime -- prunable
            fresh = "c" * 64   # not in inflight, new mtime -- protected
            for digest in (live, stale, fresh):
                with open(os.path.join(server.DONE, f"{digest}.plan"),
                          "wb") as f:
                    f.write(b"plan-bytes\n")
            # Backdate `live` and `stale` past PRUNE_MIN_AGE_S so the
            # mtime guard doesn't shadow the inflight test.
            old_t = time.time() - server.PRUNE_MIN_AGE_S - 1.0
            for digest in (live, stale):
                os.utime(os.path.join(server.DONE, f"{digest}.plan"),
                         (old_t, old_t))
            with open(os.path.join(server.STATUS, "inflight.json"),
                      "wb") as f:
                f.write(json.dumps([{"digest": live}]).encode())

            removed = server.prune_stale_jobs()
            assert removed == 1, removed
            assert os.path.exists(
                os.path.join(server.DONE, f"{live}.plan")), \
                "in-flight digest must survive prune"
            assert os.path.exists(
                os.path.join(server.DONE, f"{fresh}.plan")), \
                "fresh DONE/.plan must survive prune (mtime guard)"
            assert not os.path.exists(
                os.path.join(server.DONE, f"{stale}.plan")), \
                "non-inflight stale digest must be pruned"

            # Defence in depth: gate logic accepts upload for an
            # inflight digest even after DONE/.plan is gone.
            inflight = server._inflight_digests()
            assert live in inflight
            assert stale not in inflight

            stale_t = time.time() - server.INFLIGHT_STALE_S - 1.0
            os.utime(os.path.join(server.STATUS, "inflight.json"),
                     (stale_t, stale_t))
            assert server._inflight_digests() == set(), \
                "stale inflight snapshot must not protect jobs forever"
        finally:
            (server.INPUTS, server.OUTPUTS, server.DONE,
             server.STATUS, server.RELEASE, server.SWEEP) = old_dirs


def test_stale_cancel_resolution_removes_orphan_running_record():
    with tempfile.TemporaryDirectory() as tmp:
        old_dirs = (server.INPUTS, server.OUTPUTS, server.DONE,
                    server.STATUS, server.RELEASE, server.SWEEP,
                    server.CANCEL)
        server.INPUTS = os.path.join(tmp, "inputs")
        server.OUTPUTS = os.path.join(tmp, "outputs")
        server.DONE = os.path.join(tmp, "done")
        server.STATUS = os.path.join(tmp, "status")
        server.RELEASE = os.path.join(tmp, "release")
        server.SWEEP = os.path.join(tmp, "sweep")
        server.CANCEL = os.path.join(tmp, "cancel")
        for d in (server.INPUTS, server.OUTPUTS, server.DONE,
                  server.STATUS, server.RELEASE, server.SWEEP,
                  server.CANCEL):
            os.makedirs(d, mode=0o700, exist_ok=True)
        httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            stale = "d" * 64
            live = "e" * 64
            for digest in (stale, live):
                with open(os.path.join(server.DONE, f"{digest}.plan"),
                          "wb") as f:
                    f.write(b"plan")
                with open(os.path.join(server.CANCEL, digest), "wb"):
                    pass
            with open(os.path.join(server.STATUS, "inflight.json"),
                      "wb") as f:
                f.write(json.dumps([{"digest": live}]).encode())

            host, port = httpd.server_address
            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                conn.request("POST", f"/cancels/{stale}/stale", body=b"")
                resp = conn.getresponse()
                body = json.loads(resp.read())
            finally:
                conn.close()
            assert resp.status == 200, resp.status
            assert body["status"] == "stale_canceled", body
            assert not os.path.exists(
                os.path.join(server.DONE, f"{stale}.plan"))
            assert not os.path.exists(os.path.join(server.CANCEL, stale))

            conn = http.client.HTTPConnection(host, port, timeout=5)
            try:
                conn.request("POST", f"/cancels/{live}/stale", body=b"")
                resp = conn.getresponse()
                body = json.loads(resp.read())
            finally:
                conn.close()
            assert resp.status == 409, (resp.status, body)
            assert body["status"] == "inflight", body
            assert os.path.exists(os.path.join(server.CANCEL, live))
            assert os.path.exists(
                os.path.join(server.DONE, f"{live}.plan"))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
            (server.INPUTS, server.OUTPUTS, server.DONE,
             server.STATUS, server.RELEASE, server.SWEEP,
             server.CANCEL) = old_dirs


def test_delete_outputs_no_op_when_nothing_to_delete():
    """An agent's DELETE /outputs/<digest> can race a fresh re-pickup
    of the same digest: agent thinks it's cleaning up after a fetch,
    server has already moved INPUTS/.plan to DONE/.plan for the new
    pickup. If delete_outputs unconditionally removes DONE/.plan,
    the live session's eventual artefact upload 409s and the spool
    is parked under refused/.

    Rule: only remove DONE/<digest>.plan if at least one OUTPUTS file
    was actually removed. A bare DELETE for a digest with no OUTPUTS
    is a no-op (idempotent).
    """
    with tempfile.TemporaryDirectory() as tmp:
        old_dirs = (server.OUTPUTS, server.DONE)
        server.OUTPUTS = os.path.join(tmp, "outputs")
        server.DONE = os.path.join(tmp, "done")
        for d in (server.OUTPUTS, server.DONE):
            os.makedirs(d, mode=0o700, exist_ok=True)
        try:
            d = "a" * 64
            with open(os.path.join(server.DONE, f"{d}.plan"), "wb") as f:
                f.write(b"freshly-picked-up\n")

            # No OUTPUTS for this digest. Stale DELETE comes in.
            removed = server.delete_outputs(d, "")
            assert removed == 0
            assert os.path.exists(
                os.path.join(server.DONE, f"{d}.plan")), \
                "fresh DONE/.plan must survive a stale DELETE /outputs"

            # Now the legitimate flow: an artefact lands, then DELETE.
            with open(os.path.join(server.OUTPUTS, f"{d}.tar"),
                      "wb") as f:
                f.write(b"x")
            removed = server.delete_outputs(d, "")
            assert removed == 1
            assert not os.path.exists(
                os.path.join(server.DONE, f"{d}.plan")), \
                "DONE/.plan must be removed after a real DELETE"
        finally:
            server.OUTPUTS, server.DONE = old_dirs


def test_multi_instance_plan_holds_all_dev_locks_for_session():
    """Round-11 U1: a plan that touches two instances of the same
    plugin (e.g. fake.A and fake.B) must hold per-device dev_locks
    for BOTH instances across the whole session, not just for the
    duration of each op.

    Before this fix, _resolve_device gated on plugin NAME, discarded
    "fake" from _deferred_names after the first deferred resolve, and
    silently skipped the deferred lock acquisition for the second
    instance. A parallel session could win the lock between ops on
    fake.B, breaking the job-atomic invariant.
    """
    import threading as _th

    class TwoInstancePlugin(FakePlugin):
        name = "twoinst"
        ops = {"tick": Op(args={}, doc="no-op", run=_noop)}

        def probe(self):
            return [{"id": "A"}, {"id": "B"}]

    plugins = {"twoinst": TwoInstancePlugin()}
    reg = DeviceRegistry(plugins)
    reg.refresh()

    parsed = plan.load_tar(plan.pack_tar(
        "twoinst.A:tick\ntwoinst.B:tick\n", {}))
    session = Session(reg, parsed)
    session.run_all(plugins)

    # Both keys must be in the session-locked set after the run.
    locked = getattr(session, "_session_locked_keys", set())
    assert "twoinst.A" in locked, locked
    assert "twoinst.B" in locked, locked
    # The per-device RLocks must be the same identity that's now
    # tracked in _deferred_locks (so the session's finally-release
    # actually drops them on exit). Both instances should appear.
    deferred = [lk for _, lk in getattr(session, "_deferred_locks", [])]
    expected = {reg.per_dev_lock["twoinst.A"], reg.per_dev_lock["twoinst.B"]}
    assert set(deferred) == expected, (deferred, expected)
    reg.close_all()


def test_check_record_lands_in_manifest():
    """A plugin's session.record_check call must populate
    manifest.checks[] with a structured pass/fail record."""
    def _expect_op(session, h, args):
        session.record_check("fake_check", "fake.0",
                             "the test passed", "hit",
                             {"observed": "yes"})

    class CheckPlugin(FakePlugin):
        name = "checker"
        ops = {
            "tick": Op(args={}, doc="record a check", run=_expect_op),
        }

    parsed = plan.load_tar(plan.pack_tar("checker:tick\n", {}))
    plugins = {"checker": CheckPlugin()}
    reg = DeviceRegistry(plugins); reg.refresh()
    session = Session(reg, parsed)
    session.run_all(plugins)
    _, mtxt = pack_artefact(session)
    m = json.loads(mtxt)
    assert len(m["checks"]) == 1, m["checks"]
    assert m["checks"][0]["status"] == "hit"
    assert m["checks"][0]["kind"] == "fake_check"
    # With at least one check that hit, status should be "ok" not "inert".
    assert m["status"] == "ok"
    reg.close_all()


def test_bench_id_defaults_to_hostname_and_env_overrides():
    old = os.environ.get("TEST_SERV_BENCH_ID")
    orig_gethostname = socket.gethostname
    try:
        os.environ.pop("TEST_SERV_BENCH_ID", None)
        assert bench_id() == (socket.gethostname() or "unknown")
        socket.gethostname = lambda: ""
        assert bench_id() == "unknown"
        def _raise_hostname():
            raise OSError("hostname unavailable")
        socket.gethostname = _raise_hostname
        assert bench_id() == "unknown"
        os.environ["TEST_SERV_BENCH_ID"] = "bench-alias"
        assert bench_id() == "bench-alias"
    finally:
        socket.gethostname = orig_gethostname
        if old is None:
            os.environ.pop("TEST_SERV_BENCH_ID", None)
        else:
            os.environ["TEST_SERV_BENCH_ID"] = old


def test_usb_and_usbtmc_plugins_expose_bringup_ops():
    from plugins.usb import UsbPlugin
    from plugins.usbtmc import UsbTmcPlugin

    usb_ops = UsbPlugin.ops
    assert {"list", "descriptor", "control", "bulk_write", "bulk_read"} <= \
        set(usb_ops), usb_ops
    assert usb_ops["control"].args == {
        "bmRequestType": "int",
        "bRequest": "int",
        "wValue": "int",
        "wIndex": "int",
    }
    assert usb_ops["bulk_read"].optional_args["detach"] == "bool"

    tmc_ops = UsbTmcPlugin.ops
    assert {"list", "identify", "write", "write_blob", "read",
            "query", "expect", "clear"} <= set(tmc_ops), tmc_ops
    assert tmc_ops["write_blob"].args == {"data": "blob"}
    assert tmc_ops["write_blob"].optional_args["min_rate_Bps"] == "int"
    assert tmc_ops["read"].optional_args["chunk_size"] == "int"
    assert tmc_ops["read"].optional_args["exact"] == "bool"


def test_usbtmc_fast_path_helpers_read_write_and_rate_check():
    from plugins import usbtmc

    rfd, wfd = os.pipe()
    try:
        os.write(wfd, b"abcdef")
        got = usbtmc._read_with_timeout(
            rfd, 6, timeout_ms=1000, chunk_size=3)
        assert got == b"abcdef", got
    finally:
        os.close(rfd)
        os.close(wfd)

    rfd, wfd = os.pipe()
    try:
        written, elapsed = usbtmc._write_all(
            wfd, b"xyz", timeout_ms=1000, chunk_size=2)
        assert written == 3, (written, elapsed)
        assert os.read(rfd, 3) == b"xyz"
    finally:
        os.close(rfd)
        os.close(wfd)

    try:
        usbtmc._check_min_rate("xfer", 100, 10.0, 1000)
    except TimeoutError as e:
        assert "too slow" in str(e)
    else:
        raise AssertionError("expected slow transfer to fail")

    try:
        usbtmc._read_with_timeout(0, 1, timeout_ms=1, chunk_size=0)
    except ValueError as e:
        assert "chunk_size" in str(e)
    else:
        raise AssertionError("zero usbtmc chunk_size should fail")

    class CanceledSession:
        canceled = True

        def bail_if_canceled(self, where):
            raise RuntimeError(f"canceled at {where}")

    try:
        usbtmc._read_with_timeout(
            0, 1, timeout_ms=1000, session=CanceledSession())
    except RuntimeError as e:
        assert "canceled" in str(e)
    else:
        raise AssertionError("usbtmc query read should observe cancel")


def test_usbtmc_and_raw_usb_plan_shapes_parse():
    text = """
    usb.any:list vid=0xfe pid=0x03
    usb.gadget:control bmRequestType=0x80 bRequest=6 wValue=0x0100 wIndex=0 length=18 timeout_ms=1000
    usb.gadget:bulk_write endpoint=0x02 data=@out.bin interface=0 timeout_ms=1000
    usb.gadget:bulk_read endpoint=0x81 length=1024 interface=0 timeout_ms=1000
    usbtmc.0:identify timeout_ms=5000
    usbtmc.0:write data="*CLS\\n" timeout_ms=1000
    usbtmc.0:write_blob data=@out.bin timeout_ms=10000 chunk_size=1048576 min_rate_Bps=25000000
    usbtmc.0:read length=1048576 timeout_ms=10000 chunk_size=1048576 min_rate_Bps=25000000 exact=true
    usbtmc.0:query data="*IDN?\\n" timeout_ms=5000
    usbtmc.0:expect data="PING?\\n" sentinel="PONG" timeout_ms=5000
    usbtmc.0:clear timeout_ms=100
    """
    parsed = plan.load_tar(plan.pack_tar(text, {"out.bin": b"abc"}))
    assert len(parsed.ops) == 11
    assert parsed.ops[0].args["vid"].as_int() == 0xfe
    assert parsed.ops[6].verb == "write_blob"


def test_usbtmc_list_available_without_class_nodes():
    from plugins.usbtmc import UsbTmcPlugin
    plugins = {"usbtmc": UsbTmcPlugin()}
    reg = DeviceRegistry(plugins)
    reg.refresh()
    devices = reg.list_devices()
    assert any(d["id"] == "usbtmc.any" for d in devices), devices

    parsed = plan.load_tar(plan.pack_tar("usbtmc:list\n", {}))
    sess = Session(reg, parsed)
    sess.run_all(plugins)
    assert not sess.errors, sess.errors
    assert "usbtmc.list" in sess.streams


def test_any_pseudo_device_does_not_make_bare_real_device_ambiguous():
    class RealPlusAnyPlugin(FakePlugin):
        name = "realany"

        def probe(self):
            return [{"id": "any", "list_only": True}, {"id": "real"}]

    plugins = {"realany": RealPlusAnyPlugin()}
    reg = DeviceRegistry(plugins)
    reg.refresh()
    assert reg.resolve("realany") == "realany.real"
    assert reg.resolve("realany", "any") == "realany.any"


def test_ssh_put_op_shape_and_validation():
    from plugins import ssh
    assert "put" in ssh.SshPlugin.ops
    op = ssh.SshPlugin.ops["put"]
    assert op.args == {"data": "blob", "path": "str"}
    assert op.optional_args == {
        "mode": "int",
        "timeout_ms": "int",
        "min_rate_Bps": "int",
    }
    argv = ssh._scp_argv("192.0.2.10", "root", "/k", "/kh",
                         "/tmp/local", "/tmp/fw.bin")
    assert argv[:2] == ["scp", "-O"], argv
    assert "-i" in argv and "/k" in argv, argv

    parsed = plan.load_tar(plan.pack_tar(
        'ssh.target:put data=@fw.bin path="/tmp/fw.bin" '
        'mode=0x1a4 timeout_ms=10000 min_rate_Bps=1000\n',
        {"fw.bin": b"abc"}))
    assert parsed.ops[0].verb == "put"
    assert parsed.ops[0].args["mode"].as_int() == 0o644

    try:
        ssh._validate_remote_path("relative.bin")
    except ValueError as e:
        assert "absolute" in str(e)
    else:
        raise AssertionError("relative ssh:put path should fail")

    try:
        ssh._validate_remote_path("/tmp/bad\nname")
    except ValueError as e:
        assert "newline" in str(e)
    else:
        raise AssertionError("newline in ssh:put path should fail")

    try:
        ssh._check_min_rate("ssh:put", 100, 10.0, 1000)
    except TimeoutError as e:
        assert "too slow" in str(e)
    else:
        raise AssertionError("slow ssh:put should fail")


def test_usb_write_ops_reject_short_writes():
    from plugins import usb

    class FakeDev:
        def ctrl_transfer(self, *args, **kwargs):
            return 1

        def write(self, endpoint, data, timeout=None):
            return 1 if data else 0

    class FakeHandle:
        dev = FakeDev()

        def claim(self, interface=None, detach=False):
            pass

    class FakeSession:
        def bail_if_canceled(self, where):
            pass

        def log_event(self, *args):
            pass

    try:
        usb._op_control(FakeSession(), FakeHandle(), {
            "bmRequestType": 0x00,
            "bRequest": 1,
            "wValue": 0,
            "wIndex": 0,
            "data": b"abc",
            "length": 0,
            "timeout_ms": 1000,
        })
    except IOError as e:
        assert "short OUT" in str(e)
    else:
        raise AssertionError("short USB control OUT should fail")

    class StallingDev(FakeDev):
        def __init__(self):
            self.calls = 0

        def write(self, endpoint, data, timeout=None):
            self.calls += 1
            return 1 if self.calls == 1 else 0

    h = FakeHandle()
    h.dev = StallingDev()
    try:
        usb._op_bulk_write(FakeSession(), h, {
            "endpoint": 0x02,
            "data": b"abc",
            "interface": None,
            "detach": False,
            "timeout_ms": 1000,
        })
    except IOError as e:
        assert "stalled" in str(e)
    else:
        raise AssertionError("short/stalled USB bulk write should fail")


def test_ws_plugin_recv_schema_and_helpers():
    from plugins import ws
    assert "recv" in ws.WsPlugin.ops
    op = ws.WsPlugin.ops["recv"]
    assert op.args == {"bytes": "int", "timeout_ms": "int"}
    assert op.optional_args == {
        "url": "str",
        "expect_sha256": "str",
        "expect_crc32": "int",
        "min_rate_Bps": "int",
        "stream": "bool",
    }
    parsed = plan.load_tar(plan.pack_tar(
        'ws.any:recv url="ws://172.25.0.115:9000/stream" '
        'bytes=1048576 timeout_ms=10000 '
        'expect_sha256="abc" expect_crc32=0x12345678 '
        'min_rate_Bps=25000000 stream=false\n', {}))
    assert parsed.ops[0].device == "ws.any"
    assert parsed.ops[0].verb == "recv"
    assert parsed.ops[0].args["expect_crc32"].as_int() == 0x12345678
    assert ws._fmt_crc32(0x12345678) == "0x12345678"
    class WebSocketTimeoutException(Exception):
        pass
    assert ws._is_ws_timeout(WebSocketTimeoutException())

    try:
        ws._check_min_rate("ws:recv", 100, 10.0, 1000)
    except TimeoutError as e:
        assert "too slow" in str(e)
    else:
        raise AssertionError("slow ws:recv should fail")

    class SlowWebSocketModule:
        @staticmethod
        def create_connection(url, timeout=None):
            time.sleep(1.0)
            raise RuntimeError("should have been canceled first")

    class CanceledSession:
        def bail_if_canceled(self, where):
            raise RuntimeError(f"canceled at {where}")

    try:
        ws._connect_with_cancel(
            SlowWebSocketModule, "ws://example.invalid", 5.0,
            CanceledSession())
    except RuntimeError as e:
        assert "canceled" in str(e)
    else:
        raise AssertionError("ws connect should observe cancel")


# --- runner --------------------------------------------------------------

def main():
    tests = [
        test_parse_basic,
        test_required_device_refs_preserves_concrete_instances,
        test_blob_ref_missing_rejected,
        test_unknown_verb_rejected,
        test_pack_and_load_roundtrip,
        test_session_runs_and_artefact_has_expected_shape,
        test_session_closes_touched_handles_at_job_end,
        test_session_logs_device_use_and_release_for_jobs_only,
        test_lease_only_plan_does_not_count_as_device_usage,
        test_direct_session_without_plan_digest_does_not_log_device_usage,
        test_report_computes_duty_from_device_log_events,
        test_report_pairs_by_session_for_back_to_back_device_jobs,
        test_report_closes_stale_session_at_next_device_use,
        test_report_keeps_log_order_for_equal_timestamps,
        test_report_prints_highest_duty_first,
        test_inventory_returns_devices_and_ops_streams,
        test_server_rest_queue_helpers,
        test_request_path_strips_query_for_static_assets,
        test_static_assets_accept_query_and_disable_cache,
        test_queue_job_rejects_resubmit_while_inflight,
        test_lazy_handle_cache_and_release,
        test_bounded_sizes,
        test_stop_session_clean_termination,
        test_cancel_propagates_to_session,
        test_signal_cancel_sigkills_session_subprocs,
        test_session_watchdog_hard_exits_when_wedged,
        test_acquire_open_timeout_quarantines_and_doesnt_wedge,
        test_refresh_survives_a_hung_probe,
        test_hung_probe_keeps_last_good_specs,
        test_ftd2xx_enumeration_timeout_stays_out_of_process,
        test_fpga_program_cancel_kills_helper_process,
        test_refresh_does_not_evict_pinned,
        test_dispatch_rejects_garbage_plan,
        test_spool_unique_per_attempt,
        test_pending_upload_drain_kick_is_nonblocking,
        test_supervisor_acquires_poller_lock_before_spawning_worker,
        test_refused_spool_409_is_backed_off,
        test_dsp_boot_requires_timeout_and_kills_hung_helper,
        test_dsp_boot_cancel_race_reports_cancel,
        test_dsp_boot_already_canceled_does_not_spawn_helper,
        test_msc_generated_writes_are_reproducible,
        test_msc_write_prbs_inventory_documents_reproduction,
        test_supervisor_heartbeat_stale_uses_monotonic_age,
        test_watchdog_log_records_before_and_after_process_snapshots,
        test_lease_lifecycle,
        test_lease_release_honors_explicit_token_without_resume,
        test_expect_lands_in_manifest,
        test_stream_truncation_marker_survives_multiple_cap_hits,
        test_stream_contains_bytes_incremental_and_cross_record,
        test_stream_contains_bytes_resets_after_truncation,
        test_lease_claim_rejects_lease_pseudo_device,
        test_failure_artefact_carries_identity_fields,
        test_prune_skips_inflight_digests,
        test_stale_cancel_resolution_removes_orphan_running_record,
        test_delete_outputs_no_op_when_nothing_to_delete,
        test_multi_instance_plan_holds_all_dev_locks_for_session,
        test_check_record_lands_in_manifest,
        test_bench_id_defaults_to_hostname_and_env_overrides,
        test_usb_and_usbtmc_plugins_expose_bringup_ops,
        test_usbtmc_fast_path_helpers_read_write_and_rate_check,
        test_usbtmc_and_raw_usb_plan_shapes_parse,
        test_usbtmc_list_available_without_class_nodes,
        test_any_pseudo_device_does_not_make_bare_real_device_ambiguous,
        test_ssh_put_op_shape_and_validation,
        test_usb_write_ops_reject_short_writes,
        test_ws_plugin_recv_schema_and_helpers,
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERR   {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        return 1
    print(f"\nall {len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())

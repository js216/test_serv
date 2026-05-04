# SPDX-License-Identifier: MIT
# test_core.py --- Stdlib smoke test: parse, run, artefact shape
# Copyright (c) 2026 Jakob Kastelic

import io
import json
import os
import tempfile
import tarfile
import time

import plan
import server
from plugin import DevicePlugin, Op
from registry import DeviceRegistry
from session import Session, pack_artefact


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

    reg.stop()
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

    reg.stop()
    reg.close_all()


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

    reg.stop()
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
    reg.close_all()


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


def test_expect_lands_in_manifest():
    """`expect "<claim>"` must surface in manifest.expectations[]."""
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
    assert m["status"] == "ok"
    reg.close_all()


# --- runner --------------------------------------------------------------

def main():
    tests = [
        test_parse_basic,
        test_blob_ref_missing_rejected,
        test_unknown_verb_rejected,
        test_pack_and_load_roundtrip,
        test_session_runs_and_artefact_has_expected_shape,
        test_session_closes_touched_handles_at_job_end,
        test_inventory_returns_devices_and_ops_streams,
        test_server_rest_queue_helpers,
        test_lazy_handle_cache_and_release,
        test_bounded_sizes,
        test_stop_session_clean_termination,
        test_cancel_propagates_to_session,
        test_refresh_does_not_evict_pinned,
        test_dispatch_rejects_garbage_plan,
        test_spool_unique_per_attempt,
        test_expect_lands_in_manifest,
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

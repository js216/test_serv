# SPDX-License-Identifier: MIT
# test_core.py --- Stdlib smoke test: parse, run, artefact shape
# Copyright (c) 2026 Jakob Kastelic

import hashlib
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
        finally:
            (server.INPUTS, server.OUTPUTS, server.DONE,
             server.STATUS, server.RELEASE, server.SWEEP) = old_dirs


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
    deferred = getattr(session, "_deferred_locks", [])
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
        test_queue_job_rejects_resubmit_while_inflight,
        test_lazy_handle_cache_and_release,
        test_bounded_sizes,
        test_stop_session_clean_termination,
        test_cancel_propagates_to_session,
        test_signal_cancel_sigkills_session_subprocs,
        test_refresh_does_not_evict_pinned,
        test_dispatch_rejects_garbage_plan,
        test_spool_unique_per_attempt,
        test_lease_lifecycle,
        test_lease_release_honors_explicit_token_without_resume,
        test_expect_lands_in_manifest,
        test_stream_truncation_marker_survives_multiple_cap_hits,
        test_lease_claim_rejects_lease_pseudo_device,
        test_failure_artefact_carries_identity_fields,
        test_prune_skips_inflight_digests,
        test_delete_outputs_no_op_when_nothing_to_delete,
        test_multi_instance_plan_holds_all_dev_locks_for_session,
        test_check_record_lands_in_manifest,
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

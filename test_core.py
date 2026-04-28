# SPDX-License-Identifier: MIT
# test_core.py --- Stdlib smoke test: parse, run, artefact shape
# Copyright (c) 2026 Jakob Kastelic

import io
import json
import tarfile
import time

import plan
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
    delay ms=1
    mark tag=done
    """
    ops = plan.parse_text(text)
    assert len(ops) == 4, [o.verb for o in ops]
    assert ops[0].device == "fake" and ops[0].verb == "noop"
    assert ops[0].args["k"].as_int() == 42
    assert ops[1].args["data"].as_str() == "hello"
    assert ops[2].verb == "delay"
    assert ops[3].verb == "mark"


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


def test_fork_join_nesting():
    text = """
    fork name=A
      fake:noop k=1
    end
    fork name=B
      fake:noop k=2
    end
    """
    ops = plan.parse_text(text)
    assert len(ops) == 2
    assert ops[0].verb == "fork" and len(ops[0].body) == 1


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
    reg = DeviceRegistry(plugins, ttl_s=0.1)
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
                "errors.log", "streams/foo.bin", "streams/foo.ts",
                "streams/fake.log.bin", "streams/fake.log.ts"}
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
    reg = DeviceRegistry(plugins, ttl_s=60.0)
    reg.refresh()
    key = reg.resolve("fake")

    session = Session(reg, parsed)
    session.run_all(plugins)

    assert fake.opens == 1
    assert fake.closes == 1
    assert key not in reg.cache

    reg.stop()
    reg.close_all()


def test_lazy_handle_ttl_and_release():
    plugins = {"fake": FakePlugin()}
    reg = DeviceRegistry(plugins, ttl_s=0.05)
    reg.refresh()
    key = reg.resolve("fake")
    with reg.acquire(key) as h:
        assert h is not None
    # still cached
    time.sleep(0.02)
    assert key in reg.cache
    # TTL expires
    time.sleep(0.2)
    assert key not in reg.cache or reg.cache[key][3] == 0
    # explicit release
    with reg.acquire(key):
        pass
    ok = reg.release_now(key)
    assert ok or key not in reg.cache
    reg.stop()
    reg.close_all()


def test_bounded_sizes():
    huge = "fake:noop k=0\n" * 10000   # > MAX_OPS
    try:
        plan.parse_text(huge)
    except plan.PlanError as e:
        assert "too many ops" in str(e), e
    else:
        raise AssertionError("expected PlanError for op count")


# --- runner --------------------------------------------------------------

def main():
    tests = [
        test_parse_basic,
        test_blob_ref_missing_rejected,
        test_unknown_verb_rejected,
        test_fork_join_nesting,
        test_pack_and_load_roundtrip,
        test_session_runs_and_artefact_has_expected_shape,
        test_session_closes_touched_handles_at_job_end,
        test_lazy_handle_ttl_and_release,
        test_bounded_sizes,
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

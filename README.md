# test_serv

A hardware-test dispatcher for a multi-device bench. Clients submit a
text job plan (a `.plan` tarball containing `plan.txt` plus any binary
blobs it references). The server queues it; a poller on the bench host
picks it up, walks the plan against a plugin registry, drives hardware,
and posts a single `.tar` artefact back carrying a merged timeline, raw
streams, and a JSON manifest.

### install

The server (`server.py`) and the agent clients (`submit.py`,
`run_md.py`) are pure-stdlib and need no third-party deps. The
poller (`poller.py`) needs hardware drivers; install them on the
bench host:

```
pip install -r requirements.txt
```

Bench-host system tools (not pip):

- `STM32_Programmer_CLI` -- needed by `dfu` plugin if you flash STM32
  parts. Install ST's STM32CubeProgrammer and put the binary on PATH.
- `ssh` client -- needed by `ssh` plugin. Standard OpenSSH works.
- udev rules -- run `sudo sh do.sh` once on Linux to grant non-root
  access to the FTDI / DFU / scope / MSC USB devices the plugins
  drive. The script writes `/etc/udev/rules.d/99-bench-usb.rules`
  and reloads. Replug devices afterwards.

### running

```
python3 server.py [--port 8080]        # server/client host
python3 poller.py                      # bench host
```

`poller.py` must be able to reach `server.py` at `localhost:8080`
from the bench host, typically via an operator-managed SSH tunnel
(`ssh -R 8080:localhost:8080 bench-host`).

### environment

| variable              | purpose                                      | default                                |
|-----------------------|----------------------------------------------|----------------------------------------|
| `TEST_SERV_DIR`       | state dir for inputs/outputs/done/status     | `~/.local/share/test_serv` (POSIX)     |
| `TEST_SERV_CONFIG`    | absolute path to `config.json`               | `$TEST_SERV_DIR/config.json` then repo |
| `TEST_SERV_URL`       | base URL the agent clients post against      | `http://localhost:8080`                |
| `TEST_SERV_BENCH_ID`  | label embedded in every artefact's manifest  | unset (manifest field stays `null`)    |
| `TEST_SERV_PORT`      | port the server binds and the poller connects to | `8080`                             |

### first plan

A trivial inventory plan, end to end:

```
printf 'inventory\n' > /tmp/inventory.txt
python3 submit.py --server http://localhost:8080 /tmp/inventory.txt --extract /tmp/inv --wait 30
cat /tmp/inv/streams/bench.devices.json.bin   # devices the poller can see
cat /tmp/inv/streams/bench.ops.json.bin       # per-plugin op signatures
```

For a full identity-verified sweep before returning the device list,
`POST /sweep` first; `inventory` itself only refreshes the probe.

### submit a job

```
python3 submit.py plan.txt --blob foo.ldr=examples/blink.ldr --wait 30
python3 submit.py job.plan --extract /tmp/out --wait 30
python3 submit.py --fetch DIGEST --extract /tmp/out
```

`submit.py` talks to `TEST_SERV_URL` or `--server` (default
`http://localhost:8080`). Blobs are referenced from the plan as
`@name` where `name` is the `NAME` side of `--blob NAME=PATH`.

### run markdown tests

`run_md.py` reads `TEST.md` if present, otherwise the `### Automated
Test` section of `README.md`. Each fenced plan is submitted through the
same REST API as `submit.py`; bullet checks run against extracted
artefacts in `test_out/`.

```
python3 run_md.py --server http://localhost:8080
python3 run_md.py --fresh --block 0
```

### REST API

Agents can use HTTP directly against the server base URL.

Submit:

```
POST /submit
Content-Type: application/octet-stream

<plan tar OR plain plan.txt body>
```

Server sniffs the body: a packed plan tar (plan.txt + blobs) is queued
as-is; a plain plan.txt body is wrapped in a tar server-side. Response:

```
201 {"status": "queued",        "digest": "<sha256>"}
409 {"status": "duplicate",     "digest": "<sha256>"}
409 {"status": "stale_outputs", "digest": "<sha256>"}
413 {"status": "too_large", "limit": <bytes>}
```

Two optional request headers tune the poller:

```
X-Test-Runtime: 30          # per-session deadline in seconds
X-Test-Upload-Timeout: 30   # artefact-POST timeout in seconds
```

Fetch results:

```
HEAD /outputs/<digest>.tar    # 200 when ready, 404 when pending
GET  /outputs/<digest>.tar    # full artefact
```

After a successful fetch, clean up server-held results:

```
DELETE /outputs/<digest>      # drops outputs AND the job record
```

Cancel a job (queued or in-flight):

```
DELETE /jobs/<digest>
```

Inspect:

```
GET /jobs                     # list every job (queued, running, done)
GET /devices                  # poller's device-probe snapshot
GET /ops                      # bench.ops.json (full plugin/op map)
GET /examples                 # bundled starter plan names
GET /examples/<name>          # fetch one
POST /sweep                   # trigger a probe + verify pass
POST /devices/<id>/release    # drop an idle cached handle
```

### plan grammar (complete)

One op per line. Blank lines and `# comments` ignored.

```
plugin:op k=v k=v ...          # plugin alone, when only one
                               # instance is configured
plugin.id:op k=v k=v ...       # specific instance when several
                               # exist (e.g. mp135.evb / mp135.custom)
plugin.id:open / :close        # pin the handle across ops
ctrl-verb    k=v ...           # control: mark, delay, inventory,
                               # description, expect
```

The `plugin:op` short form errors with `ambiguous: 2 instances` when
the bench has more than one instance of the named plugin. Lease ops
(`lease:claim devices=...`) require fully-qualified `plugin.id`
strings in the device list.

Values are parsed as:

| form                   | type   |
|------------------------|--------|
| `123` / `-5`           | int    |
| `0xCAFE`               | int    |
| `true` / `false`       | bool   |
| `"hello world"`        | string |
| `some_ident`           | ident  |
| `@blob_name`           | blob   |

Control verbs:

- `delay ms=N`
- `mark tag=NAME` -- named checkpoint in the timeline
- `description "<short summary>"` -- label for the dashboard + meta
- `expect "<plain-text claim>"` -- record the human-readable
  *intent* of the plan, surfaced in `manifest.expectations[]`. This
  is descriptive only -- nothing checks it. Machine-checkable
  pass/fail records live in a *separate* list, `manifest.checks[]`,
  populated by ops like `*:uart_expect` (which writes
  `kind=uart_expect, status=hit` on match or `status=timeout` on
  miss). Note: the verb `expect` and the op suffix `*_expect` are
  different things despite sharing the word -- one records intent,
  the other actually waits for bytes. A plan that uses only `expect`
  and never any `*_expect` op (or `scope:capture`, `msc:verify`,
  ...) comes back with `manifest.status: "inert"` -- the run
  proved nothing.
- `inventory` -- return the bench poller's device list and supported
  ops as `bench.devices.json` and `bench.ops.json` files in the
  artefact tar. Always refreshes the device probe; for a full identity
  sweep, `POST /sweep` first.
- `open` / `close` on any device -- pin the handle across multiple
  ops in the same session (avoids paying the open cost per op for
  plugins where setup is expensive, e.g. FT4222 SPI ~100 ms).

Unknown device, op, arg, or arg type is rejected before any hardware
is touched.

### holding a device across plans

The `lease` plugin is the way to keep a device pinned across multiple
plan submissions -- typical use case is an interactive debug window
where each plan reads previous output before deciding what to do
next, or a multi-agent bench where another agent must not grab the
device between two of your submissions.

```
# plan 1: claim dsp.A for ten minutes. The issued token is written
# ONLY to manifest.lease_token in the artefact -- not into a stream
# or timeline event -- so the live /inflight feed doesn't expose it.
lease:claim devices="dsp.A" duration_s=600

# plan 2..N: resume and do work; other agents get fast BusyError if
# they try to acquire dsp.A while the lease is live
lease:resume token="abc1234..."
dsp.A:uart_open
...

# plan N+1: release early (or just let the duration expire)
lease:resume token="abc1234..."
lease:release token="abc1234..."
```

To read the token:
```
python3 submit.py examples/lease_claim.plan --wait 5 --extract /tmp/lease
python3 -c 'import json; print(json.load(open("/tmp/lease/manifest.json"))["lease_token"])'
```

Lease state is in-memory on the poller. A poller restart drops every
live lease (the operator can re-claim). See
`examples/lease_{claim,resume,release}.plan` and
`test_lease_lifecycle` in `test_core.py` for the canonical flow.

### artefact layout

One tarball at `<digest>.tar`. Clients poll completion with
`HEAD /outputs/<digest>.tar` (200 = ready, 404 = pending) and then
`GET` the tar.

```
manifest.json         status, t0_wall_iso, runtime, streams, files,
                      n_ops, n_errors, required_devices, expectations,
                      checks (machine-readable pass/fail), run_id,
                      plan_digest, code_digest, blob_digests,
                      bench_id (from $TEST_SERV_BENCH_ID)
timeline.log          merged human-sortable timeline (each row prefixed
                      by ISO wall-clock time + monotonic offset)
ops.jsonl             one JSON record per op: verb, start, end, status
errors.log            tracebacks, only when something failed
plan.txt              echo of the plan body the poller actually ran
bench.devices.json    poller's device map (only when `inventory` ran)
bench.ops.json        per-plugin op signatures (only when `inventory` ran)
streams/NAME.bin      raw bytes per stream (uart, scope csv, prbs mismatches...)
```

Read `manifest.json` first, then `timeline.log`. Pull `streams/*.bin`
only when raw bytes are needed. `manifest.status` is one of:

- `ok`       -- ran, no errors, at least one machine-checkable assertion
                fired (a `*:uart_expect` hit, etc.)
- `inert`    -- ran, no errors, but the plan made no machine-checkable
                claim. The DUT had no chance to fail because nothing
                was actually checked
- `errors`   -- one or more ops raised
- `failed`   -- the poller refused before the session even started
                (parse error, validation, lease conflict)
- `canceled` -- a DELETE /jobs/<digest> arrived mid-run

### device config

All per-instance parameters -- COM ports, USB VID/PID, FTDI descriptors,
VISA resources, SSH target, cubeprog path, expected identity strings --
live in `config.json`. Search order: `$TEST_SERV_CONFIG`,
`$TEST_SERV_DIR/config.json`, repo-root `config.json`. First hit wins.

Each plugin's `instances` entry can specify either a hard port
(`serial_port`: "COM15") or an `autodetect` rule (`{ "vid": "0x0483",
"pid": "0x3753", "interface": 1 }`). Autodetect follows the board
across Windows re-enumerations; no code edits needed.

### identity verification

On startup the poller performs one full sweep: every probed device is
opened, its plugin runs an identity handshake (`?` reply, `*IDN?`,
`uname -a`, ...) against the expected substring in `config.json`, then
the handle is immediately released. A summary table is printed and
exposed via `/devices`. A REST-triggered `POST /sweep` re-runs the
same sweep at any time. Identity mismatch becomes a clear failure
instead of a run that silently drives the wrong device.

### security model

Agents can submit only typed plans and blobs. The poller rejects unknown
devices, ops, args, and arg types before hardware is touched. There is
no plan syntax for shell commands or filesystem paths.

Security-relevant server endpoints available to agents:

- `POST /submit` queues a typed plan.
- `POST /sweep` asks the poller to re-probe and verify devices.
- `POST /devices/<device-id>/release` asks the poller to close an idle
  cached handle.

SSH access, when enabled (any non-empty `ssh.instances` in
`config.json`), exposes `ssh:exec command="..."` — a free-form shell
command run on the configured target as the configured user (root in
the shipped config). For an R&D bench this is intentional: plans
already grant arbitrary device control, and a shell on the system
under test is part of the same trust boundary. But submitting a plan
is then equivalent to interactive root SSH on every box listed under
`ssh.instances`. Treat the SSH tunnel correspondingly trusted, and
remove `ssh.instances` entirely on benches where this is too broad.

### adding a device

1. Drop a new file into `plugins/` implementing `DevicePlugin` with an
   `ops` dict of `plugin.Op` entries. Each `Op` has an `args` schema
   (`{name: type_name}`) and a `run(session, handle, args)` callable.
2. Implement `probe()` (side-effect-free enumeration), `open(spec)`,
   `close(handle)`.
3. `kill -HUP $(pgrep -f poller.py)` or restart. `/ops` updates
   automatically; this README is not touched.

### releasing a device without restarting

Use the device id from `inventory` or `bench.devices.json`:

```
curl -X POST http://localhost:8080/devices/dsp.A/release
```

This closes the handle only if no job is currently using it. The next
job reopens the device.

### Automated Test

`run_md.py` looks for this heading by default, runs the fenced plan(s)
below it through the same REST API as `submit.py`, and checks each
bullet against the resulting artefact via a sibling `verify.py`. The
shipped block is a smoke test:

```
description "smoke: probe the bench"
inventory
```

- `bench.devices.json`

Run with `python3 run_md.py --server http://localhost:8080`. The
shipped `verify.py` treats each bullet's first token as a path and
passes if that file is in the artefact -- enough to fail loudly if the
poller didn't post something usable, but no more. Replace it for real
bench tests (manifest status checks, scope.summary thresholds, ...).

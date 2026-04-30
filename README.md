# test_serv

A hardware-test dispatcher for a multi-device bench. Clients submit a
text job plan (a `.plan` tarball containing `plan.txt` plus any binary
blobs it references). The server queues it; a poller on the bench host
picks it up, walks the plan against a plugin registry, drives hardware,
and posts a single `.tar` artefact back carrying a merged timeline, raw
streams, and a JSON manifest.

### agent workflow

Use `submit.py --server URL` or set `TEST_SERV_URL`.

Bench discovery is a normal job:

```
printf 'inventory\n' > /tmp/inventory.txt
python3 submit.py --server http://localhost:8080 /tmp/inventory.txt --extract /tmp/test-serv-inventory --wait 30
cat /tmp/test-serv-inventory/streams/bench.devices.json.bin
cat /tmp/test-serv-inventory/streams/bench.ops.json.bin
```

Use `inventory verify=true` to make the bench poller run an identity
sweep before returning the device list. That can take longer.

### running

```
python3 server.py [--port 8080]        # server/client host
python3 poller.py                      # bench host
```

`poller.py` must be able to reach `server.py` at `localhost:8080`
from the bench host, typically via an operator-managed tunnel.

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

Submit a packed `.plan` tar:

```
POST /submit
Content-Type: application/octet-stream

<plan tar bytes>
```

Response:

```
201 {"status": "queued", "digest": "<sha256>"}
409 {"status": "duplicate", "digest": "<sha256>"}
409 {"status": "stale_outputs", "digest": "<sha256>"}
```

Optional request headers become job metadata for the poller:

```
X-Test-Runtime: 30
X-Test-Anything: value
```

Fetch results:

```
GET /outputs/<digest>.txt    # manifest/completion sentinel
GET /outputs/<digest>.tar    # full artefact
```

Both return `404` until the poller has posted that output. After a
successful fetch, clean up server-held results:

```
DELETE /outputs/<digest>
```

Discovery helpers:

```
GET /examples
GET /examples/<name>
GET /scope/signals
POST /sweep
POST /devices/<device-id>/release
```

### discover what is available

For bench hardware and bench-supported ops, use the inventory job shown
above. It returns the authoritative poller-side view.

Local server helpers:

```
curl http://localhost:8080/examples       # starter plan names
curl http://localhost:8080/examples/NAME  # fetch one
curl http://localhost:8080/scope/signals  # channel -> {name, active_below} map
```

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

### plan grammar (complete)

One op per line. Blank lines and `# comments` ignored.

```
device:op k=v k=v ...          # device op, args typed per plugin
ctrl-verb    k=v ...           # control: barrier, mark, delay,
                               # wall_time, inventory, fork, end, join,
                               # open, close
```

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
- `mark tag=NAME` / `barrier tag=NAME` -- checkpoints in the timeline
- `wall_time` -- return the bench poller's wall clock as
  `bench.time.json` in the artefact.
- `inventory [refresh=true] [verify=false]` -- return the bench
  poller's device list and supported ops as `bench.devices.json` and
  `bench.ops.json` streams in the artefact. `refresh=false` skips the
  pre-snapshot probe; `verify=true` runs the identity sweep first.
- `fork name=IDENT` ... `end` -- run the enclosed op list in a thread;
  a plan can contain several `fork` blocks, each joined at its `end`.
- `open` / `close` on any device -- pin or release its handle for the
  remainder of the session, overriding the default lazy acquire.

Unknown device, op, arg, or arg type is rejected before any hardware
is touched.

### artefact layout

One tarball posted as `<digest>.tar`, plus a short `<digest>.txt`
(the manifest JSON) used as the completion sentinel.

```
manifest.json         status, streams list, runtime, n_ops, n_errors
timeline.log          merged human-sortable timeline of events + streams
ops.jsonl             one JSON record per op: verb, start, end, status
errors.log            tracebacks, only when something failed
streams/NAME.bin      raw bytes per stream (uart, scope csv, prbs mismatches...)
streams/NAME.ts       binary index: (<d t_s>,<u4 len>) records per stream
```

Read `manifest.json` first, then `timeline.log`. Pull `streams/*.bin`
only when raw bytes are needed.

### security model

Agents can submit only typed plans and blobs. The poller rejects unknown
devices, ops, args, and arg types before hardware is touched. There is
no plan syntax for shell commands or filesystem paths.

Security-relevant server endpoints available to agents:

- `POST /submit` queues a typed plan.
- `POST /sweep` asks the poller to re-probe and verify devices.
- `POST /devices/<device-id>/release` asks the poller to close an idle
  cached handle.

SSH access, when enabled, is exposed only as fixed plugin ops such as
`ssh:run_uname`; there is no free-form SSH command op.

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

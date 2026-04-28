# test_serv

A hardware-test dispatcher for a multi-device bench. Clients submit a
text job plan (a `.plan` tarball containing `plan.txt` plus any binary
blobs it references). The server queues it; a poller on the bench host
picks it up, walks the plan against a plugin registry, drives hardware,
and posts a single `.tar` artefact back carrying a merged timeline, raw
streams, and a JSON manifest.

### agent workflow

Agents run on the server/client host, not the bench. Use `submit.py`;
do not assume local `curl /devices` or `curl /ops` reflects the bench.
Bench discovery is a normal job:

```
printf 'inventory\n' > /tmp/inventory.txt
python3 submit.py /tmp/inventory.txt --extract /tmp/test-serv-inventory --wait 30
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

The first form packs `plan.txt` + blobs into a tarball client-side.
The second submits an already-packed `.plan` tarball. Blobs are
referenced from the plan as `@name` where `name` is the `NAME` side of
`--blob NAME=PATH`.

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

The protocol is a finite-opcode TLV-style grammar; there is no code
execution path. Attackers with submit access can only issue ops that
a plugin has registered. Specifically:

- Unknown ops / args / devices rejected at parse.
- Payload, blob, op count bounded.
- No filesystem paths in the grammar; all binary data travels inline.
- SSH is exposed only as per-command ops (`ssh:run_<whitelisted>`)
  with typed placeholders, a pinned key, a pinned `known_hosts`, and
  a fixed target IP. Run the poller under a user whose outbound
  firewall permits only that IP on port 22.
- `server.py` binds `127.0.0.1`.

### adding a device

1. Drop a new file into `plugins/` implementing `DevicePlugin` with an
   `ops` dict of `plugin.Op` entries. Each `Op` has an `args` schema
   (`{name: type_name}`) and a `run(session, handle, args)` callable.
2. Implement `probe()` (side-effect-free enumeration), `open(spec)`,
   `close(handle)`.
3. `kill -HUP $(pgrep -f poller.py)` or restart. `/ops` updates
   automatically; this README is not touched.

### releasing a device without restarting

A running poller holds device handles only for the duration of an op,
plus a 5 s TTL afterwards. To grab a port (e.g. open PuTTY on the DSP
UART while the bench is idle):

```
curl -X POST http://localhost:8080/devices/dsp.A/release
```

The poller drops the cached handle immediately; the next job reopens.

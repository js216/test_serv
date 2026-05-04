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

For a full identity-verified sweep before returning the device list,
`POST /sweep` first; `inventory` itself only refreshes the probe.

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

### environment

| variable           | purpose                                      | default                                |
|--------------------|----------------------------------------------|----------------------------------------|
| `TEST_SERV_DIR`    | state dir for inputs/outputs/done/status     | `<tmp>/test_serv-<user>`               |
| `TEST_SERV_CONFIG` | absolute path to `config.json`               | `$TEST_SERV_DIR/config.json` then repo |
| `TEST_SERV_URL`    | base URL the agent clients post against      | `http://localhost:8080`                |

### running

```
python3 server.py [--port 8080]        # server/client host
python3 poller.py                      # bench host
```

`poller.py` must be able to reach `server.py` at `localhost:8080`
from the bench host, typically via an operator-managed SSH tunnel
(`ssh -R 8080:localhost:8080 bench-host`).

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
DELETE /outputs/<digest>
```

Discovery helpers:

```
GET /examples
GET /examples/<name>
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
ctrl-verb    k=v ...           # control: mark, delay, inventory,
                               # description, open, close
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
- `mark tag=NAME` -- named checkpoint in the timeline
- `description "<short summary>"` -- label for the dashboard + meta
- `inventory` -- return the bench poller's device list and supported
  ops as `bench.devices.json` and `bench.ops.json` streams in the
  artefact. Always refreshes the device probe; for a full identity
  sweep, `POST /sweep` first.
- `open` / `close` on any device -- pin the handle across multiple
  ops in the same session (avoids paying the open cost per op for
  plugins where setup is expensive, e.g. FT4222 SPI ~100 ms).

Unknown device, op, arg, or arg type is rejected before any hardware
is touched.

### artefact layout

One tarball at `<digest>.tar`. Clients poll completion with
`HEAD /outputs/<digest>.tar` (200 = ready, 404 = pending) and then
`GET` the tar.

```
manifest.json         status, streams list, runtime, n_ops, n_errors
timeline.log          merged human-sortable timeline of events + streams
ops.jsonl             one JSON record per op: verb, start, end, status
errors.log            tracebacks, only when something failed
streams/NAME.bin      raw bytes per stream (uart, scope csv, prbs mismatches...)
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

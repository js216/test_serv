# test_serv

A minimal hardware-test dispatcher: a submitter drops a job file into a
shared directory, the hardware-side harness polls for jobs matching its
kind, runs them, and posts the result artefacts back.  Jobs are routed
by file extension, so a single server handles any mix of hardware kinds
(`.ldr` for DSP firmware, `.bit` for FPGA bitstreams, etc.) with no code
changes.

### State directory

All state lives under `$TEST_SERV_DIR` (default
`/tmp/test_serv-$USER/`), created `0700` on first run:

    $TEST_SERV_DIR/
        inputs/      submit.py writes here
        outputs/     submit.py reads here
        done/        dispatched jobs rotated here

Nothing lives inside the checkout, so the server is relocatable.

### Running the server

    python3 server.py --port 8080

Listens on `127.0.0.1:8080`.  Pick another port to run a second
instance alongside the primary one.

### Submitting a job

    python3 submit.py [--runtime SEC] [--wait SEC] [--expected FILE] PATH

The job's kind is its file extension: `submit.py foo.ldr` drops a
`.ldr` job, `submit.py bar.bit` a `.bit` job.

Without `--wait`, submit.py returns as soon as the job is queued and
prints the job's SHA-256 digest to stdout -- fire-and-forget mode, for
pipelines that harvest results later by digest.  With `--wait N`, it
additionally blocks up to N seconds, then streams the returned
artefacts to stdout (see *Output stream* below) and clears them from
`outputs/`.

Common invocations:

    submit.py main.ldr                         # submit, print digest, exit
    submit.py --wait 60 main.ldr               # submit and block up to 60s
    submit.py --runtime 30 --wait 60 main.ldr  # capture for 30s, wait up to 60s
    submit.py --wait 30 --expected ref.txt a.ldr   # byte-exact check vs ref.txt

### Hardware harness protocol

Each harness polls its own extension:

    GET /ldr    # returns a random *.ldr payload, 204 if none
    GET /bit    # ditto for *.bit
    GET /<ext>  # ditto for any extension

The response carries per-job parameters as `X-Test-<Key>` headers
derived from the submitter's `--runtime` and `--meta KEY=VAL` flags
(e.g. `X-Test-Runtime: 30`).  After capturing, the harness POSTs each
artefact back:

    POST /<digest>.csv    # scope trace (any non-.txt artefact, any count)
    POST /<digest>.txt    # UART text log -- MUST be last, it is the sentinel

The `.txt` is the completion marker; by convention the harness posts all
other artefacts first so that by the time the submitter sees the
`.txt`, every sibling file is already on disk.

### Output stream

With `--wait`, submit.py streams every `outputs/{digest}.*` to stdout,
framed by header lines and with the `.txt` last:

    === abc123.csv ===
    timestamp,ch1,ch2
    0.000,0.12,-0.05
    ...
    === abc123.txt ===
    OK test_if
    OK test_while
    0x55 pass 0x2 fail

Redirect to a file for a complete run record; leave it on a tty and the
coloured pass/fail summary lands at the cursor.  All matching files are
removed from `outputs/` after dumping.

### Per-run parameters

`--runtime SEC` is shorthand for `--meta runtime=SEC`; both write a
`{digest}.{ext}.meta` sidecar with `key=value` lines.  The server
emits each key as an `X-Test-<Key>` response header.  Adding new keys
is zero-code on the server side -- the harness just reads whatever
`X-Test-*` headers it cares about.

### Ordering guarantee

Jobs appear to the server only once they are fully on disk.  submit.py
writes the `.meta` first (fsynced), copies the payload to a
`{digest}.{ext}.inprogress` tempfile, then atomically renames it to
`{digest}.{ext}`.  The server filters by the URL-derived extension, so
`.inprogress` and `.meta` files are never dispatched.

### Author

Jakob Kastelic (Stanford Research Systems)

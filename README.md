# test_serv

A minimal hardware-test dispatcher: a submitter drops a job file into a
shared directory, the hardware-side harness polls for jobs matching its
kind, runs them, and posts the result artefacts back.  Jobs are routed
by file extension, so a single server handles any mix of hardware kinds
(`.ldr` for DSP firmware, `.bin` for iCE40 FPGA bitstreams, `.qspi` for
post-boot QSPI link tests, etc.) with no code changes.

### Running the server

    python3 server.py [--port PORT]

Listens on `127.0.0.1`.  Default port is 8080; pick another to run a
second instance alongside the primary one.

### Submitting a job

    python3 submit.py [FLAGS] PATH
    python3 submit.py --fetch DIGEST [--expected FILE]

Flags:

    --wait SEC          block up to SEC seconds for results, then stream
                        them to stdout (each framed by a '=== name ===' line,
                        with .txt last) and clean outputs/
    --runtime SEC       capture duration the harness honours (sent as
                        X-Test-Runtime)
    --meta KEY=VAL      extra sidecar field, repeatable; emitted by the
                        server as X-Test-<Key>
    --expected FILE     after returning results, compare the .txt byte-for-
                        byte against FILE; exit 0 on SUCCESS, 1 on FAIL
    --fetch DIGEST      dump and clean up outputs for a previously-submitted
                        digest; use after a fire-and-forget submit
    --out DIR           move non-.txt artefacts (e.g. .bin, .csv) into DIR
                        instead of deleting them after summarizing; required
                        whenever you want to keep the raw bytes, since binary
                        artefacts are never dumped to stdout

Without `--wait`, submit.py returns as soon as the job is queued and
prints the SHA-256 digest to stdout (fire-and-forget).  The job's kind
is its file extension: `submit.py foo.ldr` drops a `.ldr` job,
`submit.py bar.bin` a `.bin` job.

Results from a fire-and-forget submit must be collected before the same
job can be resubmitted.  If `outputs/{digest}.*` is non-empty at submit
time, submit.py refuses with exit code 2 and a pointer to `--fetch`.

### Hardware harness protocol

Each harness polls its own extension:

    GET /ldr    # returns a random *.ldr payload, 204 if none
    GET /bin    # ditto for *.bin
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

### Post-boot QSPI link tests (`.qspi`)

Once the DSP has booted (via an `.ldr` job), the FT4222 QSPI master can be
re-used for link-quality and throughput tests.  The poller exposes a thin
opcode executor: the server-side test author builds a binary TLV stream
describing init + a sequence of primitive SPI operations, the poller runs
them against whatever post-boot firmware the DSP exposes on its SPI slave,
and returns captured MISO bytes plus a per-op timing log.

The poller has no test semantics of its own beyond one shared PRBS generator
(32-bit xorshift) that both it and the DSP firmware must implement
identically, so large patterns can be described by a seed + length rather
than by embedding the bytes in the job file.

See `examples/make_qspi.py` for the TLV grammar and helper constructors,
and `poller.py :: QspiHandler` for the canonical opcode table.

While the opcode script runs, the DSP UART is drained in a background
thread and returned as a `.uart` artefact, so any diagnostic prints the
firmware emits during a PRBS stream are captured in the same job.

### Author

Jakob Kastelic (Stanford Research Systems)

import sys
import time
import struct
import threading
import traceback
import ft4222
import serial
import serial.tools.list_ports
import urllib.request
import hashlib
from datetime import datetime
import pyvisa
import csv
import io
import numpy as np
import ftd2xx


class Remote:
    def __init__(self, port=8080):
        self.port = port

    def get_job(self, kind):
        r = urllib.request.urlopen(f"http://localhost:{self.port}/{kind}")
        if r.status == 204:
            return None, {}
        return r.read(), dict(r.headers)

    def post_resp(self, job_id, msg, suffix=""):

        if isinstance(msg, str):
            msg_bytes = msg.encode()
        else:
            msg_bytes = msg

        if suffix:
            url = f"http://localhost:{self.port}/{job_id}{suffix}"
        else:
            url = f"http://localhost:{self.port}/{job_id}"

        urllib.request.urlopen(
            urllib.request.Request(
                url,
                data=msg_bytes,
                method="POST"
            )
        ).read()


class Expander:
    """
    ADP5587ACPZ-1 pinout
    =====================================
    C0 = USBI_SPI0_EN*
    C1 = USBI_SPI1_EN*
    C2 = USB_QSPI_EN*
    C3 = USB_QSPI_RESET*
    C4 = ETH0_RESET
    C5 = ADAU1372_PWRDWN*
    C6 = PUSHBUTTON_EN
    C7 = DS8 (green LED on SOMCRR)
    C8 = DS7 (green LED on SOMCRR)
    C9 = DS6 (green LED on SOMCRR)
    """

    def wr(self, dev, d):
        for a, b, c in d:
            dev.i2cMaster_WriteEx(
                a, ft4222.I2CMaster.Flag.START_AND_STOP, bytes([b, c]))

    def exp_init(self, ):
        dev = ft4222.openByDescription('FT4222 A')
        dev.i2cMaster_Init(100)

        self.wr(dev, [(0x30, 0x1D, 0x00)]) # R7:0 -> GPIO
        self.wr(dev, [(0x30, 0x1E, 0x00)]) # C7:0 -> GPIO
        self.wr(dev, [(0x30, 0x1F, 0x00)]) # C9:8 -> GPIO

        # `USBI_SPI0,1` disabled, `USB_QSPI` neither enabled nor under reset,
        # `ETH0` reset asserted, `ADAU1372` powered down, pushbutton disabled,
        # LEDs off.
        self.wr(dev, [(0x30, 0x18, 0b1001_1111)])
        self.wr(dev, [(0x30, 0x19, 0b0000_0011)])

        self.wr(dev, [(0x30, 0x24, 0xff)]) # C7:0 -> output
        self.wr(dev, [(0x30, 0x25, 0xff)]) # C9:8 -> output

        self.wr(dev, [(0x30, 0x18, 0b1001_1011)]) # enable QSPI

        dev.close()
        
    def blink_led(self, led):
        dev = ft4222.openByDescription('FT4222 A')
        dev.i2cMaster_Init(100)

        if led == "DS8":
            self.wr(dev, [(0x30, 0x18, 0b0001_1011)])
        elif led == "DS7":
            self.wr(dev, [(0x30, 0x19, 0b0000_0010)])
        elif led == "DS6":
            self.wr(dev, [(0x30, 0x19, 0b0000_0001)])

        time.sleep(0.25)

        if led == "DS8":
            self.wr(dev, [(0x30, 0x18, 0b1001_1111)])
        elif led == "DS7":
            self.wr(dev, [(0x30, 0x19, 0b0000_0011)])
        elif led == "DS6":
            self.wr(dev, [(0x30, 0x19, 0b0000_0011)])

        dev.close()
        
    def reset(self):
        # LED DS8 repurposed as eval board reset control
        self.blink_led("DS8")


class QSPI:
    def load_file(self, fname):
        boot_buffer = bytearray([0x03])
        with open(fname, 'r') as f:
            for line in f:
                boot_buffer.append(int(line.strip(), 16))
        self.load_stream(boot_buffer)

    def accept_ldr(self, ldr):
        boot_buffer = bytearray([0x03])
        for line in ldr.split():
            boot_buffer.append(int(line.strip(), 16))
        self.load_stream(boot_buffer)

    def _write_chunked(self, dev, buf):
        CHUNK = 1024

        # pad to multiple of 1024
        n = len(buf)
        padded_len = ((n + CHUNK - 1) // CHUNK) * CHUNK
        if padded_len != n:
            buf += bytes(padded_len - n)

        # keep CS LOW across entire transfer
        for i in range(0, padded_len, CHUNK):
            last = (i + CHUNK) >= padded_len
            dev.spiMaster_SingleWrite(buf[i:i+CHUNK], last)

    def load_stream(self, boot_buffer):
        dev = ft4222.openByDescription('FT4222 A')
        dev.spiMaster_Init(
            ft4222.SPIMaster.Mode.SINGLE,
            ft4222.SPIMaster.Clock.DIV_8,
            ft4222.SPI.Cpol.IDLE_LOW,
            ft4222.SPI.Cpha.CLK_TRAILING,
            ft4222.SPIMaster.SlaveSelect.SS0)

        self._write_chunked(dev, boot_buffer)

        dev.close()


# PRBS generator: 32-bit xorshift. Must be bit-identical on DSP side.
# Initial state = seed (must be non-zero). Each call updates state by:
#     x ^= x << 13; x ^= x >> 17; x ^= x << 5;
# Then emits the low 8 bits of the new state as the next output byte.
def prbs_xorshift32(seed, nbytes):
    x = seed & 0xFFFFFFFF
    if x == 0:
        x = 0x1
    out = bytearray(nbytes)
    for i in range(nbytes):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17) & 0xFFFFFFFF
        x ^= (x << 5)  & 0xFFFFFFFF
        out[i] = x & 0xFF
    return bytes(out)


class QspiTest:
    """Server-driven QSPI test executor.

    Input job file is a TLV stream; see handler docstring for the grammar.
    The poller has no test semantics beyond the per-op FT4222 calls and
    a bit-exact PRBS generator shared with the DSP firmware.
    """

    MODE_MAP = {
        1: ft4222.SPIMaster.Mode.SINGLE,
        2: ft4222.SPIMaster.Mode.DUAL,
        4: ft4222.SPIMaster.Mode.QUAD,
    }
    CLK_MAP = {
        0: ft4222.SPIMaster.Clock.DIV_2,
        1: ft4222.SPIMaster.Clock.DIV_4,
        2: ft4222.SPIMaster.Clock.DIV_8,
        3: ft4222.SPIMaster.Clock.DIV_16,
        4: ft4222.SPIMaster.Clock.DIV_32,
        5: ft4222.SPIMaster.Clock.DIV_64,
        6: ft4222.SPIMaster.Clock.DIV_128,
        7: ft4222.SPIMaster.Clock.DIV_256,
        8: ft4222.SPIMaster.Clock.DIV_512,
    }
    # FT4222 system clock options; max slave SCK scales with this
    # (datasheet Table 4.2: 80 MHz -> 20 MHz SCK, 60 -> 15, 48 -> 12, 24 -> 6).
    SYS_CLK_MAP = {
        0: "SYS_CLK_24",
        1: "SYS_CLK_48",
        2: "SYS_CLK_60",
        3: "SYS_CLK_80",
    }

    def _cpol_cpha(self, flags):
        cpol = (ft4222.SPI.Cpol.IDLE_HIGH if (flags & 0x1)
                else ft4222.SPI.Cpol.IDLE_LOW)
        cpha = (ft4222.SPI.Cpha.CLK_LEADING if (flags & 0x2)
                else ft4222.SPI.Cpha.CLK_TRAILING)
        return cpol, cpha

    def _init_master(self, dev, clk, mode, flags):
        cpol, cpha = self._cpol_cpha(flags)
        # Bump operating clock to 80 MHz so CLK_DIV_2 yields 40 MHz
        # SCK -- the FT4222 datasheet peak (§4, Table 4.1).  Default
        # OpClk 60 MHz caps SCK at 30 MHz.  Harmless at lower clk
        # divisors; the SCK table still divides OpClk by the same
        # ratio.
        dev.setClock(ft4222.SysClock.CLK_80)
        dev.spiMaster_Init(
            self.MODE_MAP[mode],
            self.CLK_MAP[clk],
            cpol,
            cpha,
            ft4222.SPIMaster.SlaveSelect.SS0,
        )

    def _init_slave(self, dev, flags):
        # FT4222 slave is single-bit only (datasheet line 1187). Raw mode
        # (NO_PROTOCOL) passes bytes through without the library's ack
        # framing -- we want direct byte-stream access to what the DSP
        # shifts in / out.
        dev.spiSlave_InitEx(ft4222.SPISlave.IoProtocol.NO_PROTOCOL)
        cpol, cpha = self._cpol_cpha(flags)
        dev.spiSlave_SetMode(cpol, cpha)

    def _slave_drain(self, dev, nbytes, timeout_ms):
        """Block until ``nbytes`` have landed in the FT4222 slave RX FIFO
        or ``timeout_ms`` elapses, whichever first. Returns what we got.

        FT4222 slave RX FIFO is backed by the shared 4160 B endpoint SRAM
        (datasheet line 1009), so long streams must be drained faster
        than the DSP master fills them.
        """
        deadline = time.time() + timeout_ms / 1000.0
        got = bytearray()
        while len(got) < nbytes and time.time() < deadline:
            avail = dev.spiSlave_GetRxStatus()
            if avail <= 0:
                time.sleep(0.0005)
                continue
            want = min(avail, nbytes - len(got))
            got += bytes(dev.spiSlave_Read(want))
        return bytes(got)

    def _set_sys_clk(self, sys_clk):
        # sys_clk is a slave-role constraint: it caps the maximum SCK
        # the FT4222 can follow as a slave (datasheet Table 4.2).
        # Master-role callers can ignore it, which is why run() below
        # only invokes this for role == 1.
        #
        # The enum and setter live under slightly different names
        # across pyft4222 releases. Try the known paths, then fall
        # back to introspecting the module -- and if that still
        # fails, dump what's actually available so the next fix can
        # name the right attribute without another round-trip.
        name = self.SYS_CLK_MAP[sys_clk]

        rate_enum = None
        for attr in ("ClockRate", "SysClock", "Clock"):
            ns = getattr(ft4222, attr, None)
            if ns is not None and hasattr(ns, name):
                rate_enum = getattr(ns, name)
                break
        if rate_enum is None:
            for top in dir(ft4222):
                ns = getattr(ft4222, top, None)
                if hasattr(ns, name):
                    rate_enum = getattr(ns, name)
                    break
        if rate_enum is None:
            clockish = [n for n in dir(ft4222)
                        if "CLK" in n.upper() or "CLOCK" in n.upper()]
            raise RuntimeError(
                f"pyft4222 has no system clock enum exposing {name}; "
                f"ft4222 attrs matching CLK/CLOCK: {clockish}; "
                f"full dir(ft4222)={dir(ft4222)}"
            )

        setter = (getattr(ft4222, "setClock", None)
                  or getattr(ft4222, "setSysClock", None))
        if setter is None:
            setterish = [n for n in dir(ft4222) if "et" in n and "lock" in n]
            raise RuntimeError(
                f"pyft4222 has no setClock/setSysClock; "
                f"ft4222 attrs matching *et*lock*: {setterish}"
            )
        setter(rate_enum)

    def run(self, payload, ser=None, uart_buf=None):
        if payload[:4] != b"QSPI":
            raise ValueError("bad magic")
        ver = payload[4]
        if ver == 1:
            clk, mode, flags = struct.unpack("<BBB", payload[5:8])
            role = 0
            sys_clk = None
            hdr_len = 8
        elif ver == 2:
            role, sys_clk, clk, mode, flags = struct.unpack(
                "<BBBBB", payload[5:10])
            hdr_len = 10
        else:
            raise ValueError(f"unsupported qspi job version {ver}")

        if role == 1 and mode != 1:
            raise ValueError("slave mode requires mode=1 (SINGLE); "
                             "FT4222 slave is single-bit only")

        read_bin = bytearray()
        log = []
        early_done = False
        log.append(
            f"init role={'slave' if role else 'master'} "
            f"sys_clk={sys_clk} clk_div={clk} mode={mode} "
            f"flags=0x{flags:02x}"
        )

        # sys_clk only matters when the FT4222 is the slave (it caps
        # the SCK it can track, per datasheet Table 4.2). For master
        # role it is cosmetic, so skip the call and avoid an
        # unnecessary dependency on the pyft4222 clock-enum naming.
        if sys_clk is not None and role == 1:
            self._set_sys_clk(sys_clk)

        dev = ft4222.openByDescription('FT4222 A')
        t0 = time.time()
        try:
            if role == 0:
                self._init_master(dev, clk, mode, flags)
            else:
                self._init_slave(dev, flags)

            pos = hdr_len
            op_idx = 0
            while pos < len(payload):
                tag = payload[pos]
                ln = struct.unpack("<I", payload[pos+1:pos+5])[0]
                body = payload[pos+5:pos+5+ln]
                pos += 5 + ln
                op_idx += 1

                # Master opcodes 0x01..0x09 require master role; slave
                # opcodes 0x20..0x23 require slave role; the rest are
                # role-neutral (delay_us, reinit, uart_tx).
                if 0x01 <= tag <= 0x09 and role != 0:
                    raise ValueError(
                        f"master opcode 0x{tag:02x} not allowed in "
                        f"slave-role session")
                if 0x20 <= tag <= 0x23 and role != 1:
                    raise ValueError(
                        f"slave opcode 0x{tag:02x} not allowed in "
                        f"master-role session")

                top = time.time()
                if tag == 0x01:   # write
                    # Chunk with CS held: pyft4222 caps a single
                    # SingleWrite call at 65535 B.  Bootloader uses
                    # the same pattern to stream large payloads.
                    raw = bytes(body)
                    CHUNK = 16384
                    for off in range(0, len(raw), CHUNK):
                        last = (off + CHUNK) >= len(raw)
                        dev.spiMaster_SingleWrite(raw[off:off+CHUNK], last)
                    dt = time.time() - top
                    log.append(f"[{op_idx}] write {len(body)}B  {dt*1e3:.2f} ms "
                               f"{len(body)/dt/1e6:.2f} MB/s" if dt > 0
                               else f"[{op_idx}] write {len(body)}B")
                elif tag == 0x02:   # read
                    n = struct.unpack("<I", body[:4])[0]
                    got = dev.spiMaster_SingleRead(n, True)
                    read_bin += bytes(got)
                    dt = time.time() - top
                    log.append(f"[{op_idx}] read {n}B  {dt*1e3:.2f} ms "
                               f"{n/dt/1e6:.2f} MB/s" if dt > 0
                               else f"[{op_idx}] read {n}B")
                elif tag == 0x03:   # xfer (single-lane full-duplex)
                    got = dev.spiMaster_SingleReadWrite(bytes(body), True)
                    read_bin += bytes(got)
                    dt = time.time() - top
                    log.append(f"[{op_idx}] xfer {len(body)}B  {dt*1e3:.2f} ms")
                elif tag == 0x04:   # delay_us
                    us = struct.unpack("<I", body[:4])[0]
                    time.sleep(us / 1e6)
                    log.append(f"[{op_idx}] delay {us} us")
                elif tag == 0x05:   # reinit
                    nclk, nmode, nflags, _rsv = struct.unpack("<BBBB", body[:4])
                    if role == 0:
                        self._init_master(dev, nclk, nmode, nflags)
                    else:
                        if nmode != 1:
                            raise ValueError(
                                "slave reinit requires mode=1 (SINGLE)")
                        self._init_slave(dev, nflags)
                    log.append(f"[{op_idx}] reinit clk_div={nclk} "
                               f"mode={nmode} flags=0x{nflags:02x}")
                elif tag == 0x06:   # mixed_xfer (flash-style frame)
                    ns, nw = struct.unpack("<HH", body[:4])
                    nr = struct.unpack("<I", body[4:8])[0]
                    sbuf = bytes(body[8:8+ns])
                    mbuf = bytes(body[8+ns:8+ns+nw])
                    got = dev.spiMaster_MultiReadWrite(sbuf, mbuf, nr)
                    if got:
                        read_bin += bytes(got)
                    dt = time.time() - top
                    log.append(f"[{op_idx}] mixed_xfer ns={ns} nw={nw} nr={nr}  "
                               f"{dt*1e3:.2f} ms")
                elif tag == 0x07:   # write_prbs
                    seed, n = struct.unpack("<II", body[:8])
                    buf = prbs_xorshift32(seed, n)
                    # pyft4222 rejects single-call buffers larger
                    # than ~32 KiB with INVALID_POINTER.  Chunk the
                    # write the same way the bootloader does: keep
                    # CS low by passing isEndTransaction=False until
                    # the final chunk.
                    CHUNK = 16384
                    total = len(buf)
                    for off in range(0, total, CHUNK):
                        last = (off + CHUNK) >= total
                        dev.spiMaster_SingleWrite(buf[off:off+CHUNK], last)
                    dt = time.time() - top
                    log.append(f"[{op_idx}] write_prbs seed=0x{seed:08x} {n}B  "
                               f"{dt*1e3:.2f} ms "
                               f"{n/dt/1e6:.2f} MB/s" if dt > 0 else
                               f"[{op_idx}] write_prbs seed=0x{seed:08x} {n}B")
                elif tag == 0x08:   # read_verify_prbs
                    seed, n = struct.unpack("<II", body[:8])
                    got = bytes(dev.spiMaster_SingleRead(n, True))
                    expected = prbs_xorshift32(seed, n)
                    dt = time.time() - top
                    if got == expected:
                        log.append(f"[{op_idx}] read_verify_prbs "
                                   f"seed=0x{seed:08x} {n}B OK  "
                                   f"{dt*1e3:.2f} ms")
                    else:
                        mism = sum(1 for a, b in zip(got, expected) if a != b)
                        first = next((i for i, (a, b) in
                                      enumerate(zip(got, expected)) if a != b), -1)
                        log.append(f"[{op_idx}] read_verify_prbs "
                                   f"seed=0x{seed:08x} {n}B FAIL "
                                   f"mismatches={mism} first_at={first}  "
                                   f"{dt*1e3:.2f} ms")
                        # keep first 256 bytes around the first mismatch in .bin
                        start = max(0, first - 64)
                        read_bin += b"--MISMATCH--"
                        read_bin += bytes(got[start:start+256])
                elif tag == 0x09:   # xfer_prbs
                    seed, n = struct.unpack("<II", body[:8])
                    buf = prbs_xorshift32(seed, n)
                    got = bytes(dev.spiMaster_SingleReadWrite(buf, True))
                    read_bin += got
                    dt = time.time() - top
                    log.append(f"[{op_idx}] xfer_prbs seed=0x{seed:08x} {n}B  "
                               f"{dt*1e3:.2f} ms")
                elif tag == 0x0A:   # uart_tx
                    if ser is None:
                        raise RuntimeError(
                            "uart_tx opcode requires QspiHandler "
                            "with serial_port configured"
                        )
                    # Byte-at-a-time with a fixed inter-byte gap.  Bulk
                    # ser.write() through a pyserial handle shared with
                    # a background read thread on Windows drops bytes
                    # unpredictably (concurrent OVERLAPPED I/O state);
                    # serialising the writes sidesteps the problem
                    # entirely.  Cost is 50 ms per byte, i.e. ~650 ms
                    # for a 13-byte ASCII command -- trivial compared
                    # to the capture window.
                    for ch in body:
                        ser.write(bytes([ch]))
                        ser.flush()
                        time.sleep(0.05)
                    log.append(f"[{op_idx}] uart_tx {len(body)}B")
                elif tag == 0x0B:   # mark <label>
                    # Emit a checkpoint in the log with elapsed time
                    # from session start.  Useful for timing phases
                    # that span multiple ops.
                    label = bytes(body).decode(errors="replace")
                    elapsed = time.time() - t0
                    log.append(
                        f"[{op_idx}] mark '{label}'  t={elapsed*1e3:.2f} ms"
                    )
                elif tag == 0x0C:   # wait_uart(timeout_ms, sentinel...)
                    # Block until the UART reader has observed the
                    # sentinel byte sequence in its buffer, or the
                    # timeout expires.  Hitting the sentinel sets
                    # early_done so the caller can skip the trailing
                    # runtime-hold sleep.  uart_buf is the shared
                    # list the reader thread appends to.
                    tmo = struct.unpack("<I", body[:4])[0]
                    sentinel = bytes(body[4:])
                    if uart_buf is None:
                        raise RuntimeError(
                            "wait_uart opcode requires uart_buf "
                            "(JobHandler session)"
                        )
                    deadline = time.time() + (tmo / 1000.0)
                    hit = False
                    while time.time() < deadline:
                        if sentinel in b"".join(uart_buf):
                            hit = True
                            break
                        time.sleep(0.01)
                    dt = time.time() - top
                    if hit:
                        early_done = True
                        log.append(
                            f"[{op_idx}] wait_uart {sentinel!r} HIT  "
                            f"{dt*1e3:.2f} ms"
                        )
                    else:
                        log.append(
                            f"[{op_idx}] wait_uart {sentinel!r} TIMEOUT "
                            f"({tmo} ms)  {dt*1e3:.2f} ms"
                        )
                elif tag == 0x20:   # slave_write
                    # Queue bytes for FT4222 slave TX FIFO. The DSP master
                    # clocks them out of MISO at its own pace.
                    dev.spiSlave_Write(bytes(body))
                    log.append(f"[{op_idx}] slave_write {len(body)}B queued")
                elif tag == 0x21:   # slave_read(n, timeout_ms)
                    n, tmo = struct.unpack("<II", body[:8])
                    got = self._slave_drain(dev, n, tmo)
                    read_bin += got
                    dt = time.time() - top
                    log.append(
                        f"[{op_idx}] slave_read want={n} got={len(got)} "
                        f"timeout={tmo}ms  {dt*1e3:.2f} ms"
                    )
                elif tag == 0x22:   # slave_read_verify_prbs(seed,n,timeout)
                    seed, n, tmo = struct.unpack("<III", body[:12])
                    got = self._slave_drain(dev, n, tmo)
                    expected = prbs_xorshift32(seed, n)
                    dt = time.time() - top
                    if len(got) != n:
                        log.append(
                            f"[{op_idx}] slave_read_verify_prbs "
                            f"seed=0x{seed:08x} want={n} got={len(got)} "
                            f"SHORT timeout={tmo}ms  {dt*1e3:.2f} ms"
                        )
                        read_bin += b"--SHORT--" + got
                    elif got == expected:
                        log.append(
                            f"[{op_idx}] slave_read_verify_prbs "
                            f"seed=0x{seed:08x} {n}B OK  {dt*1e3:.2f} ms"
                        )
                    else:
                        mism = sum(1 for a, b in zip(got, expected) if a != b)
                        first = next(
                            (i for i, (a, b) in
                             enumerate(zip(got, expected)) if a != b), -1)
                        log.append(
                            f"[{op_idx}] slave_read_verify_prbs "
                            f"seed=0x{seed:08x} {n}B FAIL "
                            f"mismatches={mism} first_at={first}  "
                            f"{dt*1e3:.2f} ms"
                        )
                        start = max(0, first - 64)
                        read_bin += b"--MISMATCH--"
                        read_bin += bytes(got[start:start+256])
                elif tag == 0x23:   # slave_write_prbs
                    seed, n = struct.unpack("<II", body[:8])
                    buf = prbs_xorshift32(seed, n)
                    dev.spiSlave_Write(buf)
                    log.append(f"[{op_idx}] slave_write_prbs "
                               f"seed=0x{seed:08x} {n}B queued")
                else:
                    raise ValueError(f"unknown tag 0x{tag:02x} at pos {pos}")
        finally:
            dev.close()

        total = time.time() - t0
        log.append(f"done: {op_idx} ops, {total*1e3:.2f} ms total, "
                   f"{len(read_bin)} bytes read"
                   + ("  early_done" if early_done else ""))
        if read_bin:
            log.append(f"read sha256={hashlib.sha256(read_bin).hexdigest()}")
        return bytes(read_bin), "\n".join(log) + "\n", early_done


class ScopeDriver:
    def __init__(self, inst):
        self.inst = inst

    def _parse_val(self, s):
        return float(''.join(c for c in s if (c.isdigit() or c in ".-+eE")))

    def configure(self):
        self.inst.write("STOP")
        self.inst.write("WAV:FORM BYTE")
        self.inst.write("WAV:MODE RAW")
        self.inst.write("WAV:POIN MAX")

    def read_channel(self, ch):
        self.inst.write(f"WAV:SOUR {ch}")

        raw = self.inst.query_binary_values(
            "WAV:DATA?",
            datatype="B",
            container=bytes
        )

        wave = np.frombuffer(raw, dtype=np.uint8)

        vdiv = self._parse_val(self.inst.query(f"{ch}:VDIV?"))
        ofst = self._parse_val(self.inst.query(f"{ch}:OFST?"))
        sara = self._parse_val(self.inst.query("SARA?"))

        v = (wave - 128) * (vdiv / 25.0) - ofst
        t = np.arange(len(v)) / sara

        return t, v

    def get_traces(self, channels):
        self.configure()

        out = {}
        for ch in channels:
            out[ch] = self.read_channel(ch)

        self.inst.write("RUN")
        self.inst.write("TRIG:MODE AUTO")

        return out

    def traces_to_csv(self, traces, max_points=5000):
        first_ch = next(iter(traces))
        t = traces[first_ch][0]

        channels = list(traces.keys())
        vs = [traces[ch][1] for ch in channels]

        n = min([len(t)] + [len(v) for v in vs])
        step = max(1, n // max_points)

        t = t[::step]
        vs = [v[::step] for v in vs]

        buf = io.StringIO()
        w = csv.writer(buf)

        w.writerow(["t"] + channels)

        for i in range(len(t)):
            w.writerow([t[i]] + [vs[j][i] for j in range(len(channels))])

        return buf.getvalue()


class JobHandler:
    """Process a single job payload and return its artefacts.

    Subclasses implement run(payload, headers) and return a dict
    {suffix: bytes|str}, where each key is a file extension including
    the leading dot (e.g. ".csv", ".txt"). The Poller POSTs ".txt"
    last, so handlers don't need to think about artefact ordering.
    """
    def run(self, payload, headers):
        raise NotImplementedError


class Icestick:
    """Program iCEstick SPI flash (Micron N25Q032) via FT2232H channel A,
    then release CRESET so the FPGA boots from flash.

    Drives the FT2232H in MPSSE mode through FTDI's D2XX driver (via
    ftd2xx), so this coexists with the VCP driver on channel B and
    needs no Zadig/WinUSB binding.

    FT2232H ADBUS (channel A) pinout on the iCEstick
    (per the iceprog reference in Project IceStorm):
        AD0 = SCK       (out)
        AD1 = MOSI      (out, to flash SI)
        AD2 = MISO      (in,  from flash SO)
        AD4 = CS*       (out, to flash CS*)
        AD6 = CDONE     (in,  FPGA config done)
        AD7 = CRESET*   (out, active low FPGA reset)
    """
    DESC = b"Dual RS232-HS A"   # FT2232H channel A, under FTDI driver
    CLOCK_DIVISOR = 29          # 60 MHz / ((1+29)*2) = 1 MHz SPI
                                # (slow while bringing up; bump later)

    # Direction byte for ADBUS: AD0,1,4,7 outputs; AD2,3,5,6 inputs.
    DIR_LOW = 0x93
    # Output values (applied via MPSSE cmd 0x80).
    #   bit 4 = CS*    (1 = de-asserted)
    #   bit 7 = CRESET*(1 = FPGA running, 0 = FPGA held in reset)
    IDLE_RESET    = 0x10   # CS=1, CRESET=0
    IDLE_RUN      = 0x90   # CS=1, CRESET=1
    ACTIVE_RESET  = 0x00   # CS=0, CRESET=0

    PAGE = 256
    SECTOR = 64 * 1024

    def program(self, bitstream):
        dev = ftd2xx.openEx(self.DESC, 2)   # 2 = OPEN_BY_DESCRIPTION
        try:
            self._mpsse_init(dev)
            self._set_low(dev, self.IDLE_RESET)  # hold FPGA in reset
            time.sleep(0.01)
            self._cmd(dev, [0xAB])               # wake flash
            time.sleep(0.001)                    # tRES1 (~30 us) slack

            ident = self._xfer(dev, [0x9F], 3)
            if ident[0] != 0x20:   # Micron JEDEC manufacturer code
                raise RuntimeError(f"bad flash ID: {ident.hex()}")

            self._erase(dev, len(bitstream))
            self._write(dev, bitstream)
            self._verify(dev, bitstream)

            self._set_low(dev, self.IDLE_RUN)    # release FPGA
        finally:
            dev.setBitMode(0, 0)   # back to reset mode; pins tri-state
            dev.close()

    def _mpsse_init(self, dev):
        dev.setTimeouts(3000, 3000)
        dev.setLatencyTimer(1)
        dev.resetDevice()
        dev.purge(3)               # flush RX+TX
        dev.setBitMode(0, 0x00)    # reset bit mode
        time.sleep(0.02)
        dev.setBitMode(0, 0x02)    # enter MPSSE
        time.sleep(0.02)
        dev.purge(3)

        # Sync check: bogus opcode 0xAA should elicit "0xFA 0xAA".
        dev.write(bytes([0xAA]))
        time.sleep(0.05)
        resp = dev.read(2)
        if resp != b"\xFA\xAA":
            raise RuntimeError(
                f"MPSSE sync failed: expected 0xFA 0xAA, got {resp.hex()}"
            )

        dev.write(bytes([
            0x85,                  # disable loopback
            0x8A,                  # disable clock div-by-5 (60 MHz base)
            0x97,                  # disable adaptive clocking
            0x8D,                  # disable 3-phase clocking
            0x86,
            self.CLOCK_DIVISOR        & 0xff,
            (self.CLOCK_DIVISOR >> 8) & 0xff,
        ]))
        time.sleep(0.01)

    def _set_low(self, dev, value):
        dev.write(bytes([0x80, value, self.DIR_LOW]))

    def _cmd(self, dev, out):
        """Write `out` as one CS-asserted SPI transaction (no read)."""
        n = len(out) - 1
        dev.write(
            bytes([0x80, self.ACTIVE_RESET, self.DIR_LOW,
                   0x11, n & 0xff, (n >> 8) & 0xff])
            + bytes(out)
            + bytes([0x80, self.IDLE_RESET, self.DIR_LOW])
        )

    def _xfer(self, dev, out, rdlen):
        """Write `out`, then clock in `rdlen` bytes, under one CS."""
        nw = len(out) - 1
        nr = rdlen - 1
        dev.write(
            bytes([0x80, self.ACTIVE_RESET, self.DIR_LOW,
                   0x11, nw & 0xff, (nw >> 8) & 0xff])
            + bytes(out)
            + bytes([0x20, nr & 0xff, (nr >> 8) & 0xff,
                     0x80, self.IDLE_RESET, self.DIR_LOW,
                     0x87])
        )
        got = dev.read(rdlen)
        if len(got) != rdlen:
            raise RuntimeError(
                f"short read: got {len(got)} of {rdlen} bytes"
            )
        return got

    def _wait_wip(self, dev):
        while self._xfer(dev, [0x05], 1)[0] & 0x01:
            time.sleep(0.001)

    def _erase(self, dev, nbytes):
        for addr in range(0, nbytes, self.SECTOR):
            self._cmd(dev, [0x06])   # WREN
            self._cmd(dev, [0xD8,
                            (addr >> 16) & 0xff,
                            (addr >>  8) & 0xff,
                             addr        & 0xff])
            self._wait_wip(dev)

    def _write(self, dev, buf):
        for off in range(0, len(buf), self.PAGE):
            chunk = bytes(buf[off:off + self.PAGE])
            self._cmd(dev, [0x06])   # WREN
            self._cmd(dev, [0x02,
                            (off >> 16) & 0xff,
                            (off >>  8) & 0xff,
                             off        & 0xff] + list(chunk))
            self._wait_wip(dev)

    def _verify(self, dev, buf):
        got = self._xfer(dev, [0x03, 0, 0, 0], len(buf))
        if got == buf:
            return
        for i, (a, b) in enumerate(zip(buf, got)):
            if a != b:
                raise RuntimeError(
                    f"verify mismatch at 0x{i:06x}: "
                    f"wrote 0x{a:02x}, got 0x{b:02x}"
                )


class IcestickHandler(JobHandler):
    def __init__(self, serial_port=None, baudrate=115200, rx_duration_s=5):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.rx_duration_s = rx_duration_s
        self.icestick = Icestick()

    def _read_serial_for(self, ser, duration_s):
        start = time.time()
        buf = []
        while time.time() - start < duration_s:
            data = ser.read(1024)
            if data:
                buf.append(data)
        return b"".join(buf).decode(errors="replace")

    def run(self, payload, headers):
        ser = None
        try:
            if self.serial_port is not None:
                # DTR/RTS asserted on open send a glitch to the FPGA
                # side that corrupts the first received bytes, so clear
                # them explicitly.
                ser = serial.Serial(
                    self.serial_port,
                    baudrate=self.baudrate,
                    timeout=0.1,
                    dsrdtr=False, rtscts=False, xonxoff=False,
                )
                ser.setDTR(False)
                ser.setRTS(False)
                ser.reset_input_buffer()

            self.icestick.program(payload)

            duration = float(headers.get("X-Test-Runtime", self.rx_duration_s))
            if ser is not None:
                uart_msg = self._read_serial_for(ser, duration)
            else:
                uart_msg = f"programmed {len(payload)} bytes OK\n"
        finally:
            if ser is not None:
                ser.close()
        return {".txt": uart_msg}


class DspHandler(JobHandler):
    def __init__(self,
                 serial_port="COM31",
                 baudrate=115200,
                 rx_duration_s=5,
                 scope_resource=None):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.rx_duration_s = rx_duration_s

        self.expander = Expander()
        self.qspi = QSPI()

        if scope_resource is not None:
            rm = pyvisa.ResourceManager()
            inst = rm.open_resource(scope_resource)
            self.scope = ScopeDriver(inst)
        else:
            self.scope = None

    def _read_serial_for(self, ser, duration_s):
        start = time.time()
        buf = []

        while time.time() - start < duration_s:
            data = ser.read(1024)
            if data:
                buf.append(data)

        return b"".join(buf).decode(errors="replace")

    # Sentinel used by submit.py to staple a qspi TLV stream onto the
    # back of an LDR payload so both travel as a single job.  DspHandler
    # splits on this and runs the qspi opcodes as a post-boot phase of
    # the same session, so UART + scope capture spans reset -> boot ->
    # qspi ops -> trailing prints without interruption.
    QSPI_ATTACH_SEP = b"\n!QSPI-ATTACH!\n"

    def _split_payload(self, payload):
        i = payload.find(self.QSPI_ATTACH_SEP)
        if i < 0:
            return payload, None
        return payload[:i], payload[i + len(self.QSPI_ATTACH_SEP):]

    def run(self, payload, headers):
        ldr_payload, qspi_payload = self._split_payload(payload)

        # Open the UART before loading so the first byte of the new
        # firmware's output is captured. Reset halts the old firmware;
        # drain clears any bytes it emitted before reset took effect.
        ser = serial.Serial(
            self.serial_port,
            baudrate=self.baudrate,
            timeout=0.1
        )
        uart_buf = []
        uart_err = []
        stop_evt = threading.Event()
        reader = None

        qspi_log = ""
        qspi_bin = b""
        qspi_err = None

        try:
            self.expander.reset()
            self.expander.exp_init()
            ser.reset_input_buffer()

            # Start the UART reader *before* the boot loader runs so the
            # first byte emitted by the new firmware is captured.  The
            # reader runs in the background for the whole session and is
            # only stopped after the post-op trailing window below.
            reader = threading.Thread(
                target=QspiHandler._uart_reader_fn,
                args=(ser, stop_evt, uart_buf, uart_err),
                daemon=True,
            )
            reader.start()

            self.qspi.accept_ldr(ldr_payload)

            qspi_early_done = False
            if qspi_payload is not None:
                try:
                    qspi_bin, qspi_log, qspi_early_done = QspiTest().run(
                        qspi_payload, ser=ser, uart_buf=uart_buf)
                except Exception:
                    qspi_err = ("qspi executor raised:\n"
                                + traceback.format_exc())

            duration = float(headers.get("X-Test-Runtime", self.rx_duration_s))
            # Hold the session open for the full runtime so the reader
            # keeps draining any trailing prints the firmware emits
            # after the qspi ops finish.  A wait_uart TLV that matched
            # its sentinel (qspi_early_done) short-circuits the hold
            # -- the firmware has already signalled it is done and
            # further sleep is pure wall-clock waste.
            if not qspi_early_done:
                time.sleep(duration)
        finally:
            if reader is not None:
                stop_evt.set()
                reader.join(timeout=2.0)
            try:
                ser.close()
            except Exception:
                pass

        uart_msg = b"".join(uart_buf).decode(errors="replace")
        uart_bytes = len(uart_msg.encode(errors="replace"))
        reader_state = ("not started" if reader is None
                        else "alive" if reader.is_alive()
                        else "exited")

        log = uart_msg
        if qspi_payload is not None:
            log += ("\n--- QSPI SESSION ---\n" + (qspi_log or "")
                    + f"\nqspi_bin={len(qspi_bin)} bytes\n")
        log += (f"\nuart reader: {reader_state}, drained {uart_bytes} bytes"
                f" over {duration:.3f}s\n")
        for e in uart_err:
            log += "\n--- UART READER EXCEPTION ---\n" + e
        if qspi_err is not None:
            log += "\n--- QSPI EXCEPTION ---\n" + qspi_err

        artefacts = {}
        if self.scope is not None:
            traces = self.scope.get_traces(("C1", "C2", "C3", "C4"))
            artefacts[".csv"] = self.scope.traces_to_csv(traces)
        artefacts[".txt"] = log
        if qspi_bin:
            artefacts[".bin"] = qspi_bin
        return artefacts


class QspiHandler(JobHandler):
    """Run a server-defined QSPI opcode script against an already-booted DSP.

    Job file grammar (little-endian throughout). Two header versions are
    supported; v1 is master-only, v2 adds a role byte + system clock.

        v1 header (8 bytes):
            magic       = b"QSPI"
            version u8  = 1
            clk_div u8  (FT4222 Clock enum: 0=DIV_2, 1=DIV_4, ... 8=DIV_512)
            mode    u8  (1=SINGLE, 2=DUAL, 4=QUAD)
            flags   u8  (bit0 = CPOL idle-high, bit1 = CPHA clock-leading)

        v2 header (10 bytes):
            magic       = b"QSPI"
            version u8  = 2
            role    u8  (0 = FT4222 master / DSP slave,
                         1 = FT4222 slave  / DSP master)
            sys_clk u8  (0=24 MHz, 1=48 MHz, 2=60 MHz, 3=80 MHz;
                         caps max slave SCK per datasheet Table 4.2:
                         80->20, 60->15, 48->12, 24->6 MHz)
            clk_div u8  (FT4222 Clock enum; master only, ignored in slave)
            mode    u8  (master: 1/2/4 lanes; slave: must be 1)
            flags   u8  (CPOL/CPHA as in v1)

        then repeated ops:
            tag u8, len u32, payload[len]

        master opcodes (require role=0):
            0x01 write           payload = bytes out
            0x02 read            payload = u32 nbytes
            0x03 xfer            payload = bytes out (single-lane full-duplex)
            0x06 mixed_xfer      payload = u16 ns, u16 nw, u32 nr,
                                           then ns + nw bytes
                                  (single-lane nsB write, multi-lane nwB write,
                                   multi-lane nrB read; one CS assertion)
            0x07 write_prbs      payload = u32 seed, u32 nbytes
            0x08 read_verify_prbs payload = u32 seed, u32 nbytes
            0x09 xfer_prbs       payload = u32 seed, u32 nbytes

        slave opcodes (require role=1):
            0x20 slave_write     payload = bytes (queued; DSP clocks them out)
            0x21 slave_read      payload = u32 nbytes, u32 timeout_ms
            0x22 slave_read_verify_prbs
                                 payload = u32 seed, u32 nbytes, u32 timeout_ms
            0x23 slave_write_prbs payload = u32 seed, u32 nbytes

        role-neutral opcodes:
            0x04 delay_us        payload = u32 microseconds
            0x05 reinit          payload = clk_div u8, mode u8, flags u8, _rsv u8
                                  (slave role: clk_div ignored, mode must be 1)
            0x0A uart_tx         payload = bytes to send on the DSP UART

    Artefacts:
        .bin -- concatenation of bytes captured by read / xfer / xfer_prbs ops
                and mismatch excerpts from read_verify_prbs
        .txt -- per-op log with timing and throughput; pass/fail for PRBS verify
    """

    def __init__(self, serial_port=None, baudrate=115200):
        self.qspi_test = QspiTest()
        self.serial_port = serial_port
        self.baudrate = baudrate

    # Kept as a thin wrapper around the shared helper so existing
    # QspiHandler.run() call sites don't have to change.
    def _uart_reader(self, ser, stop_evt, out_buf, err_sink):
        QspiHandler._uart_reader_fn(ser, stop_evt, out_buf, err_sink)

    @staticmethod
    def _uart_reader_fn(ser, stop_evt, out_buf, err_sink):
        """Drain ``ser`` into ``out_buf`` until ``stop_evt`` is set.

        Runs in a daemon thread so that UART bytes emitted by the DSP
        while the SPI opcode script executes are captured alongside
        the SPI traffic, not only after it finishes.

        Any exception is captured into ``err_sink`` instead of killing
        the thread silently -- without this, a raise inside ``ser.read``
        (port contention, USB hot-unplug, ...) would leave ``out_buf``
        mysteriously empty and we'd have no way to tell that the
        reader died mid-job.
        """
        try:
            while not stop_evt.is_set():
                data = ser.read(1024)
                if data:
                    out_buf.append(data)
            # Final drain after the stop event fires, in case the DSP
            # emits a trailing summary line.
            data = ser.read(4096)
            if data:
                out_buf.append(data)
        except Exception:
            err_sink.append("uart reader raised:\n"
                            + traceback.format_exc())

    def run(self, payload, headers):
        ser = None
        uart_buf = []
        uart_err = []
        stop_evt = threading.Event()
        reader = None
        read_bin = b""
        log = ""
        err_tb = None

        try:
            if self.serial_port is not None:
                try:
                    ser = serial.Serial(
                        self.serial_port,
                        baudrate=self.baudrate,
                        timeout=0.1,
                    )
                    ser.reset_input_buffer()
                    reader = threading.Thread(
                        target=self._uart_reader,
                        args=(ser, stop_evt, uart_buf, uart_err),
                        daemon=True,
                    )
                    reader.start()
                except Exception:
                    # UART unavailable should not prevent the SPI part
                    # from running or from reporting its own failure.
                    err_tb = ("UART open failed:\n" + traceback.format_exc())
                    ser = None
                    reader = None

            try:
                read_bin, log, _early = self.qspi_test.run(
                    payload, ser=ser, uart_buf=uart_buf)
            except Exception:
                # Capture whatever log the executor had built up before
                # blowing up, so the user can see where it got to.
                err_tb = ((err_tb + "\n") if err_tb else "") \
                    + "qspi executor raised:\n" + traceback.format_exc()
        finally:
            if reader is not None:
                stop_evt.set()
                reader.join(timeout=2.0)
            if ser is not None:
                try:
                    ser.close()
                except Exception:
                    pass

        # Surface reader-thread lifecycle in the log so an empty .uart
        # is distinguishable from a crashed reader.
        uart_bytes = sum(len(b) for b in uart_buf)
        reader_state = ("not started" if reader is None
                        else "alive" if reader.is_alive()
                        else "exited")
        log = (log or "") + (
            f"\nuart reader: {reader_state}, drained {uart_bytes} bytes\n"
        )
        for e in uart_err:
            log += "\n--- UART READER EXCEPTION ---\n" + e

        if err_tb is not None:
            log += "\n--- EXCEPTION ---\n" + err_tb

        out = {".txt": log or "(empty log)\n"}
        if read_bin:
            out[".bin"] = read_bin
        if uart_buf:
            out[".uart"] = b"".join(uart_buf)
        return out


class Poller:
    def __init__(self, handlers, http_port=8080, poll_interval_s=1.0):
        self.handlers = handlers
        self.poll_interval_s = poll_interval_s
        self.remote = Remote(http_port)
        self._stop = False

    def stop(self):
        self._stop = True

    def _dispatch(self, kind, payload, headers):
        job_id = hashlib.sha256(payload).hexdigest()
        handler = self.handlers[kind]
        print(datetime.now(), "pickup", kind,
              type(handler).__name__, job_id)

        try:
            artefacts = handler.run(payload, headers)
        except Exception:
            tb = traceback.format_exc()
            print(datetime.now(), "handler", type(handler).__name__,
                  "raised:\n" + tb)
            artefacts = {
                ".txt": (f"handler {type(handler).__name__} raised "
                         f"before returning any artefacts:\n{tb}")
            }

        # .txt is the completion sentinel -- every other artefact must be
        # on disk by the time submit.py sees it, so POST it last. Never
        # let a POST failure on a sibling artefact prevent the sentinel
        # from going out, or submit.py --wait hangs until timeout.
        txt = artefacts.pop(".txt", b"(no .txt produced)\n")
        for ext, data in artefacts.items():
            try:
                self.remote.post_resp(job_id, data, suffix=ext)
            except Exception:
                print(datetime.now(), f"post {ext} failed:\n"
                      + traceback.format_exc())
        try:
            self.remote.post_resp(job_id, txt)
        except Exception:
            print(datetime.now(), "post .txt failed:\n"
                  + traceback.format_exc())

    def run(self):
        try:
            while not self._stop:
                dispatched = False
                for kind in self.handlers:
                    payload, headers = self.remote.get_job(kind)
                    if payload:
                        self._dispatch(kind, payload, headers)
                        dispatched = True

                if not dispatched:
                    time.sleep(self.poll_interval_s)

        except KeyboardInterrupt:
            self._stop = True


def find_icestick_uart():
    """UART device of iCEstick FT2232H channel B.

    Windows: pyserial's COM-device hwid strings don't carry the MI_XX
    interface suffix, so we can't tell A from B from there. Ask D2XX
    for the COM number directly (FT_GetComPortNumber) -- it sees each
    interface as its own device.

    Linux: pyserial exposes the USB interface number in the
    location/hwid string as ``:1.N``. Channel B = interface 1, so
    match VID:PID 0403:6010 and the ``:1.1`` suffix.
    """
    if sys.platform == "win32":
        try:
            dev = ftd2xx.openEx(b"Dual RS232-HS B", 2)
        except ftd2xx.DeviceError:
            return None
        try:
            num = dev.getComPortNumber()
        finally:
            dev.close()
        return f"COM{num}" if num > 0 else None

    for p in serial.tools.list_ports.comports():
        if p.vid == 0x0403 and p.pid == 0x6010:
            if ":1.1" in (p.location or "") or ":1.1" in (p.hwid or ""):
                return p.device
    return None


def check_devices(ftdi_specs, com_specs, visa_specs):
    print(datetime.now(), "devices in use:")

    present = {d.decode(errors="replace")
               for d in (ftd2xx.listDevices(2) or [])}
    for desc, purpose in ftdi_specs:
        st = "ok" if desc in present else "MISSING"
        print(f"  [{st:>7}] FTDI {desc} -- {purpose}")

    present = {p.device for p in serial.tools.list_ports.comports()}
    for port, purpose in com_specs:
        if port is None:
            print(f"  [MISSING] COM  (not found) -- {purpose}")
        else:
            st = "ok" if port in present else "MISSING"
            print(f"  [{st:>7}] COM  {port} -- {purpose}")

    try:
        present = set(pyvisa.ResourceManager().list_resources())
    except Exception as e:
        print(f"  [MISSING] VISA -- backend unavailable: "
              f"{type(e).__name__}: {e}")
        present = set()
    for res, purpose in visa_specs:
        st = "ok" if res in present else "MISSING"
        print(f"  [{st:>7}] VISA {res} -- {purpose}")


def main():
    scope = "USB0::0xF4EC::0x1011::SDS2PDDX6R1848::INSTR"
    dsp_port = "COM31"
    ice_port = find_icestick_uart()
    check_devices(
        ftdi_specs=(
            ("FT4222 A", "DSP I/O expander + QSPI boot loader"),
            ("Dual RS232-HS A", "iCEstick SPI flash programmer"),
        ),
        com_specs=(
            (dsp_port, "DSP firmware UART output"),
            (ice_port, "iCEstick FPGA UART output"),
        ),
        visa_specs=(
            (scope, "oscilloscope trace capture"),
        ),
    )
    while True:
        try:
            handlers = {
                "ldr": DspHandler(
                    serial_port=dsp_port,
                    baudrate=115200,
                    rx_duration_s=5,
                    scope_resource=scope,
                ),
                "bin": IcestickHandler(serial_port=ice_port),
                "qspi": QspiHandler(serial_port=dsp_port, baudrate=115200),
            }
            Poller(
                handlers=handlers,
                http_port=8080,
                poll_interval_s=2.5,
            ).run()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print(datetime.now(), f"{type(e).__name__}: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()

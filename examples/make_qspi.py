# SPDX-License-Identifier: MIT
# make_qspi.py --- Build .qspi job files for the QspiHandler in poller.py
# Copyright (c) 2026 Jakob Kastelic

"""Helpers to assemble a .qspi job file. The on-disk format is the TLV
stream documented in poller.py :: QspiHandler.

Usage as a script builds a small demo job that:
  1. writes 1 kB of PRBS (seed 0xC0FFEE),
  2. reads back 1 kB and verifies it against PRBS seed 0xDECAF,
  3. does a 4 kB full-duplex PRBS xfer.
"""

import struct


# FT4222 clock divisor enum (matches QspiTest.CLK_MAP).
CLK_DIV_2, CLK_DIV_4, CLK_DIV_8, CLK_DIV_16 = 0, 1, 2, 3
CLK_DIV_32, CLK_DIV_64, CLK_DIV_128, CLK_DIV_256, CLK_DIV_512 = 4, 5, 6, 7, 8

MODE_SINGLE, MODE_DUAL, MODE_QUAD = 1, 2, 4

ROLE_MASTER, ROLE_SLAVE = 0, 1

SYS_CLK_24, SYS_CLK_48, SYS_CLK_60, SYS_CLK_80 = 0, 1, 2, 3

# flags bits: bit0 CPOL idle-high, bit1 CPHA clock-leading.
# Boot-path defaults = CPOL idle-low, CPHA clock-trailing -> flags = 0.


def header(clk_div=CLK_DIV_8, mode=MODE_SINGLE, flags=0):
    """v1 header: master-only, no sys_clk control."""
    return b"QSPI" + struct.pack("<BBBB", 1, clk_div, mode, flags)


def header_v2(role=ROLE_MASTER, sys_clk=SYS_CLK_80,
              clk_div=CLK_DIV_8, mode=MODE_SINGLE, flags=0):
    """v2 header: carries role + FT4222 system clock selector.

    Default sys_clk=SYS_CLK_80 is required to hit the 20 MHz slave SCK
    ceiling (datasheet Table 4.2).
    """
    return b"QSPI" + struct.pack("<BBBBBB", 2, role, sys_clk,
                                 clk_div, mode, flags)


def _tlv(tag, payload):
    return struct.pack("<BI", tag, len(payload)) + payload


def write(data):              return _tlv(0x01, bytes(data))
def read(nbytes):             return _tlv(0x02, struct.pack("<I", nbytes))
def xfer(data):               return _tlv(0x03, bytes(data))
def delay_us(us):             return _tlv(0x04, struct.pack("<I", us))
def reinit(clk, mode, flags): return _tlv(0x05,
                                          struct.pack("<BBBB", clk, mode,
                                                      flags, 0))
def mixed_xfer(single_buf, multi_w_buf, nread):
    hdr = struct.pack("<HHI", len(single_buf), len(multi_w_buf), nread)
    return _tlv(0x06, hdr + bytes(single_buf) + bytes(multi_w_buf))
def write_prbs(seed, n):      return _tlv(0x07, struct.pack("<II", seed, n))
def read_verify_prbs(seed, n):return _tlv(0x08, struct.pack("<II", seed, n))
def xfer_prbs(seed, n):       return _tlv(0x09, struct.pack("<II", seed, n))
def uart_tx(data):            return _tlv(0x0A, bytes(data))

# Timing checkpoint.  Emits "[op] mark '<label>'  t=XX ms" with
# elapsed time from session start; no wire effect.  Useful when
# you want to time a phase whose individual op logs don't line up.
def mark(label):
    return _tlv(0x0B,
                label.encode() if isinstance(label, str) else bytes(label))

# Early-exit.  Poller blocks until ``sentinel`` appears in the UART
# reader buffer or ``timeout_ms`` elapses.  On hit, sets the
# session's ``early_done`` flag so JobHandler skips the trailing
# X-Test-Runtime sleep hold.  Typical use: DSP firmware prints
# "TEST_SUCCESS\r\n" once its test loop passes; put
# ``wait_uart(b"TEST_SUCCESS\r\n", 10000)`` at the end of the
# script and the job returns as soon as the DSP says so.
def wait_uart(sentinel, timeout_ms):
    return _tlv(0x0C, struct.pack("<I", timeout_ms) + bytes(sentinel))


def write_prbs_multi(seed, n):
    """PRBS write that goes over the multi-IO lanes.

    Wraps ``mixed_xfer`` with an empty single-IO phase and no read,
    so the whole PRBS stream lands in the FT4222's multi-IO write
    phase -- which, when the master was initialized with
    ``MODE_DUAL`` or ``MODE_QUAD``, actually spreads the bits across
    D0/D1 (dual) or D0..D3 (quad). The tag-0x07 ``write_prbs`` path
    always uses ``spiMaster_SingleWrite`` and therefore only drives
    MOSI (D0) regardless of the init mode, so it cannot exercise
    the DSP's dual/quad slave receive.
    """
    return mixed_xfer(b"", prbs_xorshift32(seed, n), 0)

# slave-role ops (only valid when header role=ROLE_SLAVE)
def slave_write(data):
    return _tlv(0x20, bytes(data))
def slave_read(nbytes, timeout_ms):
    return _tlv(0x21, struct.pack("<II", nbytes, timeout_ms))
def slave_read_verify_prbs(seed, nbytes, timeout_ms):
    return _tlv(0x22, struct.pack("<III", seed, nbytes, timeout_ms))
def slave_write_prbs(seed, nbytes):
    return _tlv(0x23, struct.pack("<II", seed, nbytes))


def prbs_xorshift32(seed, nbytes):
    """Reference implementation; must match poller.prbs_xorshift32 byte-for-byte
    and the DSP firmware's generator."""
    x = seed & 0xFFFFFFFF
    if x == 0:
        x = 1
    out = bytearray(nbytes)
    for i in range(nbytes):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17) & 0xFFFFFFFF
        x ^= (x << 5)  & 0xFFFFFFFF
        out[i] = x & 0xFF
    return bytes(out)


if __name__ == "__main__":
    import sys
    buf = bytearray()
    buf += header(clk_div=CLK_DIV_8, mode=MODE_SINGLE, flags=0)
    buf += write_prbs(0x00C0FFEE, 1024)
    buf += delay_us(100)
    buf += read_verify_prbs(0x000DECAF, 1024)
    buf += xfer_prbs(0xA5A5A5A5, 4096)
    out = sys.argv[1] if len(sys.argv) > 1 else "demo.qspi"
    with open(out, "wb") as f:
        f.write(buf)
    print(f"wrote {out} ({len(buf)} bytes)")

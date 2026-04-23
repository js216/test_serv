# SPDX-License-Identifier: MIT
# _text.py --- Plan-side string helpers (escape decoding, etc.)
# Copyright (c) 2026 Jakob Kastelic


def decode_escapes(s):
    r"""Interpret Python-style backslash escapes inside a plan string.

    shlex.split keeps ``\r`` / ``\n`` / ``\xNN`` as literal two-character
    sequences, so without this helper you cannot send a CR or a NUL
    byte through ``mp135:uart_write data="..."`` -- the bootloader shell
    never sees the Enter keystroke it needs.

    Handles: ``\\``, ``\0``, ``\a``, ``\b``, ``\f``, ``\n``, ``\r``,
    ``\t``, ``\v``, ``\"``, ``\'``, ``\xNN``.  Unknown escapes are left
    literal (``codecs.decode(..., "unicode_escape")`` would raise on
    those).

    Returns bytes.
    """
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c != "\\" or i + 1 >= n:
            out.extend(c.encode("utf-8"))
            i += 1
            continue
        nxt = s[i + 1]
        simple = {"\\": 0x5C, "'": 0x27, '"': 0x22, "0": 0x00,
                  "a": 0x07, "b": 0x08, "f": 0x0C, "n": 0x0A,
                  "r": 0x0D, "t": 0x09, "v": 0x0B}
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
        elif nxt == "x" and i + 3 < n:
            hh = s[i + 2:i + 4]
            try:
                out.append(int(hh, 16))
            except ValueError:
                out.extend(s[i:i+4].encode("utf-8"))
            i += 4
        else:
            # Unknown escape -- preserve both characters literally.
            out.extend(s[i:i+2].encode("utf-8"))
            i += 2
    return bytes(out)

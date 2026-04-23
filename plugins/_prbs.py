# SPDX-License-Identifier: MIT
# _prbs.py --- Shared PRBS generator (32-bit xorshift)
# Copyright (c) 2026 Jakob Kastelic

# Must be bit-identical to the DSP firmware's generator. Seed must be
# non-zero; zero is remapped to 1. Each step does
#     x ^= x << 13; x ^= x >> 17; x ^= x << 5
# and emits the low 8 bits of the new state.

def prbs_xorshift32(seed, nbytes):
    x = seed & 0xFFFFFFFF
    if x == 0:
        x = 1
    out = bytearray(nbytes)
    for i in range(nbytes):
        x ^= (x << 13) & 0xFFFFFFFF
        x ^= (x >> 17) & 0xFFFFFFFF
        x ^= (x << 5) & 0xFFFFFFFF
        out[i] = x & 0xFF
    return bytes(out)

# SPDX-License-Identifier: MIT
# _prbs.py --- Shared PRBS generator (32-bit xorshift)
# Copyright (c) 2026 Jakob Kastelic

# Must be bit-identical to the DSP firmware's generator. Seed must be
# non-zero; zero is remapped to 1. Each step does
#     x ^= x << 13; x ^= x >> 17; x ^= x << 5
# and emits the low 8 bits of the new state.

class XorShift32Bytes:
    def __init__(self, seed):
        self.x = seed & 0xFFFFFFFF
        if self.x == 0:
            self.x = 1

    def read(self, nbytes):
        out = bytearray(nbytes)
        x = self.x
        for i in range(nbytes):
            x ^= (x << 13) & 0xFFFFFFFF
            x ^= (x >> 17) & 0xFFFFFFFF
            x ^= (x << 5) & 0xFFFFFFFF
            out[i] = x & 0xFF
        self.x = x
        return bytes(out)


def prbs_xorshift32(seed, nbytes):
    return XorShift32Bytes(seed).read(nbytes)

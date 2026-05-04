# SPDX-License-Identifier: MIT
# verify.py --- Default per-bullet check used by run_md.py
# Copyright (c) 2026 Jakob Kastelic

"""Default verify.py for run_md.py.

run_md.py runs this script once per markdown bullet, with the bullet
text as argv[1] and `cwd` set to the extracted artefact directory.
The exit code drives PASS/FAIL.

This default treats the bullet as a path within the artefact and
passes if the file exists. It's deliberately tiny: a real bench
should replace it with project-specific checks (manifest.json
status==ok, scope.summary entries match expectations, etc.).

Bullet examples that work with this default:
    - `manifest.json exists in the artefact`  -> checks manifest.json
    - `bench.devices.json`                    -> checks bench.devices.json
    - `streams/scope.csv.bin`                 -> checks streams/scope.csv.bin

The first whitespace-separated token of the bullet is taken as the
relative path; everything after is treated as a human-readable note
and ignored. Print the path back on stdout so the run_md log shows
exactly what was checked.
"""

import os
import sys


def main():
    if len(sys.argv) < 2:
        print("usage: verify.py <bullet-text>", file=sys.stderr)
        return 2
    bullet = sys.argv[1].strip()
    if not bullet:
        print("empty bullet", file=sys.stderr)
        return 2
    rel = bullet.split()[0].strip("`")
    if os.path.isfile(rel):
        print(rel)
        return 0
    print(f"missing: {rel}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())

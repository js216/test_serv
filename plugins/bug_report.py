# SPDX-License-Identifier: MIT
# bug_report.py --- Agent-to-operator bug report inbox pseudo-device
# Copyright (c) 2026 Jakob Kastelic

import os
import re
from datetime import datetime

import paths
from plugin import DevicePlugin, Op


MAX_TITLE = 120
MAX_BODY = 64 * 1024
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def report_dir():
    return os.path.join(paths.state_dir(), "bug_reports")


def _op_submit(session, h, args):
    title = " ".join(args["title"].split())
    body = args["body"]
    if not title:
        raise ValueError("bug_report:submit title must be non-empty")
    if len(title) > MAX_TITLE:
        raise ValueError(f"bug_report:submit title > {MAX_TITLE} chars")
    if len(body) > MAX_BODY:
        raise ValueError(f"bug_report:submit body > {MAX_BODY} bytes")

    slug = _SLUG_RE.sub("-", title.lower()).strip("-")[:40] or "report"
    name = (f"{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            f"-{session.session_id}-{slug}.md")
    md = (f"# {title}\n\n"
          f"- date: {datetime.now().isoformat(timespec='seconds')}\n"
          f"- session: {session.session_id}\n"
          f"- plan_digest: {getattr(session, 'plan_digest', None)}\n\n"
          f"{body}\n")

    d = report_dir()
    os.makedirs(d, exist_ok=True)
    paths.write_atomic(os.path.join(d, name), md.encode())
    session.stream("bug_report.submit").append(md.encode())
    session.log_event("BUGREPORT", "bug_report:submit", f"filed {name}")


class BugReportHandle:
    pass


class BugReportPlugin(DevicePlugin):
    name = "bug_report"
    doc = (
        "Write-only message inbox to the bench operator (a human and "
        "their AI assistant). Use it to report problems with the BENCH "
        "ITSELF -- plugin bugs, wrong/missing inventory, flaky cables "
        "or boards, misleading docs -- or to leave suggestions. Do NOT "
        "use it for DUT/firmware test failures; those belong in your "
        "plan's checks and artefacts. Reports land in the operator's "
        "inbox and are read later; there is no reply channel.")

    ops = {
        "submit": Op(
            args={"title": "str", "body": "str"},
            doc=("File a report. title: one line, <= "
                 f"{MAX_TITLE} chars. body: free-form text (plain or "
                 f"markdown), <= {MAX_BODY} bytes; include what you "
                 "ran, what you expected, and what happened. A copy "
                 "lands in stream bug_report.submit so your artefact "
                 "tar records what was sent."),
            run=_op_submit),
    }

    def probe(self):
        return [{
            "id": "any",
            "description": ("pseudo-device: file a bug report / message "
                            "to the bench operator"),
        }]

    def open(self, spec):
        return BugReportHandle()

    def close(self, handle):
        pass

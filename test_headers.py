#!/usr/bin/env python3
"""
Tests for the frame-header check - the one assertion that cannot be proved against
the live pages, because today none of them send these headers (which is exactly the
state we want to keep). So it is tested against synthetic responses instead.

    python3 test_headers.py

The direction matters and is easy to get backwards. Whether page B may be framed by
page A is decided by B's headers, not A's:

  * Vercel build starts sending X-Frame-Options / CSP frame-ancestors
        -> it refuses to be embedded  -> every GSN wrapper goes blank.   ALARM
  * WordPress page starts sending CSP frame-src / default-src
        -> it refuses to LOAD its child -> same blank embed, other way.  ALARM
  * WordPress page starts sending X-Frame-Options
        -> only stops GSN being framed by others. Does not break our embeds. NOTE
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from check import frame_header_findings, csp_directive  # noqa: E402

RULES = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "targets.json")))["frame_headers"]

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"        got  {got!r}\n        want {want!r}")
        FAILS.append(name)


def vercel(headers):
    a, n, _ = frame_header_findings(headers, RULES["vercel_must_not_send"], [])
    return bool(a), bool(n)


def wordpress(headers):
    a, n, _ = frame_header_findings(headers, RULES["wordpress_must_not_send"],
                                    RULES["wordpress_note_only"])
    return bool(a), bool(n)


print("today's measured state - nothing set anywhere")
check("vercel: no headers -> no alarm", vercel({}), (False, False))
check("wordpress: no headers -> no alarm", wordpress({}), (False, False))

print("\nthe failure this monitor exists to catch")
check("vercel: X-Frame-Options SAMEORIGIN -> ALARM",
      vercel({"x-frame-options": "SAMEORIGIN"}), (True, False))
check("vercel: CSP frame-ancestors 'self' -> ALARM",
      vercel({"content-security-policy": "default-src 'self'; frame-ancestors 'self'"}), (True, False))
check("wordpress: CSP frame-src 'none' -> ALARM (blocks loading the child)",
      wordpress({"content-security-policy": "frame-src 'none'"}), (True, False))
check("wordpress: CSP default-src 'self' -> ALARM (frame-src falls back to it)",
      wordpress({"content-security-policy": "default-src 'self'"}), (True, False))

print("\nthe direction that must NOT alarm - the mistake the original brief made")
check("wordpress: X-Frame-Options DENY -> note only, no alarm",
      wordpress({"x-frame-options": "DENY"}), (False, True))
check("wordpress: CSP frame-ancestors 'none' -> note only, no alarm",
      wordpress({"content-security-policy": "frame-ancestors 'none'"}), (False, True))

print("\nCSP parsing must not match on a substring of another directive")
check("frame-ancestors is not read as frame-src",
      csp_directive({"content-security-policy": "frame-ancestors 'self'"}, "frame-src"), None)
check("a CSP with no frame directives at all is clean",
      wordpress({"content-security-policy": "upgrade-insecure-requests"}), (False, False))
check("directive at the very end, no trailing semicolon, still found",
      csp_directive({"content-security-policy": "img-src *; frame-src 'none'"}, "frame-src"),
      "frame-src 'none'")

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all header tests passed")

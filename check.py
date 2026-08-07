#!/usr/bin/env python3
"""
The HTTP + rollup half of the GSN page monitor.

Runs in three stages so the browser half (browser_scan.mjs, safari_scan.mjs) can
slot in between without either side knowing about the other:

    python3 check.py --stage http     # layer 1 + layer 2 + frame headers  -> data/_http.json
    node browser_scan.mjs             # chromium + webkit, 2 widths        -> data/_browsers.json
    python3 check.py --stage merge    # rollup, hysteresis, handoffs       -> data/latest.json + history

    python3 check.py                  # http + merge, skipping browsers (for a quick local look)
    python3 check.py --stage merge --dry-run    # print, write nothing

The output shape is fixed by portal-hub/page-monitor/CONTRACT.md. Read that before
changing a key name. schema_version stays 1: everything new here lands in the
existing `notes` array, which the dashboard already renders.

Stdlib only, on purpose - it has to run on a bare runner with no pip step.
"""

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HISTORY_SAMPLES = 168          # 7 days hourly
TIMEOUT = 20
SAFARI_MAX_AGE_HOURS = 26      # a daily pass is still "today's" up to 26h later
UA = "GSN-page-monitor/1 (+internal health check; David Rives Ministries)"

BROWSER_COMBOS = [(b, v) for b in ("chrome", "safari") for v in ("desktop", "mobile")]


def not_run_rows():
    return [{"browser": b, "viewport": v, "ok": None, "console_errors": None,
             "failed_requests": None, "notes": "not run"} for b, v in BROWSER_COMBOS]


def now_utc():
    return datetime.now(timezone.utc).replace(microsecond=0)


def stamp(dt):
    return dt.strftime("%Y%m%dT%H%M%SZ")


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------- HTTP layer

def fetch(url, method="GET"):
    """Return (status, body_text, headers_dict_lowercased, error). Never raises."""
    req = urllib.request.Request(url, headers={"User-Agent": UA}, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as r:
            body = r.read().decode("utf-8", "replace") if method == "GET" else ""
            return r.status, body, {k.lower(): v for k, v in r.headers.items()}, None
    except urllib.error.HTTPError as e:
        # A 401 is a real, expected answer for graphics-portal - not an error.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return e.code, body, {k.lower(): v for k, v in (e.headers or {}).items()}, None
    except Exception as e:
        return None, "", {}, f"{type(e).__name__}: {e}"


def find_iframe_src(body, expected_src):
    """
    Returns (found_any, first_or_matching_src, matches_expected).

    NOTE the DOTALL. These iframe tags are multiline on the real pages - a
    single-line regex finds nothing on /stations/ and reports a false break.
    """
    srcs = []
    for tag in re.findall(r"<iframe\b[^>]*>", body, re.I | re.S):
        m = re.search(r"""src\s*=\s*["']([^"']+)["']""", tag, re.I | re.S)
        if m:
            srcs.append(m.group(1).strip())
    if not srcs:
        return False, None, False
    if expected_src:
        for s in srcs:
            if s.rstrip("/") == expected_src.rstrip("/"):
                return True, s, True
        return True, srcs[0], False
    return True, srcs[0], True


def csp_directive(headers, directive):
    """Return the value of one CSP directive, or None if the header/directive is absent."""
    csp = headers.get("content-security-policy")
    if not csp:
        return None
    for part in csp.split(";"):
        part = part.strip()
        if part.lower().startswith(directive + " ") or part.lower() == directive:
            return part
    return None


def frame_header_findings(headers, rules_send, rules_note):
    """
    The header check, in the correct direction.

    Whether page B may be framed by page A is decided by B's headers. So:
      - a Vercel build that starts sending X-Frame-Options or CSP frame-ancestors
        refuses to be embedded -> every GSN wrapper goes blank.
      - a WordPress page that starts sending CSP frame-src/default-src refuses to
        LOAD its child -> same blank embed, opposite mechanism.
    Both are checked. Headers that only affect being-framed-by-others on WordPress
    are recorded but never alarm.

    Returns (alarms, notes, raw) - alarms are strings; a non-empty list is a fault.
    """
    alarms, notes = [], []
    raw = {}
    for key in ("x-frame-options", "content-security-policy"):
        if headers.get(key):
            raw[key] = headers[key][:400]

    def present(rule):
        if ":" in rule:
            hdr, directive = rule.split(":", 1)
            if hdr != "content-security-policy":
                return headers.get(hdr)
            return csp_directive(headers, directive)
        return headers.get(rule)

    for rule in rules_send:
        val = present(rule)
        if val:
            alarms.append(f"{rule} appeared: {str(val)[:200]}")
    for rule in rules_note:
        val = present(rule)
        if val:
            notes.append(f"{rule} present ({str(val)[:120]}) - does not break this embed, but it is new.")
    return alarms, notes, raw


def check_layer(spec, is_wordpress, header_rules):
    """Returns (record, ok, header_alarms, header_notes)."""
    if not spec:
        return None, True, [], []
    status, body, headers, error = fetch(spec["url"])
    byts = len(body.encode("utf-8", "replace")) if body else 0
    proof = spec.get("proof_string")
    proof_found = True if not proof else (proof in body)

    out = {
        "url": spec["url"],
        "status": status,
        "bytes": byts,
        "proof_found": proof_found,
        "error": error,
    }

    ok = (error is None
          and status == spec["expected_status"]
          and byts >= spec["min_bytes"]
          and proof_found)

    if is_wordpress:
        exp_src = spec.get("expected_iframe_src")
        found, src, matches = find_iframe_src(body, exp_src)
        out["iframe_src_found"] = found
        out["iframe_src"] = src
        out["iframe_src_expected"] = matches
        if exp_src:
            ok = ok and found and matches

    alarms, notes, raw = frame_header_findings(
        headers,
        header_rules["wordpress_must_not_send"] if is_wordpress else header_rules["vercel_must_not_send"],
        header_rules["wordpress_note_only"] if is_wordpress else [],
    )
    out["frame_headers"] = raw or None
    return out, ok, alarms, notes


def stage_http(registry):
    run_at = now_utc()
    rules = registry["frame_headers"]
    result = {"generated_at": iso(run_at), "run_id": stamp(run_at), "targets": {}}

    for spec in sorted(registry["targets"], key=lambda t: t["order"]):
        wp, wp_ok, wp_alarm, wp_note = check_layer(spec.get("wordpress"), True, rules)
        vc, v_ok, v_alarm, v_note = check_layer(spec.get("vercel"), False, rules)

        extras = []
        for extra in spec.get("extra_urls", []) or []:
            st, body, _h, err = fetch(extra["url"])
            eb = len(body.encode("utf-8", "replace")) if body else 0
            good = (err is None and st == extra["expected_status"] and eb >= extra["min_bytes"]
                    and (not extra.get("proof_string") or extra["proof_string"] in body))
            extras.append({"url": extra["url"], "status": st, "bytes": eb, "ok": good})

        result["targets"][spec["id"]] = {
            "wordpress": wp, "wordpress_ok": wp_ok,
            "vercel": vc, "vercel_ok": v_ok,
            "header_alarms": {"wordpress": wp_alarm, "vercel": v_alarm},
            "header_notes": wp_note + v_note,
            "extras": extras,
        }
    return result


# --------------------------------------------------------------- link sweep

def sweep_links(links, allowed_hosts, cap):
    """
    HEAD-check a capped, deduped set of links, but ONLY on hosts we own or embed.
    Third-party hosts routinely 403 datacentre IPs; checking them would make the
    board permanently red for a reason nobody can fix, which is how a monitor
    gets ignored.
    Returns (broken, checked_count, skipped_count).
    """
    seen, broken, checked, skipped = set(), [], 0, 0
    for href in links or []:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#", "data:")):
            continue
        try:
            host = urllib.parse.urlparse(href).hostname or ""
        except Exception:
            continue
        if host not in allowed_hosts:
            skipped += 1
            continue
        key = href.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        if checked >= cap:
            skipped += 1
            continue
        checked += 1
        status, _b, _h, err = fetch(key, method="HEAD")
        if err is not None or status is None:
            status, _b, _h, err = fetch(key)     # some hosts refuse HEAD
        if err is None and status is not None and status >= 400:
            broken.append({"url": key, "status": status})
    return broken, checked, skipped


# ------------------------------------------------------------ rollup + merge

def derive(wp_present, wp_ok, v_present, v_ok, unknown, browser_ok):
    if unknown:
        return "unknown"
    wp_bad = wp_present and not wp_ok
    v_bad = v_present and not v_ok
    if wp_bad and v_bad:
        return "both"
    if wp_bad:
        return "wordpress"
    if v_bad:
        return "vercel"
    if browser_ok is False:
        return "browser"
    return "none"


def roll(fault_layer, prev_failures):
    """Hysteresis. One bad sample is Degraded, two consecutive is Broken."""
    if fault_layer == "none":
        return "clear", 0
    fails = prev_failures + 1
    return ("broken" if fails >= 2 else "degraded"), fails


def handoff(spec, rec, header_alarms, broken_links):
    """
    The paste-ready prompt. Written for a fresh AI session on Daniel's laptop with
    zero other context - so it names the URL, the repo, the symptom and what was
    already ruled out. Never contains a credential: it only ever quotes URLs,
    status codes and byte counts, all of which are public facts about public pages.
    """
    wp, vc = rec.get("wordpress"), rec.get("vercel")
    L = []
    L.append(f"The GSN page \"{spec['label']}\" is failing its automated health check "
             f"and I need help fixing it. I am not a developer - explain in plain English.")
    L.append("")
    L.append(f"WHAT IT IS: {spec['blurb']}")
    L.append(f"CHECKED AT: {rec['checked_at']} (UTC)")
    L.append(f"WHERE THE ROBOT THINKS IT BROKE: {rec['fault_layer']}")
    L.append("")
    L.append("THE CHAIN, LAYER BY LAYER:")
    if wp:
        L.append(f"  1. WordPress page {wp['url']}")
        L.append(f"     status {wp['status']}, {wp['bytes']:,} bytes, "
                 f"proof string {'found' if wp['proof_found'] else 'MISSING'}"
                 + (f", error {wp['error']}" if wp.get("error") else ""))
        if "iframe_src_found" in wp:
            L.append(f"     embed tag {'present' if wp['iframe_src_found'] else 'MISSING'}; "
                     f"points at {wp.get('iframe_src')!r}; "
                     f"expected {spec['wordpress'].get('expected_iframe_src')!r} -> "
                     f"{'match' if wp.get('iframe_src_expected') else 'MISMATCH'}")
    else:
        L.append("  1. (no WordPress layer for this target)")
    if vc:
        L.append(f"  2. Vercel build {vc['url']}")
        L.append(f"     status {vc['status']} (expected {spec['vercel']['expected_status']}), "
                 f"{vc['bytes']:,} bytes, proof string "
                 f"{'found' if vc['proof_found'] else 'MISSING'}"
                 + (f", error {vc['error']}" if vc.get("error") else ""))
    else:
        L.append("  2. (no Vercel build behind this one)")

    browsers = [b for b in rec.get("browsers", []) if b.get("ok") is False]
    if browsers:
        L.append("")
        L.append("BROWSER FAILURES:")
        for b in browsers:
            L.append(f"  - {b['browser']} / {b['viewport']}: {b.get('notes') or 'failed'} "
                     f"({b.get('console_errors')} console errors, "
                     f"{b.get('failed_requests')} failed requests)")
    if header_alarms:
        L.append("")
        L.append("FRAME-BLOCKING HEADERS APPEARED (this is the big one - it means "
                 "something started refusing to be embedded, which blanks the page "
                 "while the Vercel URL still looks perfect on its own):")
        for a in header_alarms:
            L.append(f"  - {a}")
    if broken_links:
        L.append("")
        L.append("BROKEN LINKS ON OUR OWN HOSTS:")
        for b in broken_links[:10]:
            L.append(f"  - {b['status']} {b['url']}")

    L.append("")
    L.append("ALREADY RULED OUT BY THE ROBOT:")
    L.append("  - It is anonymous, so this is NOT the logged-in-only WordPress fatal.")
    L.append("  - Both layers were fetched separately, so you can trust the layer split above.")
    L.append("  - Third-party links were deliberately not checked; they are not the cause.")
    L.append("")
    L.append("WHERE THE SOURCE LIVES (on Daniel's Mac):")
    L.append("  - stations          -> ~/Desktop/gsn-stations-deploy  (NOT a git repo)")
    L.append("  - episode-metadata  -> ~/Documents/GitHub/production-portal/gsr-broll-sourcing/metadata-portal/vercel-deploy")
    L.append("  - internship        -> ~/Documents/GitHub/GSR-Internship-Program/landing")
    L.append("  - graphics-portal   -> ~/Documents/GitHub/production-portal/gsr-broll-sourcing/graphics-portal")
    L.append("  - gsn-home          -> WordPress on genesissciencenetwork.com, no repo")
    L.append("")
    L.append("WHAT I WANT: tell me which layer to fix and the smallest safe change that "
             "fixes it. If the fix is on WordPress or a plugin, say so plainly and stop - "
             "do not edit anything. If it is a Vercel build, show me the change and the "
             "exact deploy command, and I will run it myself.")
    return "\n".join(L)


def stage_merge(registry, dry_run=False):
    http = load(os.path.join(DATA, "_http.json"), None)
    if http is None:
        print("no data/_http.json - run --stage http first", file=sys.stderr)
        return 1
    browsers = load(os.path.join(DATA, "_browsers.json"), {})
    safari = load(os.path.join(DATA, "_safari.json"), {})
    prev = load(os.path.join(DATA, "latest.json"), {})
    prev_targets, prev_history = prev.get("targets", {}), prev.get("history", {})

    run_at = parse_iso(http["generated_at"]) or now_utc()
    allowed = set(registry["link_check"]["allowed_hosts"])
    cap = registry["link_check"]["max_links_per_target"]

    safari_at = parse_iso(safari.get("generated_at", "")) if safari else None
    safari_fresh = bool(safari_at and (run_at - safari_at) <= timedelta(hours=SAFARI_MAX_AGE_HOURS))

    out_targets, out_history = {}, {}

    for spec in sorted(registry["targets"], key=lambda t: t["order"]):
        tid = spec["id"]
        h = http["targets"][tid]
        wp, vc = h["wordpress"], h["vercel"]
        wp_ok, v_ok = h["wordpress_ok"], h["vercel_ok"]

        header_alarms = h["header_alarms"]["wordpress"] + h["header_alarms"]["vercel"]
        if h["header_alarms"]["wordpress"]:
            wp_ok = False
        if h["header_alarms"]["vercel"]:
            v_ok = False

        # --- browser rows: chromium/webkit from this run, safari from the daily pass
        rows = {(r["browser"], r["viewport"]): r for r in not_run_rows()}

        # The hourly lane fills all four rows: Chromium -> "chrome", Playwright WebKit
        # -> "safari". The daily real-Safari pass then OVERWRITES the safari rows if it
        # is recent enough, because WebKit is a stand-in and real Safari is the truth
        # (only real Safari reproduces the localStorage/cookie partitioning these
        # cross-origin embeds depend on). Every row says which engine produced it.
        harvested_links, browser_notes = [], []
        for src, apply_it in ((browsers, True), (safari, safari_fresh)):
            for r in (src.get("targets", {}).get(tid, {}).get("browsers", []) if src else []):
                key = (r.get("browser"), r.get("viewport"))
                if key not in rows or not apply_it:
                    continue
                rows[key] = {k: r.get(k) for k in
                             ("browser", "viewport", "ok", "console_errors", "failed_requests", "notes")}
                harvested_links += r.get("links", []) or []
        if safari and not safari_fresh:
            browser_notes.append(
                f"Daily real-Safari pass is stale (last {safari.get('generated_at', 'unknown')}); "
                f"the safari rows above are Playwright WebKit, not real Safari.")

        broken_links, checked_n, skipped_n = sweep_links(harvested_links, allowed, cap)

        ran = [r for r in rows.values() if r["ok"] is not None]
        browser_ok = None if not ran else all(r["ok"] for r in ran)
        if broken_links:
            browser_ok = False

        unknown = bool((wp and wp.get("error")) or (vc and vc.get("error")))
        fault = derive(spec.get("wordpress") is not None, wp_ok,
                       spec.get("vercel") is not None, v_ok,
                       unknown, browser_ok)

        state, fails = roll(fault, prev_targets.get(tid, {}).get("consecutive_failures", 0))

        # --- notes: everything schema_version 1 has no field for lands here
        notes = []
        if header_alarms:
            notes += ["FRAME HEADER ALARM: " + a for a in header_alarms]
        else:
            notes.append("Frame-blocking headers still absent on both layers (this is what keeps the embed alive).")
        notes += h.get("header_notes", [])
        if wp and wp.get("iframe_src"):
            notes.append(f"Live embed points at {wp['iframe_src']}")
        for ex in h.get("extras", []):
            notes.append(f"Also answers on {ex['url']} - "
                         + (f"working ({ex['status']}, {ex['bytes']:,} bytes)."
                            if ex["ok"] else f"NOT working ({ex['status']}, {ex['bytes']:,} bytes)."))
        if checked_n or skipped_n:
            notes.append(f"Links: {checked_n} checked on our own hosts, {len(broken_links)} broken, "
                         f"{skipped_n} skipped (third-party or over the {cap} cap).")
        for b in broken_links[:5]:
            notes.append(f"BROKEN LINK {b['status']}: {b['url']}")
        if not safari:
            notes.append("Safari rows are Playwright WebKit (a stand-in). The daily "
                         "real-Safari pass has never run - see README.")
        notes += browser_notes

        rec = {
            "schema_version": 1,
            "target_id": tid,
            "checked_at": iso(run_at),
            "run_id": http["run_id"],
            "wordpress": wp,
            "vercel": vc,
            "browsers": [rows[k] for k in BROWSER_COMBOS],
            "fault_layer": fault,
            "state": state,
            "consecutive_failures": fails,
            # Auto-fix is a documented stub. See README, "Why nothing auto-fixes yet":
            # not one of these five builds deploys from a repo this workflow can reach.
            "fix_attempt": None,
            "handoff_prompt": None,
            "screenshot_url": None,
            "notes": notes,
        }
        if state != "clear":
            rec["handoff_prompt"] = handoff(spec, rec, header_alarms, broken_links)

        out_targets[tid] = rec
        hist = list(prev_history.get(tid, []))
        hist.append({"t": iso(run_at), "state": state, "fault_layer": fault})
        out_history[tid] = hist[-HISTORY_SAMPLES:]

    payload = {"schema_version": 1, "generated_at": iso(run_at),
               "targets": out_targets, "history": out_history}

    if dry_run:
        print(json.dumps(payload, indent=2))
        return 0

    dump(os.path.join(DATA, "latest.json"), payload)
    for tid, rec in out_targets.items():
        dump(os.path.join(DATA, "history", tid, f"{http['run_id']}.json"), rec)

    for tid, rec in out_targets.items():
        ran = sum(1 for b in rec["browsers"] if b["ok"] is not None)
        print(f"{rec['state']:<9} {rec['fault_layer']:<10} {tid:<18} browsers:{ran}/4")
    return 0


def prune_history(days=7):
    """Keep the repo small: history older than `days` goes. latest.json keeps the strip."""
    cutoff = now_utc() - timedelta(days=days)
    removed = 0
    root = os.path.join(DATA, "history")
    for tid in os.listdir(root) if os.path.isdir(root) else []:
        d = os.path.join(root, tid)
        for fn in os.listdir(d):
            if not fn.endswith(".json"):
                continue
            try:
                when = datetime.strptime(fn[:-5], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if when < cutoff:
                os.remove(os.path.join(d, fn))
                removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["http", "merge", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune-days", type=int, default=7)
    args = ap.parse_args()

    registry = load(os.path.join(HERE, "targets.json"), None)
    if registry is None:
        print("targets.json missing or unparseable", file=sys.stderr)
        return 1

    if args.stage in ("http", "all"):
        res = stage_http(registry)
        if args.dry_run and args.stage == "http":
            print(json.dumps(res, indent=2))
            return 0
        dump(os.path.join(DATA, "_http.json"), res)
        print(f"http stage done: {res['run_id']}")

    if args.stage in ("merge", "all"):
        rc = stage_merge(registry, dry_run=args.dry_run)
        if rc == 0 and not args.dry_run:
            n = prune_history(args.prune_days)
            if n:
                print(f"pruned {n} history files older than {args.prune_days} days")
        return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())

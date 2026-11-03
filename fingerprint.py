#!/usr/bin/env python3
"""
Enroll and match fingerprints with a DigitalPersona U.are.U 4500 reader.

    python fingerprint.py enroll NAME [--samples 5]   save a finger
    python fingerprint.py verify NAME                 is this NAME's finger?
    python fingerprint.py identify                    whose finger is this?
    python fingerprint.py list                        show enrolled fingers
    python fingerprint.py delete NAME                 remove an enrolled finger

Enrolled fingers are stored in ./prints/NAME.json as minutiae (ridge ending
and fork positions), plus the scan images for reference.
"""

import argparse
import datetime
import json
import os
import re
import sys

import usb.core

import fpmatch
from uru4500_capture import Reader, normalize, raw_image

HERE = os.path.dirname(os.path.abspath(__file__))
PRINTS_DIR = os.path.join(HERE, "prints")

# Scans with fewer minutiae than this are too poor to enroll or match.
MIN_MINUTIAE = 15

# During enrollment, each new scan must score at least this against an
# earlier one. Looser than the match threshold since enrollment scans are
# deliberately shifted; a different finger typically scores under 15.
ENROLL_CONSISTENCY = 25


def print_path(name):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        raise SystemExit("error: names may only use letters, digits, '_', '-' and '.'")
    return os.path.join(PRINTS_DIR, name + ".json")


def load_print(name):
    path = print_path(name)
    if not os.path.exists(path):
        raise SystemExit(f"error: no enrolled finger named '{name}'")
    with open(path) as f:
        return json.load(f)


def all_prints():
    if not os.path.isdir(PRINTS_DIR):
        return []
    out = []
    for fn in sorted(os.listdir(PRINTS_DIR)):
        if fn.endswith(".json"):
            with open(os.path.join(PRINTS_DIR, fn)) as f:
                out.append(json.load(f))
    return out


def scan(reader, timeout):
    """Capture one finger and return (normalized image, minutiae)."""
    while True:
        img = normalize(raw_image(reader.capture(timeout)))
        reader.wait_finger_off()
        try:
            minutiae = fpmatch.extract(img.tobytes(), *img.size)
        except RuntimeError:
            minutiae = {"x": []}
        n = len(minutiae["x"])
        if n >= MIN_MINUTIAE:
            return img, minutiae
        print(f"Scan too unclear ({n} points found). Press a little firmer and try again.")


def best_score(probe, record):
    return max(fpmatch.score(probe, s["minutiae"]) for s in record["samples"])


def with_reader(fn):
    reader = Reader()
    reader.open()
    try:
        reader.power_up()
        return fn(reader)
    finally:
        reader.close()


# ---------------------------------------------------------------- commands

def cmd_enroll(args):
    path = print_path(args.name)
    if os.path.exists(path) and not args.force:
        raise SystemExit(f"error: '{args.name}' is already enrolled (use --force to replace it)")

    def run(reader):
        samples = []
        while len(samples) < args.samples:
            print(f"\nScan {len(samples) + 1} of {args.samples}"
                  + (" (shift your finger slightly each time)" if samples else ""))
            img, minutiae = scan(reader, args.timeout)
            if samples:
                s = max(fpmatch.score(minutiae, prev["minutiae"]) for prev in samples)
                if s < ENROLL_CONSISTENCY:
                    print(f"That didn't look like the same finger (score {s}). Let's redo it.")
                    continue
            n = len(samples) + 1
            img_name = f"{args.name}_{n}.png"
            img.save(os.path.join(PRINTS_DIR, img_name))
            samples.append({"image": img_name, "minutiae": minutiae})
            print(f"Got it ({len(minutiae['x'])} points).")
        return samples

    os.makedirs(PRINTS_DIR, exist_ok=True)
    samples = with_reader(run)
    record = {
        "name": args.name,
        "enrolled": datetime.datetime.now().isoformat(timespec="seconds"),
        "samples": samples,
    }
    with open(path, "w") as f:
        json.dump(record, f)
    print(f"\nEnrolled '{args.name}' with {len(samples)} scans.")


def cmd_verify(args):
    record = load_print(args.name)

    def run(reader):
        print(f"Verifying '{args.name}'")
        return scan(reader, args.timeout)

    _, probe = with_reader(run)
    score = best_score(probe, record)
    if score >= args.threshold:
        print(f"MATCH: this is '{args.name}' (score {score}, threshold {args.threshold})")
        return 0
    print(f"NO MATCH: not '{args.name}' (score {score}, threshold {args.threshold})")
    return 1


def cmd_identify(args):
    records = all_prints()
    if not records:
        raise SystemExit("error: nobody is enrolled yet")

    def run(reader):
        print("Identifying")
        return scan(reader, args.timeout)

    _, probe = with_reader(run)
    results = sorted(((best_score(probe, r), r["name"]) for r in records), reverse=True)
    for score, name in results:
        print(f"  {name:20s} score {score}")
    score, name = results[0]
    if score >= args.threshold:
        print(f"IDENTIFIED: '{name}'")
        return 0
    print("UNKNOWN: no enrolled finger matched")
    return 1


def cmd_list(args):
    records = all_prints()
    if not records:
        print("Nobody is enrolled yet.")
    for r in records:
        print(f"{r['name']:20s} {len(r['samples'])} scans, enrolled {r['enrolled']}")


def cmd_delete(args):
    record = load_print(args.name)
    for s in record["samples"]:
        try:
            os.remove(os.path.join(PRINTS_DIR, s["image"]))
        except FileNotFoundError:
            pass
    os.remove(print_path(args.name))
    print(f"Deleted '{args.name}'.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def scanning(p):
        p.add_argument("--timeout", type=float, default=30, help="seconds to wait for a finger")
        p.add_argument("--threshold", type=int, default=fpmatch.MATCH_THRESHOLD,
                       help=f"match score needed (default {fpmatch.MATCH_THRESHOLD})")

    p = sub.add_parser("enroll", help="save a finger")
    p.add_argument("name")
    p.add_argument("--samples", type=int, default=5, help="number of scans to take (default 5)")
    p.add_argument("--force", action="store_true", help="replace an existing enrollment")
    scanning(p)
    p.set_defaults(fn=cmd_enroll)

    p = sub.add_parser("verify", help="check a finger against one enrolled name")
    p.add_argument("name")
    scanning(p)
    p.set_defaults(fn=cmd_verify)

    p = sub.add_parser("identify", help="find which enrolled finger this is")
    scanning(p)
    p.set_defaults(fn=cmd_identify)

    sub.add_parser("list", help="show enrolled fingers").set_defaults(fn=cmd_list)

    p = sub.add_parser("delete", help="remove an enrolled finger")
    p.add_argument("name")
    p.set_defaults(fn=cmd_delete)

    args = ap.parse_args()
    return args.fn(args) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, TimeoutError, usb.core.USBError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

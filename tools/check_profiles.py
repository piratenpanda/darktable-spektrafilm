#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
#
# Copyright (C) 2026 darktable developers.
#
# Tooling only. The spektrafilm profile data this script checks is CC BY-SA 4.0
# by Andrea Volpato and is covered by the data repository's LICENSE.
"""Check that darktable reads each profile's density-curve model the way the
exporter wrote it.

A profile carries its density curves twice: sampled, in `density_curves`, and
as fitted sigmoid parameters, in `density_curves_model`. Those have to agree,
and darktable renders from the model. The trouble is the model's OUTER axis
means different things for different stocks:

    colour stock   (n_channels, n_layers)      outer axis = channel
    B&W stock      (n_dev_times, n_layers)     outer axis = development time

because a single-emulsion stock has one channel, so the axis is free for the
development-time family instead. Nothing in the file says which it is, and
spektra_sim.c currently guesses from the array shape:

    dev_major = dev_family || (bw && outer_len != 3)

That guess has been wrong before -- an earlier pack fed Double-X's model as
channel-major and rendered it from a curve off by 1.34 density -- and it is
tuned to exporter output that has already changed once since.

This script settles it by reconstruction: evaluate the model for each outer
row and see which column of `density_curves` it reproduces. An exact match
(max abs error ~0) is proof of what the axis means, independent of any
heuristic. It then reports whether the module's current rule picks that row,
and whether the simpler rule `dev_major = bw` would.

    ./check_profiles.py /path/to/pack            # or a repo, or packs/<ver>

Exit status is non-zero when any profile is misread, or read correctly only
because its rows happen to be identical -- which is correct by accident and
will stop being correct the moment the exporter changes.
"""

import argparse
import json
import math
import os
import sys

# Mirrors spektra_sim.c. SF_SEPT_K is the septic CDF's support in sigmas.
SF_SEPT_K = 5.8013
# A fitted model does not reproduce measured curves exactly -- colour stocks
# sit around 0.004 density residual. So the test is SEPARATION, not absolute
# error: the right column beats every other by a wide margin (0.004 vs 0.19+
# for Portra 400, ~48x). Anything under this ratio is not a confident reading.
SEPARATION = 5.0


def norm_cdf(z):
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def sept_cdf(z, alpha):
    """Order-7 Hermite smoothstep with a median-preserving skew warp, the
    `sept_norm_cdfs` family. The warp vanishes at u = 0.5, so the 0.5 crossing
    stays on the layer centre for every alpha."""
    u = min(max(z / SF_SEPT_K + 0.5, 0.0), 1.0)
    if alpha:
        t = 2.0 * u - 1.0
        u = min(max(u + alpha * u * (1.0 - u) * t * t, 0.0), 1.0)
    u2 = u * u
    return (u2 * u2) * (35.0 + u * (-84.0 + u * (70.0 - 20.0 * u)))


def layer_cdf(z, sept, alpha):
    return sept_cdf(z, alpha) if sept else norm_cdf(z)


def eval_row(model, row, log_exposure, positive):
    """Summed density over the exposure grid for one outer row of the model."""
    C, A, S = model["centers"][row], model["amplitudes"][row], model["sigmas"][row]
    AL = (model.get("alphas") or [[0.0] * len(C)] * (row + 1))[row]
    sept = model.get("model_type") == "sept_norm_cdfs"
    sign = -1.0 if positive else 1.0
    out = []
    for x in log_exposure:
        out.append(
            sum(
                A[l] * layer_cdf(sign * (x - C[l]) / S[l], sept, AL[l] if AL else 0.0)
                for l in range(len(C))
            )
        )
    return out


def best_column(curve, density_curves):
    """Which column this reconstruction reproduces, its error, and the runner-up's.

    The runner-up is what makes the answer trustworthy: a model row that fits
    one column ten times better than any other is identifying that column, not
    merely being closest to it."""
    errs = [
        max(abs(curve[i] - row[col]) for i, row in enumerate(density_curves))
        for col in range(len(density_curves[0]))
    ]
    order = sorted(range(len(errs)), key=lambda c: errs[c])
    best = order[0]
    second = errs[order[1]] if len(order) > 1 else float("inf")
    return best, errs[best], second


def check_profile(path):
    """Returns (status, stock, detail). status is 'ok', 'lucky', 'misread',
    'skip'."""
    d = json.load(open(path))
    if not isinstance(d, dict) or "info" not in d or "data" not in d:
        return "skip", os.path.basename(path), "not a profile"
    info, data = d["info"], d["data"]
    stock = info.get("stock", os.path.basename(path))
    model = data.get("density_curves_model")
    if not model or not model.get("centers"):
        return "skip", stock, "no density_curves_model"

    positive = info.get("type") == "positive"
    bw = info.get("channel_model") == "bw"
    le = data["log_exposure"]
    dc = data["density_curves"]
    C = model["centers"]
    n_outer, n_layers = len(C), len(C[0])
    dev_times = data.get("development_time") or []
    n_dev = len(dev_times) if isinstance(dev_times, list) else 1

    # Rows that are byte-identical carry no information about the axis: any
    # interpretation reproduces the same curve, so a heuristic that happens to
    # pick the right one is not being tested by this profile.
    identical = all(
        C[r] == C[0]
        and model["amplitudes"][r] == model["amplitudes"][0]
        and model["sigmas"][r] == model["sigmas"][0]
        for r in range(n_outer)
    )

    detail_shape = (
        f"model {n_outer}x{n_layers}, {n_dev} dev time(s), {len(dc[0])} curve column(s)"
    )

    # Identical rows carry no information: every interpretation reconstructs the
    # same curve, so nothing here can distinguish them and a heuristic that
    # lands on the right one is not being tested.
    if identical and n_outer > 1:
        return "lucky", stock, detail_shape + (
            f"; all {n_outer} model rows are identical copies, so any reading "
            f"is right by construction"
        )

    # Replicated columns carry no separation: a mono stock's density_curves is
    # one panchromatic curve widened to three identical columns, so "which
    # column" has no answer and asking for one fails a profile that is fine.
    # Match against the DISTINCT columns instead; the widening is a property of
    # the container, not of the fit being checked.
    cols = list(range(len(dc[0])))
    distinct = []
    for c in cols:
        if not any(all(dc[i][c] == dc[i][k] for i in range(len(dc))) for k in distinct):
            distinct.append(c)
    dc_d = [[row[c] for c in distinct] for row in dc]

    matches = []
    for r in range(n_outer):
        col, err, second = best_column(eval_row(model, r, le, positive), dc_d)
        matches.append((r, col, err, second))
    if len(distinct) == 1:
        # One curve, so nothing to separate from. Judge on absolute fidelity
        # instead, scaled by the curve's own excursion.
        span = max(max(r) for r in dc_d) - min(min(r) for r in dc_d)
        tol = max(0.02 * span, 1e-9)
        confident = [(r, distinct[col]) for r, col, err, _ in matches if err <= tol]
    else:
        confident = [
            (r, distinct[col])
            for r, col, err, second in matches
            if second >= SEPARATION * max(err, 1e-12)
        ]
    if not confident:
        worst = min(err for _, _, err, _ in matches)
        return "misread", stock, (
            f"no outer row identifies a density_curves column "
            f"(best max abs err {worst:.4f}, not separated from the runner-up) "
            f"-- model and curves disagree"
        )

    # What each rule selects. spektra_sim.c reads row `dev_row` when dev-major
    # (the selected family member, or the middle one), else row `channel`.
    dev_major_now = (n_dev > 1) or (bw and n_outer != 3)
    dev_major_proposed = bw

    def selected(dev_major):
        if not dev_major:
            return 0  # channel 0; a colour stock reads row c for channel c
        return min((n_dev - 1) // 2, n_outer - 1) if n_dev > 1 else (n_outer - 1) // 2

    row_now, row_new = selected(dev_major_now), selected(dev_major_proposed)
    confident_rows = {r for r, _ in confident}
    detail = detail_shape + "; rows->cols " + ", ".join(f"{r}->{c}" for r, c in confident)

    # Every row should identify its own column: row r <-> column r, whether r
    # indexes a channel or a development time. A row that does not is a broken
    # fit even when the row the module currently reads is still sound -- change
    # the selected development time, or the channel, and it renders from it.
    lost = [r for r in range(n_outer) if r not in confident_rows]
    if lost:
        return "misread", stock, detail_shape + (
            f"; row(s) {', '.join(map(str, lost))} identify no column -- "
            f"the model no longer reproduces those curves"
        )
    misplaced = [] if len(distinct) == 1 else [(r, c) for r, c in confident if r != c]
    if misplaced:
        return "misread", stock, detail + (
            "; rows do not line up with their own columns"
        )

    if row_now not in confident_rows:
        return "misread", stock, detail + f"; current rule picks row {row_now}"
    if row_now != row_new:
        return "ok", stock, detail + (
            f"; current rule picks {row_now}, `dev_major = bw` picks {row_new} "
            f"-- both exact, but the rules disagree"
        )
    return "ok", stock, detail


def find_profiles(root):
    root = os.path.abspath(os.path.expanduser(root))

    def jsons(d):
        return (
            sorted(os.path.join(d, n) for n in os.listdir(d) if n.endswith(".json"))
            if os.path.isdir(d)
            else []
        )

    # A pack directory. Checked before the root itself, so pointing at a
    # repository does not pick up manifest.json and stop there.
    got = jsons(os.path.join(root, "profiles"))
    if got:
        return got

    # A repository: every pack under packs/.
    packs = os.path.join(root, "packs")
    out = []
    for name in sorted(os.listdir(packs) if os.path.isdir(packs) else []):
        out += jsons(os.path.join(packs, name, "profiles"))
    if out:
        return out

    # Pointed straight at a profiles directory.
    got = jsons(root)
    if got:
        return got
    raise SystemExit(
        f"no profiles found under {root}\n"
        f"  expected {root}{os.sep}profiles{os.sep}*.json,\n"
        f"        or {root}{os.sep}packs{os.sep}<version>{os.sep}profiles{os.sep}*.json"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default=".", help="pack or repository root")
    ap.add_argument(
        "--strict",
        action="store_true",
        help="also fail on profiles that are read correctly only by coincidence",
    )
    args = ap.parse_args()

    counts = {"ok": 0, "lucky": 0, "misread": 0, "skip": 0}
    bad = []
    for path in find_profiles(args.path):
        status, stock, detail = check_profile(path)
        counts[status] += 1
        mark = {"ok": "  ok  ", "lucky": " LUCKY", "misread": "MISREAD", "skip": " skip "}
        if status != "ok":
            bad.append((status, stock, detail))
        print(f"{mark[status]}  {stock:<32} {detail}")

    print(
        f"\n{counts['ok']} ok, {counts['lucky']} correct-by-coincidence, "
        f"{counts['misread']} misread, {counts['skip']} skipped"
    )
    if counts["misread"]:
        print("\nMisread profiles render from the wrong curve. Fix before shipping.")
        return 1
    if counts["lucky"] and args.strict:
        print(
            "\nThe coincidental cases pass today because their model rows are "
            "identical copies.\nThat is a property of this export, not of the "
            "format -- they will stop passing\nthe moment those rows differ."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Build manifest.json for a spektrafilm data repository.

The manifest is what darktable's spektrafilm module reads to find out which
spectral data packs exist and what each one's files should hash to. It is
generated from the packs themselves rather than written by hand, because the
module refuses to install any file whose sha256 does not match -- a manifest
edited by hand drifts from the pack the moment either changes, and the failure
shows up as "a downloaded file failed its checksum" with no hint why.

Layout it expects and produces:

    <repo>/manifest.json
    <repo>/packs/<name>/pack.json
    <repo>/packs/<name>/spectra_lut.f32
    <repo>/packs/<name>/profiles/*.json

Usage:

    ./make_manifest.py /path/to/repo                 # every pack under packs/
    ./make_manifest.py /path/to/repo --default 0.3.3 # pick the default pack

The pack a fresh edit gets is the one flagged "default". Every other pack is
only ever fetched when an edit explicitly asks for its lut_hash.
"""

import argparse
import hashlib
import json
import os
import re
import struct
import sys

# Mirrors _parse_manifest() in src/common/spektra_fetch.c. Kept in sync by hand;
# if the C side gets stricter, tighten these too or the repository will publish
# manifests that darktable rejects at the last moment.
MAX_FILES = 512
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
RELPATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def valid_relpath(p):
    """The module's _valid_relpath(): every path here becomes a file darktable
    creates, so anything that could escape the destination directory is
    rejected outright rather than rewritten into something safe."""
    if not p or len(p) > 255:
        return False
    if p[0] in "./\\":
        return False
    if ".." in p or "\\" in p or ":" in p:
        return False
    if p.count("/") > 1:  # one optional subdirectory (profiles/), nothing deeper
        return False
    return bool(RELPATH_RE.match(p))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_lut_header(path):
    """Pull identity out of spectra_lut.f32.

    Header is fixed-width up to the id string: magic "SFS2", int32 header
    version, int32 dims[3], int32 dtype, uint32 lut_hash, int32 id_len, then
    id_len bytes. The hash is the pack's real identity -- the version string in
    pack.json is not, since an editable dev install reports whatever
    pyproject.toml happens to say."""
    with open(path, "rb") as f:
        magic = f.read(4)
        if magic != b"SFS2":
            raise ValueError(f"{path}: not a spektrafilm spectral LUT")
        (hdr_version,) = struct.unpack("<i", f.read(4))
        if hdr_version != 2:
            raise ValueError(f"{path}: LUT header version {hdr_version}, expected 2")
        dims = struct.unpack("<3i", f.read(12))
        (dtype,) = struct.unpack("<i", f.read(4))
        (lut_hash,) = struct.unpack("<I", f.read(4))
        (id_len,) = struct.unpack("<i", f.read(4))
        lut_id = f.read(id_len).decode("utf-8")

    # Catch a truncated LUT here rather than after it has been published: the
    # module's cheap header peek would happily accept it, so a short file only
    # surfaces as a wrong render.
    elem = 4 if dtype == 0 else 2
    expected = dims[0] * dims[1] * dims[2] * elem + 32 + id_len
    actual = os.path.getsize(path)
    if expected != actual:
        raise ValueError(
            f"{path}: expected {expected} bytes from its header, found {actual}"
        )

    return lut_hash, lut_id


def build_pack_entry(repo, packdir, make_default):
    rel_base = os.path.relpath(packdir, repo).replace(os.sep, "/")
    if not valid_relpath(rel_base):
        raise ValueError(f"{rel_base}: pack directory name the module would reject")

    meta = os.path.join(packdir, "pack.json")
    lut = os.path.join(packdir, "spectra_lut.f32")
    for required in (meta, lut):
        if not os.path.isfile(required):
            raise ValueError(f"{packdir}: missing {os.path.basename(required)}")

    lut_hash, lut_id = read_lut_header(lut)

    paths = ["pack.json", "spectra_lut.f32"]
    profdir = os.path.join(packdir, "profiles")
    if os.path.isdir(profdir):
        paths += sorted(
            "profiles/" + n for n in os.listdir(profdir) if n.endswith(".json")
        )

    files, total = [], 0
    for rel in paths:
        if not valid_relpath(rel):
            raise ValueError(f"{rel}: path the module would reject")
        full = os.path.join(packdir, rel)
        size = os.path.getsize(full)
        if size == 0 or size > MAX_FILE_BYTES:
            raise ValueError(f"{rel}: size {size} outside what the module accepts")
        total += size
        files.append({"path": rel, "size": size, "sha256": sha256(full)})

    if len(files) > MAX_FILES:
        raise ValueError(f"{rel_base}: {len(files)} files, module caps at {MAX_FILES}")
    if total > MAX_TOTAL_BYTES:
        raise ValueError(f"{rel_base}: {total} bytes, module caps at {MAX_TOTAL_BYTES}")

    meta_json = json.load(open(meta))

    # Republish pack.json's container format so darktable can tell, from the
    # manifest alone, whether it could load this pack -- without that it only
    # finds out after downloading the whole thing and failing to parse it.
    # Required, not defaulted: darktable refuses a pack that declares no
    # format, so publishing one would only move the failure to the user.
    if "pack_format" not in meta_json:
        raise ValueError(f"{meta}: no pack_format, re-export this pack")
    pack_format = int(meta_json["pack_format"])

    entry = {
        "lut_id": lut_id,
        "lut_hash": "%08x" % lut_hash,
        "pack_format": pack_format,
        "spektrafilm_version": meta_json.get("spektrafilm_version", ""),
        "base": rel_base,
        "files": files,
    }
    if make_default:
        entry["default"] = True
    return entry, total


def looks_like_packs_dir(path):
    """True when path directly contains pack directories rather than being the
    repository root. Checked by content, not by name, so a renamed or
    symlinked checkout still resolves."""
    if not os.path.isdir(path):
        return False
    for n in os.listdir(path):
        if os.path.isfile(os.path.join(path, n, "pack.json")):
            return True
    return False


def find_packs_dir(given):
    """Work out where the packs live.

    Accepts the repository root, the packs directory itself, or a single pack
    directory, because all three are things you would plausibly type and only
    one of them used to work. Returns (repo_root, packs_dir) or raises with an
    error that says which paths were actually examined -- the previous version
    reported only "<path>/packs: not a directory", which is the one piece of
    information that does not help you find the mistake.
    """
    given = os.path.abspath(os.path.expanduser(given))

    if not os.path.isdir(given):
        raise SystemExit(f"{given}: not a directory (does this path exist?)")

    # the repository root: packs/ underneath it
    candidate = os.path.join(given, "packs")
    if looks_like_packs_dir(candidate):
        return given, candidate

    # pointed straight at packs/
    if looks_like_packs_dir(given):
        return os.path.dirname(given), given

    # pointed at one pack; step up twice
    if os.path.isfile(os.path.join(given, "pack.json")):
        packs = os.path.dirname(given)
        return os.path.dirname(packs), packs

    found = sorted(os.listdir(given))[:8]
    raise SystemExit(
        f"no packs found from {given}\n"
        f"  looked for   {candidate}{os.sep}<name>{os.sep}pack.json\n"
        f"  and for      {given}{os.sep}<name>{os.sep}pack.json\n"
        f"  {given} contains: {', '.join(found) if found else '(empty)'}\n"
        f"\nPass the repository root, e.g.  {sys.argv[0]} /path/to/"
        f"darktable-spektrafilm\n"
        f"Expected layout: <repo>/packs/<version>/pack.json"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "repo",
        nargs="?",
        default=None,
        help="root of the data repository (default: the repository this script "
        "lives in, so running it from tools/ with no argument works)",
    )
    ap.add_argument(
        "--default",
        help="name of the pack directory under packs/ to flag as default "
        "(defaults to the last one in sorted order)",
    )
    ap.add_argument("-o", "--output", help="where to write (default <repo>/manifest.json)")
    args = ap.parse_args()

    # With no argument, assume this script sits at <repo>/tools/, which is where
    # the README puts it. Running it from inside tools/ was the easiest way to
    # trip the old error.
    given = args.repo or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo, packsdir = find_packs_dir(given)
    print(f"repository {repo}")

    names = sorted(
        n for n in os.listdir(packsdir) if os.path.isdir(os.path.join(packsdir, n))
    )
    if not names:
        sys.exit(f"{packsdir}: no packs found")

    default_name = args.default or names[-1]
    if default_name not in names:
        sys.exit(f"--default {default_name}: no such pack (have: {', '.join(names)})")

    packs, seen = [], {}
    for name in names:
        entry, total = build_pack_entry(
            repo, os.path.join(packsdir, name), name == default_name
        )
        # Two packs with one hash means a download for that hash is ambiguous
        # and the module would take whichever came first in the file.
        if entry["lut_hash"] in seen:
            sys.exit(
                f"{name} and {seen[entry['lut_hash']]} both carry table "
                f"{entry['lut_hash']} -- publish only one"
            )
        seen[entry["lut_hash"]] = name
        packs.append(entry)
        print(
            f"{name:<12} table {entry['lut_hash']}  format {entry['pack_format']}  "
            f"{len(entry['files']):>3} files  {total / 1048576:.1f} MB"
            f"{'  (default)' if name == default_name else ''}"
        )

    out = args.output or os.path.join(repo, "manifest.json")
    with open(out, "w") as f:
        json.dump({"format": 1, "packs": packs}, f, indent=2)
        f.write("\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()

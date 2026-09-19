# darktable-spektrafilm

Spectral data packs for the [_spektrafilm_](https://github.com/darktable-org/darktable/pull/21534)
module in [darktable](https://github.com/darktable-org/darktable).

The module simulates the physical chain a photograph goes through on film —
spectral sensitivity, development with inter-layer coupler inhibition, and
printing through a paper's own spectral response. It needs measured spectral
data to do that, and this repository is where it fetches it from.

The data is [spektrafilm](https://github.com/andreavolpato/spektrafilm) by
Andrea Volpato, redistributed unmodified under CC BY-SA 4.0. Nothing here is
original work beyond the packaging — see [licensing](#licensing) below.

## What a pack is

A pack is one directory holding everything the module needs to render:

| file | what it is |
| --- | --- |
| `pack.json` | colour matching functions, illuminant SPDs, dichroic filter transmittances, spectral locus, neutral print filter calibrations, per-film render defaults |
| `spectra_lut.f32` | the spectral upsampling table, float16, with a header carrying its identity hash |
| `spectra_lut_<identifier>.f32` | further tables, `pack_format` 3 only, named in `pack.json` |
| `profiles/*.json` | one film or paper stock each: characteristic curves, spectral sensitivities, dye densities, grain and halation parameters |

Packs are identified by the **hash of the spectral upsampling table** they
carry, not by a version string. Upstream revises that table between releases
and each revision renders differently, so every darktable edit records the hash
it was developed against. A version string cannot stand in for it: an editable
dev install reports whatever `pyproject.toml` happens to say, so two materially
different checkouts can claim the same version.

## Available packs

| pack | spektrafilm | table | profiles | size |
| --- | --- | --- | --- | --- |
| `packs/0.3.3` | 0.3.3 (dev branch) | `565f4ec4` — `irradiance_xy_tc@0.3.3` | 31 (22 filming, 9 printing) | 8.5 MB |

Older packs are kept rather than deleted, and this is load-bearing rather than
tidiness. When darktable opens an edit whose recorded table is not installed, it
offers to fetch precisely that pack from here; dropping the entry turns that
offer into "no pack with that spectral table is published".

One caveat worth stating plainly: the recorded hash pins the *data*, not the
module code. darktable's own rendering changes between releases, so fetching
the original pack reproduces the spectral table an edit was made with — not
necessarily the exact image it produced.

## Layout

```
manifest.json              index: every pack, every file, every checksum
packs/<version>/           one pack per spectral table
  pack.json
  spectra_lut.f32
  profiles/*.json
LICENSE                    CC BY-SA 4.0 + the spektrafilm preamble, verbatim
CHANGELOG.txt              what the packaging changes, as the license asks
tools/spektrafilm_export_data.py  exports a pack from an installed spektrafilm
tools/make_manifest.py     regenerates manifest.json from the packs
```

## How darktable uses this

The module looks for a pack in two places, in order:

1. `<config>/spektrafilm/` — installed by hand. Always preferred, and never
   overwritten by anything downloaded.
2. `<cache>/spektrafilm/packs/<lut_hash>/` — downloaded, one directory per
   spectral table.

If neither carries the table the current edit recorded, the module offers to
fetch the matching one. If you decline, or the download fails, it renders with
whatever pack it does have and shows a mismatch warning rather than refusing to
render.

Downloads read files straight out of this repository's tree over HTTPS. **Git
is not required** — no clone, no submodule, no libgit2. Three preferences
control it, all under `plugins/darkroom/spektrafilm/` in `darktablerc`:

| key | default | meaning |
| --- | --- | --- |
| `allow_download` | `false` | downloads are opt-in; nothing reaches the network until you say so |
| `repository` | `darktable-spektrafilm` | `owner/repo` to read from |
| `ref` | `main` | tag or branch to read at |

The default tracks `main`. The manifest and the files it lists are fetched in
one pass, so a branch moving mid-download cannot install mismatched data — the
per-file checksum catches it and the install is discarded. A tag is still worth
cutting once the pack set settles: an immutable ref is what lets an old edit
fetch the exact spectral table it was developed against, where a branch hands
over whatever is current.

### Installing by hand instead

Downloads are entirely optional. To skip them, copy a pack's contents into your
darktable config directory:

```sh
mkdir -p ~/.config/darktable/spektrafilm
cp -r packs/0.3.3/. ~/.config/darktable/spektrafilm/
```

That directory takes precedence over anything the module has downloaded, so
this is also how you pin a specific pack.

## The manifest

`manifest.json` is the index the module reads first. Each entry names a pack,
its table hash, where it lives, and a sha256 for every file in it:

```json
{
  "format": 1,
  "packs": [
    {
      "lut_id": "irradiance_xy_tc@0.3.3",
      "lut_hash": "565f4ec4",
      "pack_format": 2,
      "spektrafilm_version": "0.3.3",
      "base": "packs/0.3.3",
      "default": true,
      "files": [
        { "path": "pack.json", "size": 71631, "sha256": "c70fad39…" }
      ]
    }
  ]
}
```

Every file is verified against its checksum before it is installed, and a file
with no checksum is refused outright. A corrupt spectral LUT does not fail
loudly — it renders plausibly wrong — which is why there is no unverified path.

The pack flagged `default` is what a fresh edit gets. Every other pack is only
ever fetched when an edit explicitly asks for its hash.

### Pack format

`pack_format` versions the *container*, not the data. It is copied from
`pack.json` and republished here so darktable can tell from the manifest alone
whether it could load a pack — otherwise it only finds out after downloading
the whole thing.

It is mandatory in both places. There is no format that predates the field, so
a pack without one is a hand-edited or truncated `pack.json`, not an older
revision — darktable refuses it and `make_manifest.py` refuses to publish it.

A darktable build declares the range it reads (`SF_PACK_FORMAT_MIN`/`MAX`).
Packs outside it are skipped during selection, and asking for one specifically
reports "that data pack needs a newer darktable" rather than a generic failure.
An older pack an old edit needs therefore stays fetchable indefinitely, as long
as its format is still supported and the entry is still published.

Bump `pack_format` only when the layout changes in a way an older reader would
get *wrong*. Adding a field an older reader ignores is not that; moving or
redefining one is. `pack.json` is a permissive JSON object, so a silently
changed meaning would parse cleanly and render incorrectly — the version is the
only thing standing between that and a clear error.

## Adding a pack

1. Export it from the spektrafilm Python package with

   ```sh
   ./tools/spektrafilm_export_data.py -o packs/<version>
   ```

   A release shipping the spectral-LUT registry exports several upsampling
   tables and writes `pack_format` 3; pick them with `--tables` and say which
   one a fresh edit gets with `--default-table`. An older release has one
   table and writes `pack_format` 2.
2. Drop it in as `packs/<version>/`.
3. Regenerate the manifest and commit:

   ```sh
   ./tools/make_manifest.py          # or pass the repo root explicitly
   ```

   With no argument it assumes it is sitting in `<repo>/tools/`, so it works
   from anywhere in the checkout. It also accepts the repo root, `packs/`, or a
   single pack directory.

4. Note the export in `CHANGELOG.txt`, which is where the license asks changes
   be recorded.
5. Push to `main`, which is what the `ref` preference tracks by default. If you
   cut a tag instead, remember that darktable reads whichever ref the
   preference names, not the newest one.

### Checking a pack before publishing

```sh
./tools/check_profiles.py packs/0.3.3
```

Each profile carries its density curves twice — sampled in `density_curves`,
and as fitted sigmoid parameters in `density_curves_model` — and darktable
renders from the model. This reconstructs every model row and reports which
sampled column it reproduces, which catches a bad fit before it ships as a
wrong render.

It also settles an ambiguity the file format leaves open: the model's outer
axis is the channel for a colour stock and the development time for a
mono one, and nothing in the file says which. Reconstruction proves it.
`--strict` additionally fails on profiles that read correctly only because
their model rows are identical copies — correct by accident, and only until
the exporter changes.

`make_manifest.py` derives everything from the files themselves — hand-editing
the manifest makes it drift from the pack, and the module's failure mode for
that is a checksum error with no explanation. It also enforces the same limits
the module does (path shape, file count, size caps), refuses two packs carrying
the same table hash, and checks each LUT's payload length against its own
header. That last one matters: a truncated LUT passes the module's cheap header
check and only shows up later as a wrong render.

## Licensing

**The data is CC BY-SA 4.0, © 2026 Andrea Volpato.** Every profile carries that
notice in its own `metadata` block. `LICENSE` is a byte-exact copy of upstream's
[`SPEKTRAFILM_LICENSE.txt`](https://github.com/andreavolpato/spektrafilm/blob/main/SPEKTRAFILM_LICENSE.txt),
which contains the full CC BY-SA 4.0 legal text preceded by the author's own
preamble. It is copied unchanged because the author asks that it not be
modified; changes to the packaging go in `CHANGELOG.txt` instead.

If you redistribute these files or anything derived from them — in any format,
in any context — the attribution must travel with them:

```
spektrafilm by Andrea Volpato
https://github.com/andreavolpato/spektrafilm
Licensed CC BY-SA 4.0
```

Derivatives stay CC BY-SA 4.0 and must note that they were modified. Read
`LICENSE` in full; the author also states several things asked beyond the legal
floor, including not repackaging the data as a paid product and not training
commercial AI models on it.

**The tooling is GPL-3.0-or-later**, matching darktable, and covers only
`tools/`. It is not a derivative of the profiles and does not encode any of
their content.

The spektrafilm *code* upstream is GPLv3; only the profile and LUT data is
CC BY-SA 4.0. This repository contains data, not code, apart from `tools/`.

### Data provenance

The profiles were built by processing published measurement data. Each one
records its own sources in `metadata.datasource`; in summary they draw on Kodak
and Fujifilm datasheets, scientific publications and technical material, and
these public reflectance datasets:

- [Otsu et al. spectral reflectance](https://github.com/enneract/otsu2018)
- [Munsell colours](https://zenodo.org/records/3269912)
- [NIST human skin reflectance](https://www.nist.gov/programs-projects/reflectance-measurements-human-skin)
- [Forest colours](https://zenodo.org/records/3269920)
- [Japan colours](https://zenodo.org/records/5217752)

Original measurement data remains the property of the respective holders.

## Citing

If you use these profiles in your work, please cite the spektrafilm project —
see [`CITATION.cff`](https://github.com/andreavolpato/spektrafilm) upstream.

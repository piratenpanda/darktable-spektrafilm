#!/usr/bin/env python3
"""Export a spektrafilm release into a data pack for the native darktable module.

This is the version-upgrade mechanism: when a new spektrafilm release comes out,

    pip install <new spektrafilm>          (or pip install -e <checkout>)
    python spektrafilm_export_data.py -o ~/.config/darktable/spektrafilm

and the darktable module picks up the new profiles / data on restart. The
module itself contains only the *algorithms* (which track the spektrafilm
model version recorded in pack.json); all measured data lives in this pack.

Pack layout:
    pack.json           model constants, CMFS, spectral locus, illuminant SPDs,
                        dichroic filter curves, neutral print filter database,
                        and (pack_format 3) the spectral_upsampling declaration
    spectra_lut.f32     the default spectral upsampling table (header + float16)
    spectra_lut_<id>.f32  one per further table, pack_format 3 only
    profiles/*.json     verbatim spektrafilm stock profiles (CC BY-SA 4.0)

A release that ships the spectral-LUT registry (the .toml sidecars beside the
.npy files, spektrafilm 0.3.4+) exports every table named by --tables and
writes pack_format 3. One that does not has exactly one table to export and
writes pack_format 2, unchanged.
"""

import argparse
import json
import math
import shutil
import struct
import sys
from pathlib import Path

import numpy as np


def _chan3(v, bw):
    """Normalise a per-channel parameter to exactly three values.

    For a colour stock the three are passed through. For a single-emulsion (B&W)
    stock there is only one channel: upstream reaches every per-channel constant
    through match_channels(values, n_ch), which at n_ch == 1 returns values[:1] --
    the FIRST channel, for all of them. Since the C engine runs on the widened
    3-channel profile, that first value has to be replicated, or a B&W frame is
    rendered with chromatic parameters. Schema defaults make this concrete:
    rms_granularity is (6, 8, 10), scatter_core_um (2.2, 2.0, 1.6),
    halation_strength (0.05, 0.015, 0.0) -- none of which are achromatic.

    None-padded singles ([x, None, None]) collapse the same way.
    """
    v = [x for x in v if x is not None]
    if not v:
        return v
    if bw or len(v) == 1:
        return [v[0]] * 3
    return list(v)


def _grain_export(params, bw):
    gr = params.film_render.grain
    out = {
        "rms_granularity": _chan3(gr.rms_granularity, bw),
        "uniformity": _chan3(gr.uniformity, bw),
        "density_min": _chan3(gr.density_min, bw),
    }
    if hasattr(gr, "particle_scale_sublayers"):
        # sub-layer scales, not channels -- never collapsed
        out["particle_scale_sublayers"] = list(gr.particle_scale_sublayers)
    return out


def _fnv1a(data):
    """FNV-1a over the table's raw bytes. The engine reads this value and never
    recomputes it, so any stable hash would do; it is what every edit records
    and what a download is matched against."""
    h = 2166136261
    for b in data:
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h


def _write_lut(path, arr, lut_id):
    """Write one table in the SFS2 container and return its content hash."""
    lut = np.ascontiguousarray(arr, dtype=np.float16)
    payload = lut.tobytes()
    h = _fnv1a(payload)
    ident = lut_id.encode("utf-8")[:255]
    with open(path, "wb") as fh:
        fh.write(b"SFS2")
        fh.write(struct.pack("<i", 2))                 # header version
        fh.write(struct.pack("<iii", *lut.shape))
        fh.write(struct.pack("<i", 1))                 # 0 = float32, 1 = float16
        fh.write(struct.pack("<I", h))                 # content hash
        fh.write(struct.pack("<i", len(ident)))
        fh.write(ident)
        fh.write(payload)
    print(f"wrote {path} shape {lut.shape} float16 id={ident.decode()} hash={h:08x}")
    return h


def _lut_id_stem(descriptor, identifier):
    """The table's name in the header, from its .npy file name with the method
    prefix dropped: arctic2026beta04_reflectance_xy_tc -> reflectance_xy_tc.

    The kind is what survives, which is deliberate -- pack.json carries the
    identifier already, and a reader that has only the header can still tell a
    reflectance table from an irradiance one, which is the distinction that
    renders wrongly rather than failing if it is got wrong."""
    stem = str(descriptor.get("file", identifier))
    if stem.endswith(".npy"):
        stem = stem[:-4]
    prefix = identifier + "_"
    return stem[len(prefix):] if stem.startswith(prefix) else stem


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", required=True, help="output pack directory")
    ap.add_argument(
        "--tables", default="hanatos2025,arctic2026beta04",
        help="spectral upsampling tables to export, comma separated. Ignored by "
             "a release with no LUT registry, which has only one. Every table "
             "adds ~5.7 MB to the pack, so this is a selection and not all of "
             "them (default: %(default)s)")
    ap.add_argument(
        "--default-table", default="hanatos2025",
        help="the table a fresh edit renders with (default: %(default)s)")
    args = ap.parse_args()

    try:
        import spektrafilm  # noqa: F401
        from importlib.metadata import version as dist_version
        import importlib.resources as pkg_resources
        from spektrafilm.config import SPECTRAL_SHAPE, STANDARD_OBSERVER_CMFS, LOG_EXPOSURE
        from spektrafilm.model.illuminants import standard_illuminant
        from spektrafilm.model.color_filters import DichroicFilters
        from spektrafilm.utils.io import read_neutral_print_filters
        from spektrafilm.utils.spectral_upsampling import _load_hanatos2025_spectra_lut
        from spektrafilm.utils.gamut_compression import spectral_locus_xy
    except ImportError as err:
        print(f"error: spektrafilm must be importable ({err})", file=sys.stderr)
        return 1

    # The generic spectral-LUT registry (0.3.4+). Its absence is not an error:
    # a release without it ships exactly one table and exports as it always did.
    try:
        from spektrafilm.utils.spectral_upsampling import (
            get_lut_spectra, lut_descriptor)
    except ImportError:
        get_lut_spectra = lut_descriptor = None

    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "profiles").mkdir(exist_ok=True)

    wl = SPECTRAL_SHAPE.wavelengths
    version = dist_version("spektrafilm")

    # --- illuminants (normalized SPDs on the model wavelength grid) ---------
    illuminant_names = ["D50", "D55", "D65", "D75", "T", "TH-KG3", "TH-KG3-L", "K75P"]
    illuminants = {}
    for name in illuminant_names:
        try:
            illuminants[name] = np.asarray(standard_illuminant(name), dtype=float).tolist()
        except Exception as err:  # keep the pack usable if one SPD is missing
            print(f"warning: skipping illuminant {name}: {err}", file=sys.stderr)

    # --- enlarger dichroic filters ------------------------------------------
    filter_brands = ["thorlabs", "edmund_optics", "durst_digital_light", "custom"]
    dichroics = {}
    for brand in filter_brands:
        try:
            dichroics[brand] = np.asarray(DichroicFilters(brand=brand).filters, dtype=float).tolist()
        except Exception as err:
            print(f"warning: skipping dichroic brand {brand}: {err}", file=sys.stderr)

    # --- neutral print filter database --------------------------------------
    try:
        neutral_filters = read_neutral_print_filters()
    except FileNotFoundError:
        neutral_filters = {}

    # --- per-film digested render defaults -----------------------------------
    # Stock-specific tuning (DIR coupler gamma matrices, halation presets)
    # lives in spektrafilm's params_builder. Exporting the digested values per
    # film keeps that tuning in the data pack so the native module needs no
    # hardcoded per-stock tables and upgrades cleanly with new releases.
    from spektrafilm.runtime.params_builder import init_params, digest_params

    film_render_defaults = {}
    for res in pkg_resources.files("spektrafilm.data.profiles").iterdir():
        if not res.name.endswith(".json"):
            continue
        stock = res.name[:-5]
        with pkg_resources.as_file(res) as p:
            info = json.loads(Path(p).read_text()).get("info", {})
        if info.get("stage") != "filming":
            continue
        target_print = info.get("target_print") or "kodak_portra_endura"
        try:
            params = digest_params(init_params(film_profile=stock, print_profile=target_print))
        except Exception as err:
            print(f"warning: could not digest defaults for {stock}: {err}", file=sys.stderr)
            continue
        dc = params.film_render.dir_couplers
        ha = params.film_render.halation
        bw = bool(getattr(params.film, "is_bw", False))
        if bw:
            # Single emulsion: upstream's matrix is 1x1 self-inhibition
            # (compute_dir_couplers_matrix populates M_inter only for n_ch == 3),
            # so it uses gamma_samelayer_rgb[0] and its interlayer entries are
            # inert. They are NOT zero in couplers.toml -- defaults.bw.negative
            # carries the colour values verbatim, deliberately -- so on the
            # widened 3-channel profile they have to be zeroed here or every
            # channel picks up its whole matrix COLUMN instead of the diagonal.
            g0 = dc.gamma_samelayer_rgb[0]
            dc.gamma_samelayer_rgb = (g0, g0, g0)
            dc.gamma_interlayer_r_to_gb = (0.0, 0.0)
            dc.gamma_interlayer_g_to_rb = (0.0, 0.0)
            dc.gamma_interlayer_b_to_rg = (0.0, 0.0)
        film_render_defaults[stock] = {
            "dir_couplers": {
                "gamma_samelayer_rgb": list(dc.gamma_samelayer_rgb),
                "gamma_interlayer_r_to_gb": list(dc.gamma_interlayer_r_to_gb),
                "gamma_interlayer_g_to_rb": list(dc.gamma_interlayer_g_to_rb),
                "gamma_interlayer_b_to_rg": list(dc.gamma_interlayer_b_to_rg),
                "diffusion_size_um": dc.diffusion_size_um,
                "diffusion_tail_um": dc.diffusion_tail_um,
                "diffusion_tail_weight": dc.diffusion_tail_weight,
                # Langmuir saturating couplers (spektrafilm dev/0.4+); absent
                # on 0.3.x, in which case the engine uses the linear model
                **({"langmuir_donor_k_rgb": _chan3(dc.langmuir_donor_k_rgb, bw),
                    "langmuir_receiver_k_rgb": _chan3(dc.langmuir_receiver_k_rgb, bw)}
                   if hasattr(dc, "langmuir_donor_k_rgb") else {}),
            },
            "grain": _grain_export(params, bw),
            "halation": {
                "strength": _chan3(ha.halation_strength, bw),
                "first_sigma_um": _chan3(ha.halation_first_sigma_um, bw),
                "scatter_core_um": _chan3(ha.scatter_core_um, bw),
                "scatter_tail_um": _chan3(ha.scatter_tail_um, bw),
                "scatter_tail_weight": _chan3(ha.scatter_tail_weight, bw),
            },
        }

    # --- spectral upsampling tables ------------------------------------------
    # Upstream revises these often (arctic2026 alpha, alpha02, beta01..beta04 so
    # far) and each revision changes every render, so the engine has to be able
    # to tell one from another rather than silently rendering differently. The
    # content hash is authoritative; the name only makes the mismatch message
    # readable.
    #
    # A release carrying the registry can ship several tables in one pack, which
    # is what pack_format 3 declares. Each is written to its own file and named
    # in pack.json, and the default one keeps the plain spectra_lut.f32 name.
    tables = []
    if lut_descriptor is not None:
        wanted = [t.strip() for t in args.tables.split(",") if t.strip()]
        if args.default_table not in wanted:
            print(f"error: --default-table {args.default_table} is not in --tables",
                  file=sys.stderr)
            return 1
        # Default first, so the pack reads in precedence order.
        wanted.sort(key=lambda t: t != args.default_table)
        for identifier in wanted:
            try:
                descriptor = lut_descriptor(identifier)
            except KeyError as err:
                print(f"error: {err}", file=sys.stderr)
                return 1
            kind = descriptor.get("kind")
            if kind not in ("irradiance", "reflectance"):
                print(f"error: table {identifier} has kind {kind!r}", file=sys.stderr)
                return 1
            is_default = identifier == args.default_table
            fname = "spectra_lut.f32" if is_default else f"spectra_lut_{identifier}.f32"
            lut_id = f"{_lut_id_stem(descriptor, identifier)}@{version}"
            _write_lut(out / fname, get_lut_spectra(identifier), lut_id)
            entry = {"identifier": identifier, "kind": kind, "file": fname}
            if kind == "reflectance":
                # Recovered under this white, and the runtime must project
                # chromaticity under the same one; without it the table renders
                # with the wrong input adaptation and nothing says so.
                scene = descriptor.get("reflectance", {}).get("scene_illuminant")
                if not scene:
                    print(f"error: reflectance table {identifier} declares no "
                          f"scene_illuminant", file=sys.stderr)
                    return 1
                if scene not in illuminants:
                    print(f"error: table {identifier} needs illuminant {scene}, "
                          f"which this pack does not carry", file=sys.stderr)
                    return 1
                entry["scene_illuminant"] = scene
            if is_default:
                entry["default"] = True
            tables.append(entry)
    else:
        # No registry: one table, and its name is whatever this release calls
        # the single .npy it ships.
        lut_name = "unknown"
        for attr in ("SPECTRAL_UPSAMPLING_LUT", "HANATOS2025_LUT_NAME"):
            try:
                from spektrafilm import config as _cfg
                lut_name = str(getattr(_cfg, attr))
                break
            except (ImportError, AttributeError):
                pass
        if lut_name == "unknown":
            try:
                lutdir = pkg_resources.files("spektrafilm.data.luts.spectral_upsampling")
                names = sorted(r.name[:-4] for r in lutdir.iterdir()
                               if r.name.endswith(".npy"))
                if names:
                    lut_name = names[-1]
            except Exception:
                pass
        _write_lut(out / "spectra_lut.f32", _load_hanatos2025_spectra_lut(),
                   f"{lut_name}@{version}")

    pack = {
        # 3 once the pack names its tables: a format 2 reader handed such a pack
        # loads spectra_lut.f32, ignores the rest, and then reports a table match
        # to an edit developed against one of the others.
        "pack_format": 3 if tables else 2,
        "spektrafilm_version": version,
        "film_render_defaults": film_render_defaults,
        "wavelengths": np.asarray(wl, dtype=float).tolist(),
        "log_exposure": np.asarray(LOG_EXPOSURE, dtype=float).tolist(),
        "cmfs": np.asarray(STANDARD_OBSERVER_CMFS[:], dtype=float).tolist(),
        "spectral_locus_xy": np.asarray(spectral_locus_xy(), dtype=float).tolist(),
        "illuminants": illuminants,
        "dichroic_filters": dichroics,
        "neutral_print_filters": neutral_filters,
    }
    if tables:
        pack["spectral_upsampling"] = tables
    def _sanitize(obj):
        """JSON has no NaN/Inf — emit null instead (readers treat null as NaN)."""
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        return obj

    # pack.json is written at the end of main(), once the profiles exist: its
    # pack_hash covers them, and a hash cannot be written before the thing it
    # covers has been produced.
    pack = _sanitize(pack)

    # --- stock profiles ------------------------------------------------------
    # Colour profiles are copied verbatim. Single-emulsion B&W profiles
    # (channel_model == "bw", spektrafilm dev/0.4+) have their per-channel
    # ARRAYS widened to the 3-channel layout the C engine expects: replicated,
    # and channel_density divided by 3 so the spectral sum over channels equals
    # the single emulsion's density spectrum. info.channel_model stays "bw" so
    # the module can couple the grain across channels and collapse per-channel
    # constants.
    #
    # The curve MODEL is never widened -- that is the pack_format 2 change.
    # Its outer axis is the channel for a colour stock and the development time
    # for a mono one, and nothing in the file says which, so the reader had to
    # guess from the array shape:
    #
    #     dev_major = dev_family || (bw && outer_len != 3)
    #
    # It guessed because this exporter was inconsistent: a mono stock with a
    # development family kept its n_dev rows, while one without had its single
    # row replicated to three, so the same field arrived in two layouts within
    # one pack. Both readings then had to be supported, and the tiebreak was the
    # row count -- which is data, not metadata. That guess has been wrong before
    # (an earlier pack fed Double-X's model as channel-major and rendered from a
    # curve off by 1.34 density) and it currently survives only because the one
    # replicated stock, Tri-X, has three identical rows.
    #
    # Leaving the model at its natural row count makes the rule follow from
    # declared metadata instead: outer axis is development time exactly when
    # channel_model == "bw". Verify with tools/check_profiles.py, which
    # reconstructs each model row and proves which curve column it reproduces.
    #
    # Development-time families are NOT collapsed here any more. Some B&W stocks
    # carry one density curve, base+fog spectrum and curve-model row per
    # development time (kodak_doublex: 4/5/6.5/9/12 min, kodak_2302: 2/3.5/5/7/9
    # min). Collapsing to upstream's default middle member at export time threw
    # that away and made the choice unreachable from the module; keeping the
    # family lets the module do what select_development_time() does, at render
    # time, from a slider. `development_time` carries the full list of times, and
    # its length is what tells the loader whether the arrays are a family or an
    # already-widened single member -- so old packs keep loading unchanged.
    #
    # A family is exported with the development axis LAST on the 2-D arrays
    # (density_curves (n_le, n_dev), base_density (n_wl, n_dev)) and FIRST on the
    # curve model (centers (n_dev, n_layers)), which is how upstream stores them;
    # the module selects a member and widens to 3 channels itself.
    profile_dir = pkg_resources.files("spektrafilm.data.profiles")
    profile_texts = {}
    n = nbw = nfam = 0
    for res in profile_dir.iterdir():
        if not res.name.endswith(".json"):
            continue
        with pkg_resources.as_file(res) as p:
            prof = json.loads(p.read_text())
        if prof.get("info", {}).get("channel_model") == "bw":
            d = prof["data"]
            times = d.get("development_time") or []
            n_dev = len(times)
            curves = d.get("density_curves") or []
            family = (n_dev > 1 and curves and isinstance(curves[0], list)
                      and len(curves[0]) == n_dev)
            if family:
                # Leave density_curves, base_density and the curve model as the
                # full family; only sanity-check that the model row count agrees,
                # since a mismatch there is what silently summed N development
                # fits together as if they were N sub-layers of one curve.
                model = d.get("density_curves_model") or {}
                for key in ("centers", "amplitudes", "sigmas", "alphas"):
                    arr = model.get(key)
                    if arr and len(arr) != n_dev:
                        # Was a warning that fell through to the single-member
                        # path. It cannot be: the reader pairs model row i with
                        # development time i, so a disagreement here means it
                        # renders one development time from another one's fit --
                        # silently, and differently depending on the slider.
                        print(f"error: {res.name}: density_curves_model.{key} has "
                              f"{len(arr)} rows but development_time has {n_dev}. "
                              f"These index the same axis and must agree.",
                              file=sys.stderr)
                        return 1
            if family:
                nfam += 1
            else:
                # Single member (or no family at all): the density curve is one
                # panchromatic column, replicated. The curve model is NOT
                # touched -- see the note above; its single row stays a single
                # row, which is what tells the reader the axis is development
                # time rather than channel.
                if "density_curves" in d and d["density_curves"] \
                   and isinstance(d["density_curves"][0], list) \
                   and len(d["density_curves"][0]) == 1:
                    d["density_curves"] = [row * 3 for row in d["density_curves"]]
            # log_sensitivity is one panchromatic curve either way
            if "log_sensitivity" in d and d["log_sensitivity"] \
               and isinstance(d["log_sensitivity"][0], list) and len(d["log_sensitivity"][0]) == 1:
                d["log_sensitivity"] = [row * 3 for row in d["log_sensitivity"]]
            if "channel_density" in d and d["channel_density"] \
               and isinstance(d["channel_density"][0], list) and len(d["channel_density"][0]) == 1:
                d["channel_density"] = [
                    [None if row[0] is None else row[0] / 3.0] * 3
                    for row in d["channel_density"]
                ]
            nbw += 1
        text = json.dumps(prof)
        (out / "profiles" / res.name).write_text(text)
        profile_texts[res.name] = text
        n += 1
    print(f"copied {n} profiles ({nbw} B&W, {nfam} carrying a development-time family)")

    # --- pack identity --------------------------------------------------------
    # The spectral table's hash identifies the table and nothing else, and a
    # release can carry the same table forward unchanged -- hanatos2025 is
    # byte-identical between 0.3.3 and 0.3.4. Two packs then present one
    # identity while rendering differently, because the profiles moved: across
    # those two releases every one of the 31 changed, by up to 0.42 density on
    # the print films. An edit that recorded only the table cannot say which it
    # was developed against, and nothing downstream can tell it was handed the
    # other one.
    #
    # So the pack gets an identity of its own, over everything that decides a
    # render: the model constants here, the profiles, and which tables are
    # carried. Same FNV-1a the tables use, over a canonical text, so the value
    # is reproducible from a pack directory alone by anyone who wants to check
    # it.
    ident = ["pack_format=%d" % pack["pack_format"],
             "spektrafilm_version=%s" % version,
             "constants=" + json.dumps({k: v for k, v in pack.items()
                                        if k != "spectral_upsampling"},
                                       sort_keys=True, separators=(",", ":"))]
    for t in tables:
        ident.append("table=%s:%s:%s" % (t["identifier"], t["kind"], t["file"]))
    for name in sorted(profile_texts):
        ident.append("profile=%s:%08x" % (name, _fnv1a(profile_texts[name].encode())))
    pack["pack_hash"] = "%08x" % _fnv1a("\n".join(ident).encode())

    (out / "pack.json").write_text(json.dumps(pack))
    print(f"wrote {out / 'pack.json'} (spektrafilm {version}, "
          f"pack_hash {pack['pack_hash']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())

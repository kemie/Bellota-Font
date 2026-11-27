#!/usr/bin/env python3
"""
Pre-build step for Bellota / Bellota Text.

glyphsLib (and therefore fontmake / gftools builder) ignores most custom
parameters set on Glyphs *instances* ("Rename Glyphs", "Decompose Glyphs",
"Remove Glyphs", "Remove Features"...). Bellota Text is entirely defined by
those parameters, so this script applies them itself.

For every *variable* instance (Glyphs "Variable Font Setting") found in the
.glyphs sources, it:

  1. converts the .glyphs file to a designspace + master UFOs (same glyphsLib
     call as fontmake),
  2. applies, in the same order as Glyphs.app:
       - Decompose Glyphs   (before renaming, so original shapes are kept)
       - Rename Glyphs      (A=A.ss01 swaps the two glyphs; unicodes stay
                             with the name, outlines/kerning follow the shape)
       - decomposes any component pointing at a glyph that "Remove Glyphs"
         will delete,
  3. keeps the static instances of the same family, so statics match the VF,
  4. writes sources/build/<FileName>.designspace and a manifest
     (sources/build/postbuild.json) listing the glyphs and features that the
     post-build step must remove from the compiled fonts.

Glyph and feature removal happen on the binaries (scripts/postbuild.py): the
fontTools subsetter prunes GSUB/GPOS/GDEF cleanly, which is much more robust
than editing the feature code.

The .glyphs files remain the single source of truth: change the instance
custom parameters in Glyphs.app and rebuild.
"""

import argparse
import fnmatch
import json
import logging
import math
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import glyphsLib
import glyphsLib.classes
import ufoLib2
from fontTools.pens.recordingPen import DecomposingRecordingPointPen
from ufo2ft.filters import FILTERS_KEY, loadFilters

log = logging.getLogger("prepare_sources")

# Instance parameters handled here, stripped before handing over to glyphsLib.
HANDLED_PARAMS = {
    "Rename Glyphs",
    "Decompose Glyphs",
    "Remove Glyphs",
    "Remove Features",
    "Filter",  # Glyphs.app plugins (RMXHarmonizer, dekink): not available outside Glyphs
    "Variable Font Origin",
    "fileName",
}

TMP_NAME = "__prepare_sources_swap_tmp__"
COMPONENT_INFO_KEY = "com.schriftgestaltung.Glyphs.ComponentInfo"


# --------------------------------------------------------------------------- #
# Glyphs-level preparation
# --------------------------------------------------------------------------- #

def instance_family(instance, font):
    return instance.familyName or font.familyName


def param(obj, name, default=None):
    value = obj.customParameters[name]
    return default if value is None else value


def pick_origin(font, vf_instance, default_location):
    """Master to use as the VF default (origin)."""
    master_ids = {m.id for m in font.masters}
    for candidate in (
        param(vf_instance, "Variable Font Origin"),
        param(font, "Variable Font Origin"),
    ):
        if candidate in master_ids:
            return candidate
    # The italic source points at a master ID that does not exist (copied
    # from the roman file): fall back to the master at the default location.
    for master in font.masters:
        for loc in param(master, "Axis Location", []):
            if loc.get("Location") == default_location:
                log.warning(
                    "  'Variable Font Origin' not found, using master '%s'", master.name
                )
                return master.id
    log.warning("  no origin found, using first master '%s'", font.masters[0].name)
    return font.masters[0].id


def _signed_area(points):
    return sum(
        x0 * y1 - x1 * y0
        for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1])
    ) / 2


def _offset_polygon(points, amount_x, amount_y):
    """Offset a closed straight-line polygon; positive = to the left of each
    edge (inside for a counter-clockwise contour)."""
    n = len(points)
    lines = []
    for i in range(n):
        (x0, y0), (x1, y1) = points[i], points[(i + 1) % n]
        dx, dy = x1 - x0, y1 - y0
        length = math.hypot(dx, dy)
        nx, ny = -dy / length * amount_x, dx / length * amount_y
        lines.append(((x0 + nx, y0 + ny), (x1 + nx, y1 + ny)))
    result = []
    for i in range(n):
        (p1, p2), (p3, p4) = lines[i - 1], lines[i]
        d = (p1[0] - p2[0]) * (p3[1] - p4[1]) - (p1[1] - p2[1]) * (p3[0] - p4[0])
        if abs(d) < 1e-9:
            result.append(p3)
            continue
        a = p1[0] * p2[1] - p1[1] * p2[0]
        b = p3[0] * p4[1] - p3[1] * p4[0]
        result.append((
            (a * (p3[0] - p4[0]) - (p1[0] - p2[0]) * b) / d,
            (a * (p3[1] - p4[1]) - (p1[1] - p2[1]) * b) / d,
        ))
    return result


def _gs_path(points):
    path = glyphsLib.classes.GSPath()
    path.closed = True
    for x, y in points:
        path.nodes.append(glyphsLib.classes.GSNode((x, y), "line"))
    return path


def outline_stroked_paths(font):
    """Glyphs 3 'stroke' paths (here only .notdef) are not supported by
    glyphsLib: expand straight-line strokes the way Glyphs does (checked
    against the exported .notdef)."""
    master_ids = {m.id for m in font.masters}
    for glyph in font.glyphs:
        for layer in glyph.layers:
            # only exported layers: masters, brace and bracket layers (not backups)
            name = layer.name or ""
            if layer.layerId not in master_ids and not ("{" in name or "[" in name):
                continue
            shapes, changed = [], False
            for shape in layer.shapes:
                attrs = getattr(shape, "attributes", None) or {}
                if not isinstance(shape, glyphsLib.classes.GSPath) or "strokeWidth" not in attrs:
                    shapes.append(shape)
                    continue
                changed = True
                if any(n.type != "line" for n in shape.nodes):
                    log.warning("  %s: curved stroke path dropped", glyph.name)
                    continue
                sw = float(attrs["strokeWidth"])
                sh = float(attrs.get("strokeHeight", sw))
                pts = [(n.position.x, n.position.y) for n in shape.nodes]
                if shape.closed:
                    if _signed_area(pts) < 0:
                        pts.reverse()  # make it counter-clockwise
                    pos = int(float(attrs.get("strokePos", 0)))
                    # strokePos 1: inside, -1: outside, 0/absent: centred
                    k_out, k_in = {1: (0, 1), -1: (1, 0)}.get(pos, (0.5, 0.5))
                    outer = _offset_polygon(pts, -sw * k_out, -sh * k_out) if k_out else pts
                    inner = _offset_polygon(pts, sw * k_in, sh * k_in) if k_in else pts
                    shapes += [_gs_path(list(reversed(inner))), _gs_path(outer)]
                else:
                    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                        length = math.hypot(x1 - x0, y1 - y0)
                        ox = -(y1 - y0) / length * sw / 2
                        oy = (x1 - x0) / length * sh / 2
                        quad = [(x0 - ox, y0 - oy), (x1 - ox, y1 - oy),
                                (x1 + ox, y1 + oy), (x0 + ox, y0 + oy)]
                        if _signed_area(quad) < 0:
                            quad.reverse()
                        shapes.append(_gs_path(quad))
            if changed:
                layer.shapes = shapes


WEIGHT_CLASSES = {
    "Thin": 100, "ExtraLight": 200, "Light": 300, "Regular": 400, "Medium": 500,
    "SemiBold": 600, "Bold": 700, "ExtraBold": 800, "Black": 900,
}


def check_axis_location(inst, family):
    """The wght 'Axis Location' of a static instance must match its weight
    class, otherwise glyphsLib builds a broken axis mapping (avar)."""
    locations = param(inst, "Axis Location")
    if not locations:
        return
    expected = WEIGHT_CLASSES.get(
        inst.weight if isinstance(inst.weight, str) and not inst.weight.isdigit()
        else None, None
    )
    if expected is None:
        try:
            expected = int(inst.weight)
        except (TypeError, ValueError):
            return
    fixed = []
    for loc in locations:
        loc = dict(loc)
        if loc.get("Axis") == "Weight" and float(loc["Location"]) != expected:
            log.warning(
                "  SOURCE BUG: '%s %s' has Axis Location %s, expected %s (fixed for "
                "this build, please fix it in Glyphs)",
                family, inst.name, loc["Location"], expected,
            )
            loc["Location"] = expected
        else:
            loc["Location"] = float(loc["Location"])  # '700' (str) → 700.0
        fixed.append(loc)
    inst.customParameters["Axis Location"] = fixed


def prepare_glyphs_font(glyphs_path, vf_instance_index, default_location):
    """Load a fresh copy of the .glyphs font, trimmed for one VF setting."""
    font = glyphsLib.GSFont(str(glyphs_path))
    vf = font.instances[vf_instance_index]
    family = instance_family(vf, font)

    font.customParameters["Variable Font Origin"] = pick_origin(
        font, vf, default_location
    )
    members = [
        inst for inst in font.instances
        if inst.type == 0 and instance_family(inst, font) == family
    ]
    font.familyName = family

    statics = []
    for inst in members:
        inst.active = True  # Bellota Text statics are inactive in the source
        for name in HANDLED_PARAMS:
            del inst.customParameters[name]
        # Normalise style linking (several italic instances lack the flag)
        words = inst.name.split()
        inst.isItalic = "Italic" in words
        inst.isBold = [w for w in words if w != "Italic"] == ["Bold"]
        check_axis_location(inst, family)
        statics.append(inst)
    font.instances = statics
    outline_stroked_paths(font)

    transforms = {
        "decompose": list(dict.fromkeys(param(vf, "Decompose Glyphs", []))),
        "rename": list(param(vf, "Rename Glyphs", [])),
        "remove_glyphs": list(param(vf, "Remove Glyphs", [])),
        "remove_features": list(param(vf, "Remove Features", [])),
        "ignored_filters": [str(param(vf, "Filter"))] if param(vf, "Filter") else [],
    }
    return font, vf, family, transforms


# --------------------------------------------------------------------------- #
# UFO-level transformations
# --------------------------------------------------------------------------- #

BAKED_FILTERS = {("glyphsLib.filters", "cornerComponents"), ("glyphsLib.filters", "eraseOpenCorners")}


def bake_glyphs_filters(ufos):
    """Corner components are normally applied by a ufo2ft filter at compile
    time. Decomposing a glyph before that would lose the corners of its
    components, so apply them now, in the masters, and drop the filters."""
    for ufo in ufos:
        entries = ufo.lib.get(FILTERS_KEY, [])
        todo = [e for e in entries if (e.get("namespace"), e.get("name")) in BAKED_FILTERS]
        if not todo:
            continue
        pre, _ = loadFilters(SimpleNamespace(lib={FILTERS_KEY: todo}))
        for flt in pre:
            flt(ufo)
        ufo.lib[FILTERS_KEY] = [e for e in entries if e not in todo]
        if not ufo.lib[FILTERS_KEY]:
            del ufo.lib[FILTERS_KEY]


def iter_layers(ufos):
    for ufo in ufos:
        for layer in ufo.layers:
            yield ufo, layer


def decompose(layer, glyph, only=None):
    """Decompose all components of `glyph` (or only those whose base is in `only`)."""
    pen = DecomposingRecordingPointPen(
        layer, skipMissingComponents=True, reverseFlipped=True
    )
    kept = []
    for component in glyph.components:
        if only is None or component.baseGlyph in only:
            component.drawPoints(pen)
        else:
            kept.append(component)
    glyph.components = kept
    pen.replay(glyph.getPointPen())


def apply_decompose(ufos, names):
    missing = set(names)
    for _, layer in iter_layers(ufos):
        for name in names:
            if name in layer:
                missing.discard(name)
                if layer[name].components:
                    decompose(layer, layer[name])
    for name in sorted(missing):
        log.warning("  Decompose Glyphs: '%s' not in font, skipped", name)


def parse_renames(pairs, default_layer):
    swaps = []
    for pair in pairs:
        a, _, b = (s.strip() for s in pair.partition("="))
        if a in default_layer and b in default_layer:
            swaps.append((a, b))
        else:
            log.warning("  Rename Glyphs: '%s' skipped (glyph missing)", pair)
    return swaps


def make_swapper(swaps):
    mapping = {}
    for a, b in swaps:
        mapping[a], mapping[b] = b, a

    def swap(name):
        return mapping.get(name, name)

    return swap


def apply_renames(ufos, designspace, swaps):
    """Reproduce Glyphs.app 'Rename Glyphs' (checked against VFs exported by
    Glyphs 3 for every composite of both Text families):

    - the two glyphs of a pair exchange names; unicodes stay with the name,
      outlines, anchors, kerning and groups follow the shape;
    - component references *inside the swapped glyphs* are remapped too
      (agrave.ss01 = a.ss01 + grave becomes agrave = a + grave);
    - every other composite keeps referencing by name (Aacute = A + acute
      now uses the new A).
    """
    swap = make_swapper(swaps)
    swapped = {n for pair in swaps for n in pair}

    for _, layer in iter_layers(ufos):
        for a, b in swaps:
            ga, gb = layer.get(a), layer.get(b)
            if ga is None or gb is None:
                continue
            ua, ub = list(ga.unicodes), list(gb.unicodes)
            layer.renameGlyph(a, TMP_NAME)
            layer.renameGlyph(b, a)
            layer.renameGlyph(TMP_NAME, b)
            layer[a].unicodes = ua
            layer[b].unicodes = ub
        for name in swapped:
            if name in layer:
                glyph = layer[name]
                for component in glyph.components:
                    component.baseGlyph = swap(component.baseGlyph)
                for info in glyph.lib.get(COMPONENT_INFO_KEY, []):
                    info["name"] = swap(info["name"])

    for ufo in ufos:
        ufo.kerning = {(swap(l), swap(r)): v for (l, r), v in ufo.kerning.items()}
        for group, members in list(ufo.groups.items()):
            ufo.groups[group] = [swap(n) for n in members]
        lib = ufo.lib
        for key in ("public.glyphOrder", "public.skipExportGlyphs"):
            if key in lib:
                lib[key] = [swap(n) for n in lib[key]]
        if "public.openTypeCategories" in lib:
            lib["public.openTypeCategories"] = {
                swap(n): c for n, c in lib["public.openTypeCategories"].items()
            }
        # public.postscriptNames is keyed by name and derived from unicodes,
        # which stay with the name: left untouched on purpose.

    if "public.skipExportGlyphs" in designspace.lib:
        designspace.lib["public.skipExportGlyphs"] = [
            swap(n) for n in designspace.lib["public.skipExportGlyphs"]
        ]
    for rule in designspace.rules:
        rule.subs = [(swap(a), swap(b)) for a, b in rule.subs]


def _anchors(glyph):
    return {a.name: (a.x, a.y) for a in glyph.anchors}


def _apply(t, point):
    xx, xy, yx, yy, dx, dy = t
    x, y = point
    return (xx * x + yx * y + dx, xy * x + yy * y + dy)


def _topological(layer):
    """Glyph names ordered so that components come before their composites."""
    order, seen = [], set()

    def visit(name, stack=()):
        if name in seen or name not in layer or name in stack:
            return
        for comp in layer[name].components:
            visit(comp.baseGlyph, stack + (name,))
        seen.add(name)
        order.append(name)

    for name in layer.keys():
        visit(name)
    return order


def realign_composites(ufos, swaps):
    """Glyphs re-aligns automatically aligned components at export time.

    After a swap, a composite such as Agrave (A + gravecomb.case, not swapped
    itself) uses the new A. Like Glyphs, re-place base components on the new
    advance widths, move marks with the base anchor they hang on (stacked
    marks and explicit component anchors included), update the composite's
    width and anchors, and propagate to composites of composites
    (Iegrave-cy = Ie-cy + grave, Ie-cy = E...). Glyphs only auto-aligns
    layers made of components only, and never components whose alignment
    is disabled.
    """
    swap = make_swapper(swaps)
    swapped = {n for pair in swaps for n in pair}
    count = 0
    for _, layer in iter_layers(ufos):
        snapshot = {g.name: (g.width, _anchors(g)) for g in layer}
        realigned = set()
        for name in _topological(layer):
            glyph = layer[name]
            if not glyph.components or len(glyph.contours):
                continue
            owner_swapped = name in swapped

            def old_state(base):
                if base in swapped and not owner_swapped and swap(base) in snapshot:
                    return snapshot[swap(base)]
                return snapshot[base]

            def changed(base):
                return (base in swapped and not owner_swapped) or base in realigned

            if not any(changed(c.baseGlyph) for c in glyph.components):
                continue
            infos = {i.get("index"): i for i in glyph.lib.get(COMPONENT_INFO_KEY, [])}
            anchor_delta = {}   # anchor name → (dx, dy) in composite space
            width_delta = 0.0   # accumulated advance change of base components
            for index, comp in enumerate(glyph.components):
                base = comp.baseGlyph
                if base not in layer:
                    continue
                info = infos.get(index, {})
                auto = info.get("alignment", 0) != -1
                new_w, new_a = layer[base].width, _anchors(layer[base])
                old_w, old_a = old_state(base) if changed(base) else (new_w, new_a)
                t_old = tuple(comp.transformation)
                attach = [n[1:] for n in new_a if n.startswith("_")]

                if index == 0 or not attach:  # base component
                    dx = width_delta if auto else 0.0
                    if auto and changed(base):
                        a = t_old[0]
                        start = t_old[4] + min(0.0, a * old_w)
                        dx += (start - min(0.0, a * new_w)) - t_old[4]
                        width_delta += abs(a) * (new_w - old_w)
                    t_new = t_old[:4] + (t_old[4] + dx, t_old[5])
                else:  # mark component
                    target = info.get("anchor") or next(
                        (n for n in attach if n in anchor_delta), None)
                    shift = anchor_delta.get(target, (0.0, 0.0)) if auto else (0.0, 0.0)
                    mark = "_" + (target or "")
                    if auto and changed(base) and mark in new_a and mark in old_a:
                        p, q = _apply(t_old, new_a[mark]), _apply(t_old, old_a[mark])
                        shift = (shift[0] - (p[0] - q[0]), shift[1] - (p[1] - q[1]))
                    t_new = t_old[:4] + (t_old[4] + shift[0], t_old[5] + shift[1])
                for n, p in new_a.items():
                    if not n.startswith("_"):
                        q = old_a.get(n, p)
                        np_, op_ = _apply(t_new, p), _apply(t_old, q)
                        anchor_delta[n] = (np_[0] - op_[0], np_[1] - op_[1])
                comp.transformation = t_new
            if infos.get(0, {}).get("alignment", 0) != -1:
                glyph.width = round(glyph.width + width_delta)
            for anchor in glyph.anchors:
                dx, dy = anchor_delta.get(anchor.name, (0.0, 0.0))
                anchor.x, anchor.y = anchor.x + dx, anchor.y + dy
            realigned.add(name)
            count += 1
    return count


def glyphs_to_remove(default_layer, patterns):
    return sorted(
        name
        for name in default_layer.keys()
        if any(fnmatch.fnmatchcase(name, p) for p in patterns)
    )


def decompose_references_to(ufos, removed, swaps, default_layer):
    """Like Glyphs, decompose components pointing at glyphs that 'Remove
    Glyphs' will delete. Returns the exported glyphs whose outline now comes
    from a swapped glyph through such a reference: after the swap they show
    the *other* style (e.g. Text eng → n.ss01, which is now the Bellota n).
    Glyphs.app produces the same result; these are listed for review, the fix
    is to add them to 'Decompose Glyphs' in the source."""
    removed = set(removed)
    swapped = {n for pair in swaps for n in pair}

    def uses_swapped(name, seen=()):
        if name in swapped:
            return True
        if name not in default_layer or name in seen:
            return False
        return any(uses_swapped(c.baseGlyph, seen + (name,))
                   for c in default_layer[name].components)

    skip = set()
    for ufo in ufos:
        skip |= set(ufo.lib.get("public.skipExportGlyphs", []))
    suspicious = sorted(
        g.name for g in default_layer
        if g.name not in removed and g.name not in skip
        and any(c.baseGlyph in removed and uses_swapped(c.baseGlyph) for c in g.components)
    )
    count = 0
    for _, layer in iter_layers(ufos):
        for glyph in layer:
            if glyph.name in removed:
                continue
            if any(c.baseGlyph in removed for c in glyph.components):
                decompose(layer, glyph, only=removed)
                count += 1
    return count, suspicious


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def build_target(glyphs_path, vf_index, out_dir, default_location):
    font, vf, family, tf = prepare_glyphs_font(glyphs_path, vf_index, default_location)
    file_name = param(vf, "fileName") or family.replace(" ", "")
    stem = file_name.split("[")[0]
    log.info("%s → %s (%s, %d statics)", glyphs_path.name, stem, family, len(font.instances))

    designspace = glyphsLib.to_designspace(
        font,
        ufo_module=ufoLib2,
        instance_dir="instance_ufos",
        generate_GDEF=True,
        store_editor_state=False,
        write_skipexportglyphs=True,
        minimal=True,
    )
    # Consistent RIBBI naming (the italic source has a stray
    # "Bellota Extralight" style-map family name on one instance)
    for inst in designspace.instances:
        words = inst.styleName.split()
        weight = " ".join(w for w in words if w != "Italic") or "Regular"
        italic = "Italic" in words
        ribbi = weight in ("Regular", "Bold")
        inst.styleMapFamilyName = family if ribbi else f"{family} {weight}"
        inst.styleMapStyleName = (
            ("bold " if weight == "Bold" else "") + ("italic" if italic else "")
        ).strip() or "regular"
        inst.localisedStyleMapFamilyName = {}
    ufos = list({id(s.font): s.font for s in designspace.sources}.values())
    default_ufo = designspace.findDefault().font
    default_layer = default_ufo.layers.defaultLayer

    bake_glyphs_filters(ufos)
    apply_decompose(ufos, tf["decompose"])
    swaps = parse_renames(tf["rename"], default_layer)
    apply_renames(ufos, designspace, swaps)
    realigned = realign_composites(ufos, swaps) if swaps else 0
    removed = glyphs_to_remove(default_layer, tf["remove_glyphs"])
    n, suspicious = decompose_references_to(ufos, removed, swaps, default_layer)

    log.info(
        "  decomposed %d, swapped %d pairs (%d composites re-aligned), %d glyphs "
        "to remove (%d composites decomposed), features to remove: %s",
        len(tf["decompose"]), len(swaps), realigned, len(removed), n,
        ", ".join(tf["remove_features"]) or "-",
    )
    if suspicious:
        log.warning(
            "  REVIEW: %d glyphs take the other style's outline through a removed "
            "glyph (same as Glyphs.app export); add them to 'Decompose Glyphs' if "
            "unintended: %s", len(suspicious), " ".join(suspicious),
        )
    for f in tf["ignored_filters"]:
        log.warning("  Filter '%s' is a Glyphs.app plugin: not applied", f)

    target_dir = out_dir / stem
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = set()
    for source in designspace.sources:
        if source.filename not in saved:
            source.font.save(target_dir / source.filename, overwrite=True)
            saved.add(source.filename)
    ds_path = out_dir / f"{stem}.designspace"
    for source in designspace.sources:
        source.filename = f"{stem}/{source.filename}"
        source.path = None
    designspace.write(ds_path)

    psnames = default_ufo.lib.get("public.postscriptNames", {})
    return {
        "designspace": ds_path.name,
        "family": family,
        "file_prefix": stem.split("-")[0],
        "italic": "Italic" in stem,
        "remove_features": tf["remove_features"],
        "remove_glyphs": sorted(set(removed) | {psnames.get(g, g) for g in removed}),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    parser.add_argument("glyphs", nargs="+", type=Path, help=".glyphs sources")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("sources/build"))
    parser.add_argument(
        "--default-location", type=float, default=400,
        help="axis location of the VF default, used when 'Variable Font "
        "Origin' points at a missing master (default: 400)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.output_dir.exists():
        shutil.rmtree(args.output_dir)
    args.output_dir.mkdir(parents=True)

    manifest = []
    for path in args.glyphs:
        font = glyphsLib.GSFont(str(path))
        vf_indices = [i for i, inst in enumerate(font.instances) if inst.type == 1]
        if not vf_indices:
            sys.exit(f"{path}: no Variable Font Setting found")
        for index in vf_indices:
            manifest.append(build_target(path, index, args.output_dir, args.default_location))

    (args.output_dir / "postbuild.json").write_text(json.dumps(manifest, indent=2))
    log.info("Wrote %d designspaces to %s", len(manifest), args.output_dir)


if __name__ == "__main__":
    main()

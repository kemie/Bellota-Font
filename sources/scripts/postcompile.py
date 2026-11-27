#!/usr/bin/env python3
"""
Post-compile step for Bellota / Bellota Text, run by gftools builder on every
compiled font (variable and static) through `postCompile` in config.yaml,
before STAT, autohinting and gftools fix.

Applies the "Remove Glyphs" and "Remove Features" custom parameters of the
Glyphs variable instances, as collected by prepare_sources.py in
build/postbuild.json. The fontTools subsetter drops the glyphs and features
and prunes GSUB/GPOS/GDEF accordingly (works on variable fonts too).

The builder hands over temporary files, so the font is identified by its
name table (typographic family) and its italic flag, not by its file name.

    postcompile.py --manifest build/postbuild.json IN.ttf OUT.ttf
"""

import argparse
import json
import sys
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont


def family_and_italic(font):
    name = font["name"]
    family = name.getDebugName(16) or name.getDebugName(1)
    style = name.getDebugName(17) or name.getDebugName(2) or ""
    italic = bool(font["OS/2"].fsSelection & 1) or "Italic" in style.split()
    return family, italic


def layout_features(font):
    tags = set()
    for tag in ("GSUB", "GPOS"):
        if tag in font and font[tag].table.FeatureList:
            tags |= {r.FeatureTag for r in font[tag].table.FeatureList.FeatureRecord}
    return tags


def subset_options(keep_features):
    o = subset.Options()
    o.layout_features = sorted(keep_features)
    o.layout_scripts = ["*"]
    o.layout_closure = False  # do not pull removed alternates back in via aalt
    o.glyph_names = True
    o.notdef_glyph = True
    o.notdef_outline = True
    o.recommended_glyphs = False
    o.legacy_kern = True
    o.symbol_cmap = True
    o.legacy_cmap = True
    o.name_IDs = ["*"]
    o.name_languages = ["*"]
    o.name_legacy = True
    o.hinting = True
    o.hinting_tables = ["*"]
    o.drop_tables = []
    o.passthrough_tables = True
    o.prune_unicode_ranges = False
    if hasattr(o, "prune_codepage_ranges"):
        o.prune_codepage_ranges = False
    o.ignore_missing_glyphs = True
    o.ignore_missing_unicodes = True
    return o


def main(argv=None):
    parser = argparse.ArgumentParser(description="Remove glyphs/features after compile")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text())
    font = TTFont(args.input)
    family, italic = family_and_italic(font)
    rules = [r for r in manifest if r["family"] == family and r["italic"] == italic]
    if len(rules) != 1:
        sys.exit(f"postcompile: no unique rule for '{family}' italic={italic}")
    rule = rules[0]

    order = font.getGlyphOrder()
    removed = set(rule["remove_glyphs"]) & set(order)
    removed.discard(".notdef")
    keep = [g for g in order if g not in removed]
    features_before = layout_features(font)
    keep_features = features_before - set(rule["remove_features"])
    unicodes = [u for u, g in font.getBestCmap().items() if g not in removed]

    subsetter = subset.Subsetter(subset_options(keep_features))
    subsetter.populate(glyphs=keep, unicodes=unicodes)
    subsetter.subset(font)
    font["name"].removeUnusedNames(font)  # orphaned ssXX UI names
    font.save(args.output)

    print(
        f"postcompile: {family}{' Italic' if italic else ''} "
        f"[{font['OS/2'].usWeightClass}{', VF' if 'fvar' in font else ''}]: "
        f"-{len(removed)} glyphs ({len(order)} → {len(font.getGlyphOrder())}), "
        f"features removed: {', '.join(sorted(features_before - keep_features)) or '-'}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()

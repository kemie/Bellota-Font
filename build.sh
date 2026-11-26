#!/usr/bin/env bash
# Build Bellota & Bellota Text (variable + static TTFs, webfonts).
#
#   ./build.sh            build everything into fonts/
#   ./build.sh --qa       ... then run fontbakery (Google Fonts profile)
#
# Steps
#   1. pre-build   sources/scripts/prepare_sources.py
#                  .glyphs → designspace/UFOs, applying the instance custom
#                  parameters that glyphsLib ignores (Rename / Decompose Glyphs)
#   2. build       gftools builder sources/config.yaml
#                  (its postCompile step runs sources/scripts/postcompile.py:
#                  Remove Glyphs / Remove Features)
#   3. post-build  optional QA
set -euo pipefail
cd "$(dirname "$0")"

QA=0
[[ "${1:-}" == "--qa" ]] && QA=1

# --- environment ------------------------------------------------------------
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  if [[ ! -d venv ]]; then
    echo "→ Creating virtualenv"
    python3 -m venv venv
    venv/bin/pip install -q --upgrade pip
    venv/bin/pip install -q -r requirements.txt
  fi
  # shellcheck disable=SC1091
  source venv/bin/activate
fi

GLYPHS_SOURCES=(
  sources/1-glyphs/Bellota.glyphs
  sources/1-glyphs/Bellota_italic.glyphs
)

# --- 1. pre-build -----------------------------------------------------------
echo "→ Preparing sources"
python3 sources/scripts/prepare_sources.py "${GLYPHS_SOURCES[@]}" -o sources/build 2>&1 \
  | grep -v -E "Running '|Parsing \.glyphs|Non-existent glyph class" || true

# --- 2. build ---------------------------------------------------------------
echo "→ Building fonts"
FAMILIES=(bellota bellotatext)
for family in "${FAMILIES[@]}"; do rm -rf "fonts/$family"; done
rm -rf fonts/variable fonts/ttf fonts/webfonts
gftools builder sources/config.yaml

# --- 3. post-build ----------------------------------------------------------
# One folder per family: fonts/<family>/{variable,ttf,webfonts}
echo "→ Sorting fonts by family"
for kind in variable ttf webfonts; do
  [[ -d "fonts/$kind" ]] || continue
  for file in "fonts/$kind"/*; do
    name=$(basename "$file")
    case "$name" in
      BellotaText-*|BellotaText\[*) family=bellotatext ;;
      Bellota-*|Bellota\[*)         family=bellota ;;
      *) echo "  unexpected file: $file" >&2; continue ;;
    esac
    mkdir -p "fonts/$family/$kind"
    mv "$file" "fonts/$family/$kind/"
  done
  rmdir "fonts/$kind"
done

if [[ $QA == 1 ]]; then
  if ! command -v fontbakery >/dev/null; then
    echo "fontbakery not found: pip install 'fontbakery[googlefonts]'" >&2
    exit 1
  fi
  mkdir -p out/fontbakery
  for family in "${FAMILIES[@]}"; do
    echo "→ Running fontbakery on $family"
    fontbakery check-googlefonts -l WARN --succinct \
      --html "out/fontbakery/$family.html" "fonts/$family/variable/"*.ttf || true
  done
fi

echo "✓ Done: fonts/bellota, fonts/bellotatext"
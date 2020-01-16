TTFDIR="./ttf"

## Unused as long as fontmake isn't used to generate fonts:
# rm -r $TTFDIR/*.ttf

## We're not using fontmake here at the moment, because it can't generate fonts using corner components (as of Jan. 2020)
## Neither can we build form UFOs out of Glyphs.app, because Glyphs doesn't apply the slant angle defined for the Italic instances.
## Instead, we're generating normal TTFs (remove overlaps, autohint) directly out of Glyphs, and then apply below changes.
# fontmake -g ./Glyphs/sen.glyphs -o ttf -i --output-dir $TTFDIR -a

for f in $TTFDIR/*.ttf
do
	gftools fix-dsig --autofix $f
	gftools fix-hinting $f
	mv $f.fix $f
done

# # Clean up
# rm -r instance_ufo
# rm -r master_ufo

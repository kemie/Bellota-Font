rm ./ttf/*.ttf

 # Build static instances
fontmake -g ./src/Bellota.glyphs -o ttf -i --output-dir ./ttf -a
fontmake -g ./src/Bellota\ Italics.glyphs -o ttf -i --output-dir ./ttf -a
for f in ./ttf/*.ttf
do
	echo 'Processing $f'
	gftools fix-dsig --autofix $f
	gftools fix-hinting $f
	mv $f.fix $f
done

# Clean up
rm -r instance_ufo
rm -r master_ufo

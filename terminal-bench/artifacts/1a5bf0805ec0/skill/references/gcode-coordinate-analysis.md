# Analyzing Coordinate/Path Data for Text Extraction

## When to use this reference

When a task requires decoding text or information from a file format where content
is encoded as 2D/3D coordinates, paths, or movements — gcode, SVG, DXF, HPGL,
CNC programs, plotter data, embroidery patterns, or any format where geometry
represents text rather than explicit string data.

## Overview

Text encoded in coordinate data (printer toolpaths, vector graphics, etc.)
cannot be read by grepping for strings — it exists only as the visual pattern
formed by the coordinates. The reliable extraction workflow uses **per-character
segmentation and OCR**, NOT full-image deskewing + OCR, and NEVER image_read
on the full visualization (which fabricates plausible but incorrect text).

## Step-by-step procedure

### 1. Run the extraction script

```bash
python3 /skill/scripts/gcode-extract-text.py /app/text.gcode /app/out.txt
```

This script performs the entire pipeline: parse extrusion coordinates from the
Embossed text M486 object → segment into characters using DBSCAN clustering
+ X-axis gap detection → render each character at high resolution → OCR each
character individually with tesseract --psm 10 (single character mode, no
dictionary) → concatenate results. It also cross-checks with full-image OCR
at multiple rotation angles.

### 2. Verify the script output

Read the script's output and the character-level details it prints. Check for
'?' characters (meaning OCR failed on that character). If any characters were
not recognized, inspect the corresponding /tmp/char_N.png images using
image_read on INDIVIDUAL character crops ONLY — never on the full visualization.

### 3. If the script fails entirely

If the script finds no points or cannot segment characters, the gcode may use
a non-standard structure. In that case:

1. Manually parse the gcode to understand its structure (object markers,
   layer markers, coordinate ranges).
2. Extract extrusion coordinates from the text-bearing object.
3. Segment characters by X-axis gaps in the rotated coordinate distribution.
4. Render each character separately and run `tesseract --psm 10` on each.

### 4. NEVER use image_read on the full coordinate visualization

image_read on a point-cloud visualization of gcode coordinates fabricates
plausible English words that do NOT match the actual text. This has been
observed in every prior attempt: image_read reported 'DREAM 3D' and 'HELLO
WORLD' when the actual text was a CTF-style flag. The Reference's own Key
Pitfalls warn against this, but agents still use image_read as a fallback
when OCR fails. **There is no valid use case for image_read on a full gcode
visualization.** Only use image_read on individual character crops after
segmentation, and even then only as a supplement to per-character OCR.

## Why per-character OCR instead of full-image deskewing + OCR

The previous pipeline (PCA deskew → rasterize full image → OCR) has three
failure modes that make it unreliable:

1. **PCA deskewing fails for text inside rectangular objects**: PCA computes
   the direction of maximum variance across ALL points, including infill and
   border geometry. When text is inside a rectangular bounding box, PCA
   often picks up the rectangle's diagonal rather than the text baseline,
   producing wrong rotation angles (e.g., -157.87 degrees observed in
   practice).

2. **Full-image OCR cannot read CTF flags**: OCR engines apply language
   model correction that transforms leet-speak ('gc0d3') into natural
   language ('geode' or 'gcode'). Per-character OCR with dictionary
   disabled avoids this.

3. **image_read fabricates text**: When OCR fails, agents fall back to
   image_read which invents plausible English words. This has produced
   wrong answers in every attempt.

Per-character segmentation avoids all three: no full-image deskewing needed
(single characters have no directional ambiguity), no language model
interference (dictionary disabled), and no image_read dependency.

## Key pitfalls

- **NEVER use image_read on the full visualization**: It fabricates plausible
  English text. This is not a matter of retrying for consistency — a single
  image_read call on a full gcode visualization will produce a confident but
  wrong answer (observed: 'DREAM 3D', 'HELLO WORLD', 'HI FRIEND').
- **The text may not be English**: CTF-style flags (flag{...}), encoded strings,
  and non-standard character sequences with leet-speak substitutions (0→o,
  3→e, 4→a) are common. Do not assume the text reads as natural language —
  OCR may struggle with mixed case, special characters ({, }, _, digits), and
  unusual spacing. Always disable the OCR dictionary for coordinate text.
- **Y-axis inversion**: Gcode Y+ may be image Y-. Always try FLIP_TOP_BOTTOM.
- **Travel vs extrusion moves**: Include only extrusion moves in the text
  visualization. Travel moves create noise that obscures the text.
- **Do NOT rotate by trial-and-error**: Trying 45, -45, 30, -30 degrees one
  at a time wastes turns. The script handles rotation automatically.
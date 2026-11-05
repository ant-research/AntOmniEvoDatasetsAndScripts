#!/usr/bin/env python3
"""Extract text from a gcode file by per-character OCR.

Usage: python3 gcode-extract-text.py <gcode_file> [output_file]

Extracts extrusion coordinates from 'Embossed text' M486 objects,
segments into individual characters using X-axis gap detection,
renders each character at high resolution in isolation, and
runs tesseract --psm 10 (single character mode) on each crop.
This avoids both PCA deskewing issues (no rotation needed for
single characters) and image_read hallucination (uses OCR, not
a vision model, with dictionary disabled for leet-speak/CTF flags).
"""
import sys
import re
import os
import subprocess
import tempfile
import numpy as np
from PIL import Image, ImageDraw


def parse_gcode_extrusion_points(filepath):
    """Extract extrusion (X, Y) points from the Embossed text M486 object."""
    current_x, current_y = 0.0, 0.0
    prev_e = 0.0
    in_text_object = False
    relative_extrusion = False  # M83 = relative, M82 = absolute
    points = []

    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            # Track extrusion mode
            if line == 'M83':
                relative_extrusion = True
                continue
            if line == 'M82':
                relative_extrusion = False
                continue

            # Track M486 object markers (Prusa-specific)
            if line.startswith('M486'):
                if 'S0' in line or ('A' in line and 'text' in line.lower()):
                    in_text_object = True
                elif 'S-1' in line:
                    in_text_object = False
                elif line.startswith('M486 S') and 'S0' not in line:
                    # S1, S2, etc. = other objects
                    in_text_object = False
                continue

            if not in_text_object:
                continue

            if line.startswith('G1') or line.startswith('G0'):
                x_match = re.search(r'X([\d.]+)', line)
                y_match = re.search(r'Y([\d.]+)', line)
                e_match = re.search(r'E([-.\d]+)', line)

                if x_match:
                    current_x = float(x_match.group(1))
                if y_match:
                    current_y = float(y_match.group(1))

                e_val = float(e_match.group(1)) if e_match else prev_e
                if relative_extrusion:
                    # M83: E is per-move increment; positive = extrusion
                    is_extrusion = e_match is not None and e_val > 0
                else:
                    # M82: E is cumulative position; increasing = extrusion
                    is_extrusion = e_match is not None and e_val > prev_e
                prev_e = e_val

                if is_extrusion:
                    points.append((current_x, current_y))

    return np.array(points) if points else np.array([]).reshape(0, 2)


def segment_characters(points, gap_threshold_factor=0.15):
    """Segment points into individual characters by X-axis gaps.

    Steps:
    1. Determine text baseline angle from character centroids
       (not PCA on all points, which is confused by rectangular borders).
    2. Rotate points to align text horizontally.
    3. Find X-axis gaps to separate characters.
    4. Return list of point arrays, one per character.
    """
    if len(points) < 10:
        return []

    xs, ys = points[:, 0], points[:, 1]

    # Step 1: Determine the text direction using a robust method.
    # Cluster points into rough groups, then fit a line through
    # the cluster centroids. This is more reliable than PCA
    # on all points when the text is inside a rectangular border.
    try:
        from sklearn.cluster import DBSCAN

        # Subsample for clustering speed if too many points
        if len(points) > 5000:
            indices = np.random.choice(len(points), 5000, replace=False)
            sample = points[indices]
        else:
            sample = points

        # Use DBSCAN to find dense clusters (candidate characters)
        # eps tuned for gcode coordinate scales
        x_range = xs.max() - xs.min()
        y_range = ys.max() - ys.min()
        eps = min(x_range, y_range) * 0.15

        clustering = DBSCAN(eps=eps, min_samples=5).fit(sample)
        labels = clustering.labels_

        # Compute centroids of non-noise clusters
        unique_labels = set(labels) - {-1}
        centroids = []
        for lbl in unique_labels:
            mask = labels == lbl
            cx = sample[mask, 0].mean()
            cy = sample[mask, 1].mean()
            centroids.append((cx, cy))

        if len(centroids) >= 2:
            # Fit a line through the centroids using least squares
            centroids = np.array(centroids)
            # First PC of centroids = text direction
            c_centered = centroids - centroids.mean(axis=0)
            c_cov = np.cov(c_centered.T)
            _, eigvecs = np.linalg.eigh(c_cov)
            principal = eigvecs[:, -1]
            angle = np.arctan2(principal[1], principal[0])
        else:
            # Fall back: use PCA on all points
            coords = np.column_stack([xs - xs.mean(), ys - ys.mean()])
            cov = np.cov(coords.T)
            eigenvalues, eigenvectors = np.linalg.eigh(cov)
            principal = eigenvectors[:, -1]
            angle = np.arctan2(principal[1], principal[0])
    except ImportError:
        # sklearn not available — fall back to PCA on all points
        coords = np.column_stack([xs - xs.mean(), ys - ys.mean()])
        cov = np.cov(coords.T)
        eigenvalues, eigenvectors = np.linalg.eigh(cov)
        principal = eigenvectors[:, -1]
        angle = np.arctan2(principal[1], principal[0])

    # Ensure the angle points rightward (principal component direction
    # is ambiguous — pick the one where x increases)
    if principal[0] < 0:
        angle += np.pi

    # Step 2: Rotate points to align text horizontally
    rot_angle = -angle
    cx, cy = xs.mean(), ys.mean()
    dx, dy = xs - cx, ys - cy
    rotated_x = dx * np.cos(rot_angle) - dy * np.sin(rot_angle)
    rotated_y = dx * np.sin(rot_angle) + dy * np.cos(rot_angle)

    # Step 3: Find X-axis gaps to separate characters
    x_min, x_max = rotated_x.min(), rotated_x.max()
    n_bins = min(200, int(x_max - x_min))
    hist, bin_edges = np.histogram(rotated_x, bins=n_bins)
    bin_width = bin_edges[1] - bin_edges[0]

    # Find gaps: consecutive bins with zero or near-zero density
    threshold = hist.max() * gap_threshold_factor
    in_gap = hist < threshold

    # Find gap regions
    gap_starts = []
    gap_ends = []
    in_gap_region = False
    for i, is_gap in enumerate(in_gap):
        if is_gap and not in_gap_region:
            gap_starts.append(bin_edges[i])
            in_gap_region = True
        elif not is_gap and in_gap_region:
            gap_ends.append(bin_edges[i])
            in_gap_region = False
    if in_gap_region:
        gap_ends.append(bin_edges[-1])

    # Character boundaries are midpoints of gaps
    boundaries = [x_min - bin_width]
    for gs, ge in zip(gap_starts, gap_ends):
        boundaries.append((gs + ge) / 2)
    boundaries.append(x_max + bin_width)
    boundaries.sort()

    # Step 4: Extract points for each character
    characters = []
    for i in range(len(boundaries) - 1):
        left, right = boundaries[i], boundaries[i + 1]
        mask = (rotated_x >= left) & (rotated_x <= right)
        char_points = np.column_stack([rotated_x[mask], rotated_y[mask]])
        if len(char_points) >= 5:  # minimum points for a character
            characters.append(char_points)

    return characters


def render_character(char_points, img_height=128, padding=10, dot_radius=3):
    """Render a character's points as a bitmap image."""
    xs, ys = char_points[:, 0], char_points[:, 1]

    x_range = xs.max() - xs.min()
    y_range = ys.max() - ys.min()
    if x_range < 0.001 or y_range < 0.001:
        return None

    # Scale to fit img_height pixels tall
    scale = (img_height - 2 * padding) / y_range
    img_w = int(x_range * scale) + 2 * padding
    img_h = img_height

    img = Image.new('L', (img_w, img_h), 255)  # white background
    draw = ImageDraw.Draw(img)

    for x, y in zip(xs, ys):
        px = int((x - xs.min()) * scale) + padding
        py = int((y - ys.min()) * scale) + padding
        draw.ellipse(
            [px - dot_radius, py - dot_radius, px + dot_radius, py + dot_radius],
            fill=0
        )

    # Y-axis may be inverted
    img = img.transpose(Image.FLIP_TOP_BOTTOM)
    return img


def ocr_character(img, tessconfig=''):
    """Run tesseract on a single character image."""
    with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
        img.save(tmp.name)
        tmp_path = tmp.name

    try:
        # psm 10 = single character mode
        # disable dictionary for CTF flags / leet-speak
        result = subprocess.run(
            ['tesseract', tmp_path, 'stdout', '--psm', '10',
             '-c', 'tessedit_char_whitelist=',
             '--oem', '3'],
            capture_output=True, text=True, timeout=10
        )
        text = result.stdout.strip()

        # If psm 10 returns empty, try psm 13 (raw line)
        if not text:
            result = subprocess.run(
                ['tesseract', tmp_path, 'stdout', '--psm', '13',
                 '--oem', '3'],
                capture_output=True, text=True, timeout=10
            )
            text = result.stdout.strip()

        # Filter to printable ASCII only
        text = ''.join(c for c in text if 32 <= ord(c) < 127)
        return text
    finally:
        os.unlink(tmp_path)


def main():
    if len(sys.argv) < 2:
        print('Usage: python3 gcode-extract-text.py <gcode_file> [output_file]')
        sys.exit(1)

    gcode_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    print(f'Extracting text from {gcode_file}...')

    # Step 1: Parse extrusion points
    points = parse_gcode_extrusion_points(gcode_file)
    if len(points) == 0:
        print('ERROR: No extrusion points found in embossed text object.')
        sys.exit(1)
    print(f'Found {len(points)} extrusion points.')

    # Step 2: Segment into characters
    characters = segment_characters(points)
    if not characters:
        print('ERROR: Could not segment characters from points.')
        sys.exit(1)
    print(f'Segmented {len(characters)} character regions.')

    # Step 3: Render and OCR each character
    decoded_chars = []
    for i, char_pts in enumerate(characters):
        img = render_character(char_pts)
        if img is None:
            decoded_chars.append('?')
            continue

        # Save character image for inspection
        char_path = f'/tmp/char_{i}.png'
        img.save(char_path)

        char_text = ocr_character(img)
        if char_text:
            decoded_chars.append(char_text)
        else:
            decoded_chars.append('?')
        print(f'  Char {i}: {repr(char_text)} (from {len(char_pts)} points)')

    result_text = ''.join(decoded_chars)
    print(f'\nDecoded text: {result_text}')

    # Also try full-image OCR with different rotations as a cross-check
    print('\nCross-check with full-image OCR (no deskewing)...')
    xs, ys = points[:, 0], points[:, 1]
    scale = 5.0
    x_range = xs.max() - xs.min()
    y_range = ys.max() - ys.min()
    img_w = int(x_range * scale) + 20
    img_h = int(y_range * scale) + 20
    full_img = Image.new('L', (img_w, img_h), 255)
    draw = ImageDraw.Draw(full_img)
    for x, y in zip(xs, ys):
        px = int((x - xs.min()) * scale) + 10
        py = int((y - ys.min()) * scale) + 10
        draw.ellipse([px-2, py-2, px+2, py+2], fill=0)
    full_img = full_img.transpose(Image.FLIP_TOP_BOTTOM)
    full_img.save('/tmp/full_raw.png')

    # Try common angles for deskewed full-image OCR
    for angle in [0, 45, -45, 90, -90]:
        rotated = full_img.rotate(angle, expand=True, fillcolor=255)
        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            rotated.save(tmp.name)
            tmp_path = tmp.name
        try:
            result = subprocess.run(
                ['tesseract', tmp_path, 'stdout', '--psm', '7',
                 '-c', 'tessedit_char_whitelist='],
                capture_output=True, text=True, timeout=10
            )
            full_text = result.stdout.strip()
            full_text = ''.join(c for c in full_text if 32 <= ord(c) < 127)
            if full_text:
                print(f'  Full OCR at {angle} deg: {repr(full_text)}')
        finally:
            os.unlink(tmp_path)

    # Write result
    if output_file:
        with open(output_file, 'w') as f:
            f.write(result_text)
        print(f'Written to {output_file}')


if __name__ == '__main__':
    main()
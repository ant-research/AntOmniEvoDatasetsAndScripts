#!/usr/bin/env python3
"""Extract readable text and financial amounts from PDF files using Python modules.

Handles common PDF stream encodings:
- FlateDecode (zlib compression)
- ASCII85Decode followed by FlateDecode
- Raw text streams

Also attempts to extract structured financial data:
- Total amounts (Total, Grand Total, Amount Due, TotalPrice)
- VAT/tax amounts (VAT, Tax, GST)

Usage: python3 extract-pdf-text.py <pdf_file> [output_file]
If output_file is given, write extracted text there; otherwise print to stdout.
"""
import sys
import re
import zlib
import base64


def extract_streams(filepath):
    """Extract and decompress all content streams from a PDF file."""
    with open(filepath, "rb") as f:
        data = f.read()

    results = []

    # Find all stream...endstream blocks
    streams = re.findall(rb"stream\r?\n(.+?)\r?\nendstream", data, re.DOTALL)

    for stream_data in streams:
        text = None

        # Try FlateDecode (zlib) first — most common encoding
        try:
            decompressed = zlib.decompress(stream_data)
            text = decompressed
        except zlib.error:
            pass

        # Try ASCII85Decode then FlateDecode — double-encoded streams
        if text is None:
            try:
                decoded = base64.a85decode(stream_data)
                decompressed = zlib.decompress(decoded)
                text = decompressed
            except Exception:
                pass

        # Try raw ASCII85Decode without subsequent FlateDecode
        if text is None:
            try:
                text = base64.a85decode(stream_data)
            except Exception:
                pass

        if text:
            results.append(text)

    # Also collect raw text strings from the PDF (outside streams)
    raw_strings = re.findall(rb'\(([^\)]+)\)', data)
    for s in raw_strings:
        try:
            decoded = s.decode('utf-8', errors='replace')
            if decoded.strip():
                results.append(decoded.encode('utf-8'))
        except Exception:
            pass

    return results


def streams_to_text(streams):
    """Convert decompressed streams to readable text."""
    text_parts = []
    for stream_data in streams:
        # Extract text inside parentheses (PDF text objects)
        texts = re.findall(rb'\(([^)]+)\)', stream_data)
        for t in texts:
            try:
                text_parts.append(t.decode('utf-8', errors='replace'))
            except Exception:
                pass
        # Also extract text from BT/ET blocks with Tj/TJ operators
        tj_texts = re.findall(rb'\[(.*?)\]TJ', stream_data)
        for tj_block in tj_texts:
            parts = re.findall(rb'\(([^)]+)\)', tj_block)
            for p in parts:
                try:
                    text_parts.append(p.decode('utf-8', errors='replace'))
                except Exception:
                    pass
    return ' '.join(text_parts)


def extract_financial_amounts(text):
    """Extract total and VAT amounts from PDF text.

    Priority order for total:
    1. Grand Total
    2. Total (if Amount Due also exists, prefer Total per task rules)
    3. TotalPrice
    4. Amount Due

    Returns dict with 'total_amount' and 'vat_amount' keys.
    """
    result = {'total_amount': None, 'vat_amount': None}

    # Normalize text for easier matching
    text_lower = text.lower()

    # Extract total amount — try patterns in priority order
    total_patterns = [
        (r'grand\s+total[:\s]*\$?([\d,]+\.?\d*)', 'grand_total'),
        (r'total(?:price|\s+price)?[:\s]*\$?([\d,]+\.?\d*)', 'total'),
        (r'amount\s+due[:\s]*\$?([\d,]+\.?\d*)', 'amount_due'),
    ]

    found_totals = {}
    for pattern, label in total_patterns:
        matches = re.findall(pattern, text_lower)
        if matches:
            # Take the LAST match (usually the bottom-of-page total)
            value = matches[-1].replace(',', '')
            try:
                found_totals[label] = float(value)
            except ValueError:
                pass

    # Priority: grand_total > total > totalprice > amount_due
    if 'grand_total' in found_totals:
        result['total_amount'] = found_totals['grand_total']
    elif 'total' in found_totals:
        result['total_amount'] = found_totals['total']
    elif 'amount_due' in found_totals:
        result['total_amount'] = found_totals['amount_due']

    # Extract VAT/tax amount
    vat_patterns = [
        r'(?:vat|tax|gst)[:\s]*\$?([\d,]+\.?\d*)',
        r'(?:vat|tax|gst)\s*[%]?\s*\$?([\d,]+\.?\d*)',
    ]
    for pattern in vat_patterns:
        matches = re.findall(pattern, text_lower)
        if matches:
            # Take the last match (usually the summary line)
            value = matches[-1].replace(',', '')
            try:
                result['vat_amount'] = float(value)
            except ValueError:
                pass

    return result


def extract_text(filepath):
    """Extract text and financial amounts from a PDF file."""
    streams = extract_streams(filepath)
    text = streams_to_text(streams)
    amounts = extract_financial_amounts(text)
    return text, amounts


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python3 extract-pdf-text.py <pdf_file> [output_file]",
            file=sys.stderr,
        )
        sys.exit(1)

    filepath = sys.argv[1]
    streams = extract_streams(filepath)
    text = streams_to_text(streams)
    amounts = extract_financial_amounts(text)

    output_parts = []
    # Diagnostic line: stream count and encoding info
    stream_count = len(streams)
    if stream_count == 0:
        output_parts.append("DIAGNOSTIC: No content streams found in PDF (possibly scanned image or empty). Try tesseract OCR on a rendered image instead.")
    else:
        output_parts.append(f"DIAGNOSTIC: {stream_count} content stream(s) extracted.")
    if not text.strip() and stream_count > 0:
        output_parts.append("DIAGNOSTIC: Streams decompressed but no readable text found (possibly encoded font or vector graphics). Try tesseract OCR on a rendered image instead.")
    if amounts['total_amount'] is not None:
        output_parts.append(f"TOTAL_AMOUNT: {amounts['total_amount']}")
    else:
        # Check if text contains any number that could be a total
        number_matches = re.findall(r'\d+[.,]?\d*', text)
        if number_matches and text.strip():
            output_parts.append("DIAGNOSTIC: No structured total pattern found (Total, Amount Due, Grand Total). Document may not be an invoice.")
    if amounts['vat_amount'] is not None:
        output_parts.append(f"VAT_AMOUNT: {amounts['vat_amount']}")
    output_parts.append("")
    output_parts.append(text)
    full_output = "\n".join(output_parts)

    if len(sys.argv) >= 3:
        with open(sys.argv[2], "w") as f:
            f.write(full_output)
        print(f"Extracted text written to {sys.argv[2]}")
    else:
        print(full_output)
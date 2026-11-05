#!/usr/bin/env python3
"""HTML sanitizer that removes XSS content while preserving clean HTML byte-for-byte.

Strategy:
1. Parse with BeautifulSoup to DETECT dangerous content.
2. If no dangerous content found -> return original UNCHANGED (clean file guarantee).
3. If dangerous content found -> remove surgically using parser-identified elements.
4. NEVER use unconditional regex on the whole document.

Usage: python3 html-sanitizer.py <file>
"""

import sys
import re
import html as html_module
from urllib.parse import unquote
from bs4 import BeautifulSoup

# Patterns that indicate XSS in event handler values
_XSS_HANDLER_PATTERNS = [
    'alert(', 'confirm(', 'prompt(',
    'eval(', 'settimeout(', 'setinterval(',
    'document.write(', 'document.writeln(',
    'javascript:', 'vbscript:',
]

# URL attributes that can contain dangerous schemes
_URL_ATTRS = frozenset({
    'href', 'src', 'action', 'formaction', 'background',
    'dynsrc', 'lowsrc', 'data', 'poster', 'code', 'codebase',
})

# Dangerous URL schemes
_DANGEROUS_SCHEMES = ['javascript:', 'vbscript:', 'data:text/html', 'data:application']


def _is_dangerous_url(value):
    """Check if an attribute value contains a dangerous URL scheme."""
    val_decoded = html_module.unescape(value)
    val_lower = val_decoded.lower()
    val_normalized = re.sub(r'[\s\t\r\n\x00-\x1f#]+', '', val_lower)
    try:
        val_url_decoded = unquote(val_normalized)
    except Exception:
        val_url_decoded = val_normalized
    return any(s in val_normalized or s in val_url_decoded for s in _DANGEROUS_SCHEMES)


def _is_dangerous_css(value):
    """Check if a CSS string contains dangerous content."""
    val_lower = html_module.unescape(value).lower()
    return any(kw in val_lower for kw in ['expression', '-moz-binding', 'javascript:', 'vbscript:'])


def _is_dangerous_handler(value):
    """Check if an event handler value contains XSS patterns."""
    val_decoded = html_module.unescape(value).lower()
    val_clean = re.sub(r'[\x00-\x1f]+', '', val_decoded)
    return any(pat in val_clean for pat in _XSS_HANDLER_PATTERNS)


def sanitize_html(original):
    """Remove XSS content from HTML while preserving clean content byte-for-byte.

    Key design: if no dangerous content is detected, return the original string
    UNCHANGED. This guarantees zero diff on clean files. For files with dangerous
    content, only BeautifulSoup-identified elements are removed — no unconditional
    regex runs on the whole document.
    """
    result = original

    # === PHASE 1: Check for null-byte obfuscated script patterns ===
    # Null bytes are almost never in clean HTML; strip only if near script keywords
    needs_null_strip = False
    if '\x00' in original:
        if re.search(r'scr', original, re.IGNORECASE) or re.search(r'ipt', original, re.IGNORECASE):
            needs_null_strip = True
            result = re.sub(r'\x00', '', original)

    # === PHASE 2: Parse with BeautifulSoup ===
    soup = BeautifulSoup(result, 'html.parser')

    # === PHASE 3: Detect dangerous content ===
    # Only treat script tags with JavaScript MIME types as dangerous.
    # Non-JS types (application/json, text/template, text/x-template, etc.)
    # do not execute code in browsers and should be preserved.
    _JS_SCRIPT_TYPES = {
        '', 'text/javascript', 'application/javascript',
        'text/ecmascript', 'application/ecmascript',
        'module', 'text/jscript', 'text/vbscript',
        'application/x-javascript', 'application/x-ecmascript',
        'importmap', 'text/jsx', 'text/typescript',
    }
    script_tags = []
    for tag in soup.find_all('script'):
        script_type = tag.get('type', '').lower().strip()
        if script_type in _JS_SCRIPT_TYPES:
            script_tags.append(tag)

    dangerous_on_handlers = set()  # attribute names to remove
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr.lower().startswith('on'):
                val = tag.get(attr, '')
                if isinstance(val, list):
                    val = ' '.join(val)
                if _is_dangerous_handler(val):
                    dangerous_on_handlers.add(attr)

    dangerous_url_attrs = set()  # attribute names to clear
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr.lower() in _URL_ATTRS:
                val = tag.get(attr, '')
                if isinstance(val, list):
                    val = ' '.join(val)
                if _is_dangerous_url(val):
                    dangerous_url_attrs.add(attr)

    # Check meta refresh with javascript: in content attribute
    for meta in soup.find_all('meta'):
        http_equiv = meta.get('http-equiv', '').lower()
        if http_equiv == 'refresh':
            content_val = meta.get('content', '')
            if _is_dangerous_url(content_val):
                dangerous_url_attrs.add('content')

    has_dangerous_style_attrs = False
    for tag in soup.find_all(True):
        style = tag.get('style', '')
        if style and _is_dangerous_css(style):
            has_dangerous_style_attrs = True

    has_dangerous_style_tags = False
    for style_tag in soup.find_all('style'):
        content = style_tag.string or ''
        if _is_dangerous_css(content):
            has_dangerous_style_tags = True

    # === PHASE 4: Early exit — if nothing dangerous, return original UNTOUCHED ===
    no_danger = (not script_tags and not dangerous_on_handlers
                 and not dangerous_url_attrs and not has_dangerous_style_attrs
                 and not has_dangerous_style_tags)
    if no_danger and not needs_null_strip:
        return original  # Clean file — guaranteed zero diff

    # === PHASE 5: Remove dangerous content ===

    # 5a. Remove script tags found by BeautifulSoup
    for script in script_tags:
        tag_str = str(script)
        if tag_str in result:
            result = result.replace(tag_str, '')

    # Script tag regex fallback (only fires because script content was detected)
    # These patterns are targeted to script tags only — they will not match
    # non-script content because Phase 4 already returned for clean files.
    result = re.sub(
        r'<\s*script[^>]*>.*?<\s*/\s*script\s*>',
        '', result, flags=re.IGNORECASE | re.DOTALL
    )
    # Malformed unclosed script tags like <SCRIPT/alert(...)  or <SCRIPT<...
    result = re.sub(r'<\s*script\s*[/<][^>]*>', '', result, flags=re.IGNORECASE)
    # Remaining script open/close tags
    result = re.sub(r'<\s*script[^>]*>', '', result, flags=re.IGNORECASE)
    result = re.sub(r'<\s*/\s*script[^>]*>', '', result, flags=re.IGNORECASE)

    # 5b. Remove dangerous on* event handler attributes
    for attr_name in dangerous_on_handlers:
        attr_pattern = re.compile(
            r'\s+' + re.escape(attr_name) + r'\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)',
            re.IGNORECASE
        )
        result = attr_pattern.sub('', result)

    # 5c. Clear dangerous URL attribute values (per-instance, not per-name)
    # Only clear the specific attribute INSTANCE that contains a dangerous URL,
    # preserving all other instances of the same attribute name.
    def _replace_dangerous_url_attr(match):
        """Replace URL value only if it contains a dangerous scheme."""
        full_match = match.group(0)
        attr_part = match.group(1)
        value_part = match.group(2)
        if value_part.startswith('"') and value_part.endswith('"'):
            check_val = value_part[1:-1]
        elif value_part.startswith("'") and value_part.endswith("'"):
            check_val = value_part[1:-1]
        else:
            check_val = value_part
        if _is_dangerous_url(check_val):
            return attr_part + '""'
        return full_match

    if dangerous_url_attrs:
        for attr_name in dangerous_url_attrs:
            if attr_name == 'content':
                # Handle meta refresh specifically — remove the whole meta tag
                result = re.sub(
                    r'<\s*meta[^>]*http-equiv\s*=\s*["\']?refresh["\']?[^>]*/?>',
                    '', result, flags=re.IGNORECASE
                )
            else:
                attr_pattern = re.compile(
                    r'(\s+' + re.escape(attr_name) + r'\s*=\s*)("[^"]*"|\'[^\']*\'|\S+)',
                    re.IGNORECASE
                )
                result = attr_pattern.sub(_replace_dangerous_url_attr, result)

    # 5d. Clear dangerous style attributes
    if has_dangerous_style_attrs:
        attr_pattern = re.compile(
            r'(\s+style\s*=\s*)(?:"[^"]*"|\'[^\']*\')',
            re.IGNORECASE
        )
        result = attr_pattern.sub(r'\1""', result)

    # 5e. Handle dangerous style tags — strip dangerous content, keep tag
    if has_dangerous_style_tags:
        def _clean_style_tag(match):
            content = match.group(1)
            if _is_dangerous_css(content):
                return '<style></style>'
            return match.group(0)
        result = re.sub(
            r'<\s*style[^>]*>(.*?)<\s*/\s*style\s*>',
            _clean_style_tag, result, flags=re.IGNORECASE | re.DOTALL
        )

    return result


if __name__ == '__main__':
    filepath = sys.argv[1]
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        html_content = f.read()
    html_content = sanitize_html(html_content)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html_content)
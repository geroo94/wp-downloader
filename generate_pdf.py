#!/usr/bin/env python3
"""Konwertuje Markdown do HTML a potem do PDF przez weasyprint - bez zewnętrznych bibliotek."""

import subprocess
import sys
import re

md_file = 'DOKUMENTACJA.md'
html_file = 'DOKUMENTACJA.html'
pdf_file = 'DOKUMENTACJA.pdf'

def simple_markdown_to_html(md_content):
    """Prosty konwerter Markdown → HTML."""
    html_lines = []
    
    in_code_block = False
    in_table = False
    
    for line in md_content.split('\n'):
        line = line.rstrip()
        
        # Code blocks
        if line.startswith('```'):
            if in_code_block:
                html_lines.append('</code></pre>')
                in_code_block = False
            else:
                html_lines.append('<pre><code>')
                in_code_block = True
            continue
        
        if in_code_block:
            html_lines.append(line)
            continue
        
        # Nagłówki
        if line.startswith('######'):
            html_lines.append(f'<h6>{line[7:]}</h6>')
            continue
        elif line.startswith('#####'):
            html_lines.append(f'<h5>{line[6:]}</h5>')
            continue
        elif line.startswith('####'):
            html_lines.append(f'<h4>{line[5:]}</h4>')
            continue
        elif line.startswith('###'):
            html_lines.append(f'<h3>{line[4:]}</h3>')
            continue
        elif line.startswith('##'):
            html_lines.append(f'<h2>{line[3:]}</h2>')
            continue
        elif line.startswith('# '):
            html_lines.append(f'<h1>{line[2:]}</h1>')
            continue
        
        # Poziome linie
        if line.startswith('---'):
            html_lines.append('<hr>')
            continue
        
        # Listy
        if line.strip().startswith('- ') or line.strip().startswith('* '):
            html_lines.append(f'<li>{line[2:]}</li>')
            continue
        
        # Listy numerowane
        match = re.match(r'^(\d+)\.\s+(.+)$', line.strip())
        if match:
            html_lines.append(f'<li>{match.group(2)}</li>')
            continue
        
        # Blockquote
        if line.startswith('> '):
            html_lines.append(f'<blockquote>{line[2:]}</blockquote>')
            continue
        
        # Puste linie
        if not line.strip():
            html_lines.append('<br>')
            continue
        
        # Zwykły tekst - proste formatowanie
        text = line
        text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)
        text = re.sub(r'\*([^*]+)\*', r'<em>\1</em>', text)
        text = re.sub(r'`([^`]+)`', r'<code>\1</code>', text)
        text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', text)
        
        html_lines.append(f'<p>{text}</p>')
    
    return '\n'.join(html_lines)

print("1. Czytam Markdown...")

with open(md_file, 'r', encoding='utf-8') as f:
    md = f.read()

print("2. Konwertuję do HTML...")

html_content = simple_markdown_to_html(md)

full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>WP Downloader 1.0 — Dokumentacja Techniczna</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; max-width: 800px; }}
h1 {{ color: #212121; border-bottom: 2px solid #E3000F; padding-bottom: 10px; }}
h2 {{ color: #333; margin-top: 30px; }}
h3 {{ color: #555; }}
code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-family: monospace; }}
pre {{ background: #f4f4f4; padding: 15px; border-radius: 5px; overflow-x: auto; }}
blockquote {{ border-left: 4px solid #E3000F; padding-left: 15px; color: #666; }}
ul {{ margin: 10px 0; }}
li {{ margin: 5px 0; }}
</style>
</head>
<body>
{html_content}
</body>
</html>"""

with open(html_file, 'w', encoding='utf-8') as f:
    f.write(full_html)

print(f"   ✓ HTML zapisany: {html_file}")

print("3. Konwertuję HTML → PDF...")

try:
    result = subprocess.run(
        ['weasyprint', html_file, pdf_file],
        capture_output=True,
        text=True,
        timeout=60
    )
    
    if result.returncode == 0:
        print(f"   ✓ PDF zapisany: {pdf_file}")
        print(f"\n✅ Gotowe! Dokumentacja: {pdf_file}")
    else:
        print(f"   ✗ Błąd: {result.stderr}")
        sys.exit(1)
        
except FileNotFoundError:
    print("   ✗ Błąd: weasyprint nie jest zainstalowany")
    print("   Zainstaluj: brew install weasyprint")
    sys.exit(1)
except Exception as e:
    print(f"   ✗ Błąd: {e}")
    sys.exit(1)
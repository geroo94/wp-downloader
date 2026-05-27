#!/usr/bin/env python3
"""Konwertuje Markdown do HTML a potem do PDF przez weasyprint."""

import markdown
import subprocess
import sys
import os

md_file = 'DOKUMENTACJA.md'
html_file = 'DOKUMENTACJA.html'
pdf_file = 'DOKUMENTACJA.pdf'

print("1. Konwertuję Markdown → HTML...")

with open(md_file, 'r', encoding='utf-8') as f:
    md = f.read()

html_content = markdown.markdown(md, extensions=['extra', 'codehilite'])

full_html = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>WP Downloader 1.0 Dokumentacja</title>
<style>
body { font-family: "DejaVu Sans", Arial, sans-serif; margin: 40px; line-height: 1.6; color: #333; }
h1 { color: #212121; border-bottom: 3px solid #E3000F; padding-bottom: 10px; margin-top: 40px; }
h2 { color: #212121; border-bottom: 1px solid #ccc; margin-top: 30px; padding-bottom: 5px; }
h3 { color: #555; }
code { background: #f4f4f4; padding: 2px 6px; border-radius: 3px; font-family: monospace; }
pre { background: #f4f4f4; padding: 15px; border-radius: 5px; overflow-x: auto; }
table { border-collapse: collapse; width: 100%; margin: 20px 0; }
th, td { border: 1px solid #ddd; padding: 12px; text-align: left; }
th { background: #f4f4f4; }
ul { margin: 10px 0; }
li { margin: 5px 0; }
hr { border: 0; border-top: 1px solid #eee; margin: 20px 0; }
@page { margin: 2cm; }
</style>
</head>
<body>
""" + html_content + """
</body>
</html>"""

with open(html_file, 'w', encoding='utf-8') as f:
    f.write(full_html)

print(f"   ✓ HTML zapisany: {html_file}")

print("2. Konwertuję HTML → PDF...")

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
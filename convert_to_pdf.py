#!/usr/bin/env python3
"""
Konwerter Markdown → PDF dla WP Downloader 1.0
"""

import markdown
import sys
from fpdf import FPDF
import re


class PDF(FPDF):
    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=15)
        
    def header(self):
        self.set_font('Helvetica', 'B', 15)
        self.set_text_color(33, 33, 33)
        self.cell(0, 10, 'WP Downloader 1.0 — Dokumentacja Techniczna', ln=True, align='C')
        self.ln(5)
        
    def footer(self):
        self.set_y(-15)
        self.set_font('Helvetica', 'I', 8)
        self.set_text_color(128, 128, 128)
        self.cell(0, 10, f'Strona {self.page_no()}', align='C')


def parse_heading_level(line):
    """Określa poziom nagłówka."""
    if line.startswith('######'):
        return 6
    elif line.startswith('#####'):
        return 5
    elif line.startswith('####'):
        return 4
    elif line.startswith('###'):
        return 3
    elif line.startswith('##'):
        return 2
    elif line.startswith('#'):
        return 1
    return 0


def clean_text(text):
    """Czyści tekst z tagów Markdown."""
    text = re.sub(r'\*\*([^*]+)\*\*', r'\1', text)
    text = re.sub(r'\*([^*]+)\*', r'\1', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    return text.strip()


def convert_md_to_pdf(md_file, pdf_file):
    """Konwertuje plik Markdown do PDF."""
    
    # Czytaj plik Markdown
    with open(md_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    pdf = PDF()
    pdf.add_page()
    
    # Ustawienia czcionki
    pdf.set_text_color(33, 33, 33)
    
    in_code_block = False
    code_content = []
    
    for line in content.split('\n'):
        # Pomijaj linie z obrazkami Mermaid
        if line.strip().startswith('```mermaid'):
            in_code_block = True
            code_content = []
            continue
        elif in_code_block and line.strip() == '```':
            in_code_block = False
            code_content = []
            continue
        elif in_code_block:
            continue
            
        # Nagłówki
        heading_level = parse_heading_level(line)
        if heading_level > 0:
            pdf.ln(3)
            if heading_level == 1:
                pdf.set_font('Helvetica', 'B', 16)
                pdf.set_text_color(33, 33, 33)
            elif heading_level == 2:
                pdf.set_font('Helvetica', 'B', 14)
                pdf.set_text_color(33, 33, 33)
            elif heading_level == 3:
                pdf.set_font('Helvetica', 'B', 12)
                pdf.set_text_color(33, 33, 33)
            else:
                pdf.set_font('Helvetica', 'B', 11)
                pdf.set_text_color(33, 33, 33)
            
            heading_text = clean_text(line.lstrip('#').strip())
            pdf.cell(0, 8, heading_text, ln=True)
            pdf.ln(2)
            continue
        
        # Tabele (uproszczona obsługa)
        if line.strip().startswith('|') and '---' not in line:
            # Pomijamy tabele w prostym renderingu
            continue
        elif line.strip().startswith('|') and '---' in line:
            continue
            
        # Listy
        if line.strip().startswith('- ') or line.strip().startswith('* '):
            pdf.set_font('Helvetica', '', 10)
            pdf.set_text_color(33, 33, 33)
            bullet = clean_text(line.strip()[:2])
            text = clean_text(line.strip()[2:])
            pdf.cell(5)
            pdf.cell(3, 6, '•', ln=False)
            pdf.cell(2)
            pdf.multi_cell(0, 6, text)
            continue
            
        # Listy numerowane
        match = re.match(r'^\d+\.\s+(.+)$', line.strip())
        if match:
            pdf.set_font('Helvetica', '', 10)
            pdf.set_text_color(33, 33, 33)
            text = clean_text(match.group(1))
            pdf.cell(5)
            pdf.multi_cell(0, 6, text)
            continue
        
        # Puste linie
        if not line.strip():
            pdf.ln(2)
            continue
        
        # Kod w linii
        if '`' in line:
            pdf.set_font('Courier', '', 10)
        else:
            pdf.set_font('Helvetica', '', 10)
        
        pdf.set_text_color(33, 33, 33)
        
        # Specjalne elementy
        if line.startswith('> '):
            # Blockquote
            pdf.set_font('Helvetica', 'I', 10)
            pdf.set_text_color(100, 100, 100)
            text = clean_text(line[2:])
            pdf.multi_cell(0, 6, text)
            continue
            
        # Zwykły tekst
        text = clean_text(line)
        if len(text) > 100:
            pdf.multi_cell(0, 6, text)
        else:
            pdf.cell(0, 6, text, ln=True)
    
    pdf.output(pdf_file)
    print(f"✓ PDF zapisany: {pdf_file}")


if __name__ == '__main__':
    md_file = 'DOKUMENTACJA.md'
    pdf_file = 'DOKUMENTACJA.pdf'
    
    if len(sys.argv) > 1:
        md_file = sys.argv[1]
    if len(sys.argv) > 2:
        pdf_file = sys.argv[2]
    
    convert_md_to_pdf(md_file, pdf_file)
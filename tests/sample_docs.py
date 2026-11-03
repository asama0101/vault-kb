"""変換テスト用に、各形式の最小の実ファイルを作る。"""

from pathlib import Path

import openpyxl
from docx import Document
from pptx import Presentation


def make_txt(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def make_docx(path: Path, text: str) -> None:
    document = Document()
    document.add_heading("見積について", level=1)
    document.add_paragraph(text)
    document.save(path)


def make_pptx(path: Path, text: str) -> None:
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[1])
    slide.shapes.title.text = "提案の概要"
    slide.placeholders[1].text = text
    deck.save(path)


def make_xlsx(path: Path, text: str) -> None:
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "売上"
    sheet.append(["項目", "金額"])
    sheet.append([text, 1200])
    book.save(path)


def make_pdf(path: Path, text: str) -> None:
    """text が空なら、文字のない（スキャン相当の）PDF になる。"""
    stream = f"BT /F1 18 Tf 72 700 Td ({text}) Tj ET" if text else ""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R"
        " /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin-1")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("latin-1")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode("latin-1")
    path.write_bytes(out)

"""DANFE fictícia (a versão impressa da NF-e) para testar a leitura por imagem.

Layout simplificado, mas com os blocos que importam para a conferência: emitente, chave de
acesso, destinatário, itens (com pedido/item do cliente) e totais. `foto=True` imita uma foto
de celular: inclina, desfoca e comprime em JPEG.
"""

from __future__ import annotations

import io
from decimal import Decimal

import pypdfium2
from fpdf import FPDF
from PIL import Image, ImageEnhance, ImageFilter

from recebimento.dominio import NotaFiscal


def _cnpj(c: str) -> str:
    return f"{c[:2]}.{c[2:5]}.{c[5:8]}/{c[8:12]}-{c[12:]}"


def _br(valor: Decimal, casas: int = 2) -> str:
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "X").replace(".", ",").replace("X", ".")


def gerar_pdf(nota: NotaFiscal) -> bytes:
    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 13)
    pdf.cell(0, 8, "DANFE - Documento Auxiliar da Nota Fiscal Eletrônica", border=1, align="C")
    pdf.ln(10)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(170, 7, nota.emitente_nome, border="LTR")
    pdf.cell(0, 7, f"NF-e Nº {nota.numero:09d}  Série {nota.serie:03d}", border="LTR")
    pdf.ln()
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(170, 7, f"CNPJ {_cnpj(nota.emitente_cnpj)}   UF {nota.emitente_uf}", border="LBR")
    pdf.cell(0, 7, f"Emissão {nota.data_emissao.strftime('%d/%m/%Y')}", border="LBR")
    pdf.ln(9)

    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(0, 5, "CHAVE DE ACESSO", border="LTR")
    pdf.ln()
    pdf.set_font("Courier", "B", 13)
    grupos = " ".join(nota.chave[i : i + 4] for i in range(0, 44, 4))
    pdf.cell(0, 8, grupos, border="LBR", align="C")
    pdf.ln(10)

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, f"DESTINATÁRIO  CNPJ {_cnpj(nota.destinatario_cnpj)}", border=1)
    pdf.ln(9)

    # A4 paisagem: 277 mm úteis. Soma das larguras = 277 (nada passa da margem).
    colunas = [
        ("Código", 20), ("Descrição", 56), ("NCM", 16), ("CFOP", 12), ("Qtd", 16),
        ("V. Unit", 22), ("V. Total", 24), ("BC ICMS", 22), ("% ICMS", 14),
        ("V. ICMS", 20), ("% IPI", 12), ("V. IPI", 16), ("Pedido/Item", 27),
    ]  # fmt: skip
    pdf.set_font("Helvetica", "B", 8)
    for titulo, largura in colunas:
        pdf.cell(largura, 6, titulo, border=1, align="C")
    pdf.ln()
    pdf.set_font("Helvetica", "", 8)
    for item in nota.itens:
        valores = [
            item.codigo,
            item.descricao[:38],
            item.ncm,
            item.cfop,
            _br(item.quantidade, 0)
            if item.quantidade == item.quantidade.to_integral()
            else _br(item.quantidade, 4),
            _br(item.valor_unitario),
            _br(item.valor_total),
            _br(item.base_icms),
            _br(item.aliquota_icms * 100),
            _br(item.valor_icms),
            _br(item.aliquota_ipi * 100),
            _br(item.valor_ipi),
            f"{item.pedido or '-'}/{item.item_pedido or '-'}",
        ]
        for (titulo, largura), texto in zip(colunas, valores, strict=True):
            pdf.cell(largura, 6, texto, border=1, align="L" if titulo == "Descrição" else "R")
        pdf.ln()

    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 9)
    totais = [
        ("V. TOTAL PRODUTOS", nota.totais.valor_produtos),
        ("V. ICMS", nota.totais.valor_icms),
        ("V. IPI", nota.totais.valor_ipi),
        ("V. TOTAL DA NOTA", nota.totais.valor_nota),
    ]
    for rotulo, _ in totais:
        pdf.cell(66, 6, rotulo, border="LTR")
    pdf.ln()
    pdf.set_font("Helvetica", "", 11)
    for _, total in totais:
        pdf.cell(66, 7, _br(total), border="LBR", align="R")
    return bytes(pdf.output())


def pdf_para_png(pdf: bytes, escala: float = 2.0) -> bytes:
    documento = pypdfium2.PdfDocument(pdf)
    imagem = documento[0].render(scale=escala).to_pil()
    saida = io.BytesIO()
    imagem.save(saida, format="PNG")
    return saida.getvalue()


def efeito_foto(png: bytes, angulo: float = 1.2) -> bytes:
    """Imita uma foto de celular: inclinação, desfoque leve, menos contraste, JPEG."""
    imagem = Image.open(io.BytesIO(png)).convert("RGB")
    imagem = imagem.rotate(angulo, expand=True, fillcolor=(235, 232, 225))
    imagem = imagem.filter(ImageFilter.GaussianBlur(0.7))
    imagem = ImageEnhance.Contrast(imagem).enhance(0.85)
    saida = io.BytesIO()
    imagem.save(saida, format="JPEG", quality=70)
    return saida.getvalue()


def gerar_imagem(nota: NotaFiscal, foto: bool = False) -> tuple[bytes, str]:
    """Imagem da DANFE e o tipo MIME (`image/png` ou `image/jpeg`)."""
    png = pdf_para_png(gerar_pdf(nota))
    return (efeito_foto(png), "image/jpeg") if foto else (png, "image/png")

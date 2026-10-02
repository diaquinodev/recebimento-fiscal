"""Serviço de recebimento: XML → leitura → contexto do ERP → motor → registro.

É a única porta de entrada de notas (arquivo, SOAP ou OCR), para que toda nota passe pelas
mesmas regras e fique registrada com a decisão.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from recebimento.chave import chave_valida
from recebimento.dominio import NotaFiscal, PedidoCompra, SituacaoSefaz
from recebimento.erp import Erp, NotaEntradaRow
from recebimento.ia import ProvedorIA
from recebimento.match import ContextoErp, Divergencia, Resultado, Status, avaliar_nota
from recebimento.nfe_xml import ErroLeituraNFe, gerar_xml, ler_xml
from recebimento.ocr import ler_danfe


class ConsultaSefaz(Protocol):
    def situacao(self, chave: str) -> SituacaoSefaz: ...


class SefazEmMemoria:
    """SEFAZ simulado a partir de um dicionário chave → situação (ex.: `sefaz.json`)."""

    def __init__(self, situacoes: dict[str, str]) -> None:
        self._situacoes = situacoes

    @classmethod
    def de_arquivo(cls, caminho: Path) -> SefazEmMemoria:
        return cls(json.loads(caminho.read_text(encoding="utf-8")))

    def situacao(self, chave: str) -> SituacaoSefaz:
        return SituacaoSefaz(self._situacoes.get(chave, SituacaoSefaz.NAO_ENCONTRADA.value))


def montar_contexto(erp: Erp, sefaz: ConsultaSefaz, nota: NotaFiscal) -> ContextoErp:
    pedidos: dict[str, PedidoCompra] = {}
    for numero in {i.pedido for i in nota.itens if i.pedido}:
        pedido = erp.pedido(numero)
        if pedido is not None:
            pedidos[numero] = pedido
    return ContextoErp(
        compradora_cnpj=erp.compradora_cnpj(),
        pedidos=pedidos,
        recebimentos={numero: erp.recebimentos(numero) for numero in pedidos},
        tolerancias=erp.tolerancias(),
        chave_ja_lancada=erp.chave_aceita(nota.chave),
        situacao_sefaz=sefaz.situacao(nota.chave) if chave_valida(nota.chave) else None,
    )


def receber_xml(
    erp: Erp, sefaz: ConsultaSefaz, conteudo: bytes, arquivo: str, origem: str = "arquivo"
) -> NotaEntradaRow:
    try:
        nota = ler_xml(conteudo)
    except ErroLeituraNFe as erro:
        resultado = Resultado(Status.REJEITADA, (Divergencia("xml_invalido", str(erro)),))
        return erp.registrar(
            chave="",
            arquivo=arquivo,
            origem=origem,
            emitente_cnpj="",
            numero=0,
            valor_nota=Decimal(0),
            resultado=resultado,
            xml=conteudo,
        )
    resultado = avaliar_nota(nota, montar_contexto(erp, sefaz, nota))
    return erp.registrar(
        chave=nota.chave,
        arquivo=arquivo,
        origem=origem,
        emitente_cnpj=nota.emitente_cnpj,
        numero=nota.numero,
        valor_nota=nota.totais.valor_nota,
        resultado=resultado,
        xml=conteudo,
    )


def receber_imagem(
    erp: Erp,
    sefaz: ConsultaSefaz,
    ia: ProvedorIA,
    imagem: bytes,
    mime: str,
    arquivo: str,
    id_chamada: str | None = None,
) -> NotaEntradaRow:
    """DANFE (foto/PDF renderizado) → leitura por IA → conferência → mesmo fluxo do XML.

    Só leitura conferida segue para o 3-way match. A que falha é registrada como rejeitada
    (`leitura_ocr`) com os problemas, para revisão humana ou até o XML chegar.
    """
    leitura = ler_danfe(imagem, mime, ia, id_chamada=id_chamada)
    nota = leitura.nota
    if nota is None or not leitura.conferida:
        mensagem = "; ".join(p.mensagem for p in leitura.problemas) or "leitura incompleta"
        return erp.registrar(
            chave=nota.chave if nota else "",
            arquivo=arquivo,
            origem="ocr",
            emitente_cnpj=nota.emitente_cnpj if nota else "",
            numero=nota.numero if nota else 0,
            valor_nota=nota.totais.valor_nota if nota else Decimal(0),
            resultado=Resultado(
                Status.REJEITADA, (Divergencia("leitura_ocr", f"revisar leitura: {mensagem}"),)
            ),
            xml=leitura.bruto.encode(),
        )
    return receber_xml(erp, sefaz, gerar_xml(nota, protocolo=False), arquivo, origem="ocr")


def codigos(row: NotaEntradaRow) -> list[str]:
    return sorted({d["codigo"] for d in json.loads(row.divergencias_json)})

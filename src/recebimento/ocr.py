"""OCR/IDP de DANFE: imagem → campos estruturados → conferência em código.

O modelo de visão só transcreve. Quem decide se a leitura é confiável é o código:
dígito verificador da chave, chave × CNPJ/número, quantidade × unitário = total e somas.
Uma leitura com problema não entra sozinha: vai para revisão humana.

Importante: a DANFE é só a representação impressa. O documento fiscal é o XML. A leitura
por imagem serve para triagem e pré-lançamento enquanto o XML não chega do fornecedor.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ValidationError

from recebimento.chave import CODIGO_UF, chave_valida, montar_chave
from recebimento.dominio import ItemNota, NotaFiscal, Totais
from recebimento.ia import ErroIA, ProvedorIA
from recebimento.nfe_xml import Problema, conferir_nota

PROMPT_VERSAO = "2"
PROMPT = """Você transcreve a imagem de uma DANFE (nota fiscal brasileira) para JSON.

Regras:
- Transcreva SOMENTE o que está visível. Campo ilegível ou ausente = null. Nunca invente.
- "chave_grupos": a chave de acesso como está impressa: 11 grupos de 4 dígitos, na ordem.
  Copie grupo por grupo; não junte, não pule e não repita grupos.
- CNPJ só com dígitos (14). Datas no formato AAAA-MM-DD.
- Números como texto com ponto decimal e sem separador de milhar ("14450.00").
- Percentuais como aparecem (ICMS 12,00 → "12.00").
- "pedido" e "item_pedido" vêm da coluna Pedido/Item ("4500000021/10" → "4500000021" e 10).

Responda apenas com este JSON:
{"chave_grupos": [str, ...11 grupos], "numero": int, "serie": int, "data_emissao": str,
 "emitente_cnpj": str, "emitente_nome": str, "emitente_uf": str, "destinatario_cnpj": str,
 "itens": [{"codigo": str, "descricao": str, "ncm": str, "cfop": str, "quantidade": str,
            "valor_unitario": str, "valor_total": str, "base_icms": str,
            "aliquota_icms_pct": str, "valor_icms": str, "aliquota_ipi_pct": str,
            "valor_ipi": str, "pedido": str|null, "item_pedido": int|null}],
 "valor_produtos": str, "valor_icms": str, "valor_ipi": str, "valor_nota": str}"""


class ItemLido(BaseModel):
    codigo: str
    descricao: str
    ncm: str
    cfop: str
    quantidade: Decimal
    valor_unitario: Decimal
    valor_total: Decimal
    base_icms: Decimal
    aliquota_icms_pct: Decimal
    valor_icms: Decimal
    aliquota_ipi_pct: Decimal
    valor_ipi: Decimal
    pedido: str | None = None
    item_pedido: int | None = None


class DanfeLida(BaseModel):
    chave_grupos: list[str]
    numero: int
    serie: int
    data_emissao: date
    emitente_cnpj: str
    emitente_nome: str
    emitente_uf: str
    destinatario_cnpj: str
    itens: list[ItemLido]
    valor_produtos: Decimal
    valor_icms: Decimal
    valor_ipi: Decimal
    valor_nota: Decimal

    def para_nota(self) -> NotaFiscal:
        return NotaFiscal(
            chave=re.sub(r"\D", "", "".join(self.chave_grupos)),
            numero=self.numero,
            serie=self.serie,
            data_emissao=self.data_emissao,
            emitente_cnpj=re.sub(r"\D", "", self.emitente_cnpj),
            emitente_nome=self.emitente_nome,
            emitente_uf=self.emitente_uf,
            destinatario_cnpj=re.sub(r"\D", "", self.destinatario_cnpj),
            itens=tuple(
                ItemNota(
                    numero=n,
                    codigo=i.codigo,
                    descricao=i.descricao,
                    ncm=i.ncm,
                    cfop=i.cfop,
                    quantidade=i.quantidade,
                    valor_unitario=i.valor_unitario,
                    valor_total=i.valor_total,
                    base_icms=i.base_icms,
                    aliquota_icms=i.aliquota_icms_pct / 100,
                    valor_icms=i.valor_icms,
                    aliquota_ipi=i.aliquota_ipi_pct / 100,
                    valor_ipi=i.valor_ipi,
                    pedido=i.pedido,
                    item_pedido=i.item_pedido,
                )
                for n, i in enumerate(self.itens, start=1)
            ),
            totais=Totais(
                valor_produtos=self.valor_produtos,
                valor_icms=self.valor_icms,
                valor_ipi=self.valor_ipi,
                valor_nota=self.valor_nota,
            ),
        )


@dataclass(frozen=True)
class LeituraDanfe:
    nota: NotaFiscal | None
    problemas: list[Problema]
    bruto: str
    #: correções feitas pelo código (ex.: chave reconstruída), sempre registradas
    ajustes: tuple[str, ...] = ()

    @property
    def conferida(self) -> bool:
        """Leitura passou em todas as conferências e pode seguir para o 3-way match."""
        return self.nota is not None and not self.problemas


def reconstruir_chave(nota: NotaFiscal, chave_lida: str) -> str | None:
    """Monta a chave com campos redundantes lidos na DANFE e o final da chave lida.

    A chave repete UF, ano/mês, CNPJ, série e número, que aparecem em outros pontos da DANFE.
    Só o código numérico (cNF) e o DV são exclusivos dela: vêm dos 9 últimos dígitos lidos.
    A candidata só vale se o dígito verificador fechar.
    """
    if len(chave_lida) < 9 or nota.emitente_uf not in CODIGO_UF:
        return None
    cnf, dv = chave_lida[-9:-1], chave_lida[-1]
    try:
        candidata = montar_chave(
            nota.emitente_uf,
            nota.data_emissao.strftime("%y%m"),
            nota.emitente_cnpj,
            nota.serie,
            nota.numero,
            cnf,
        )
    except (KeyError, ValueError):
        return None
    return candidata if candidata[-1] == dv and chave_valida(candidata) else None


def _extrair_json(texto: str) -> str:
    texto = texto.strip()
    cerca = re.search(r"```(?:json)?\s*(.*?)```", texto, re.DOTALL)
    return cerca.group(1).strip() if cerca else texto


def ler_danfe(
    imagem: bytes, mime: str, ia: ProvedorIA, id_chamada: str | None = None
) -> LeituraDanfe:
    url = f"data:{mime};base64,{base64.b64encode(imagem).decode()}"
    mensagens: list[dict[str, object]] = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]
    bruto = ""
    for tentativa in range(2):
        sufixo = "" if tentativa == 0 else "-correcao"
        resposta = ia.completar(
            mensagens,
            modo_json=True,
            id_chamada=f"{id_chamada}{sufixo}" if id_chamada else None,
        )
        bruto = str(resposta.get("content") or "")
        try:
            lida = DanfeLida.model_validate(json.loads(_extrair_json(bruto)))
            nota = lida.para_nota()
        except (json.JSONDecodeError, ValidationError, ValueError) as erro:
            if tentativa == 1:
                return LeituraDanfe(
                    None, [Problema("formato", f"resposta inválida: {erro}")], bruto
                )
            mensagens += [
                {"role": "assistant", "content": bruto},
                {
                    "role": "user",
                    "content": f"A resposta não seguiu o formato ({erro}). "
                    "Responda de novo só com o JSON pedido.",
                },
            ]
            continue
        ajustes: tuple[str, ...] = ()
        if not chave_valida(nota.chave):
            candidata = reconstruir_chave(nota, nota.chave)
            if candidata is not None:
                ajustes = (f"chave reconstruída por campos redundantes (lida: {nota.chave})",)
                nota = nota.model_copy(update={"chave": candidata})
        return LeituraDanfe(nota, conferir_nota(nota), bruto, ajustes)
    raise ErroIA("inalcançável")  # pragma: no cover

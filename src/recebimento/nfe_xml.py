"""NF-e em XML (layout 4.00, subconjunto): gerar, ler e conferir consistência.

Campos cobertos: ide, emit, dest, det/prod (com xPed/nItemPed, que ligam o item da nota ao
item do pedido de compra), ICMS00, IPITrib e totais. Assinatura digital e validação por XSD
oficial ficam fora do escopo (ver README).

A leitura usa um parser sem resolução de entidades nem acesso à rede: XML de terceiros não
pode ler arquivos do servidor (ataque XXE).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from lxml import etree

from recebimento.chave import CODIGO_UF, chave_valida, decompor_chave
from recebimento.dominio import ItemNota, NotaFiscal, Totais

NS = "http://www.portalfiscal.inf.br/nfe"
_N = f"{{{NS}}}"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)


class ErroLeituraNFe(ValueError):
    """XML ilegível ou sem campo obrigatório."""


@dataclass(frozen=True)
class Problema:
    codigo: str
    mensagem: str


# ============================== escrita ==============================


def _sub(pai: etree._Element, tag: str, texto: object | None = None) -> etree._Element:
    el = etree.SubElement(pai, _N + tag)
    if texto is not None:
        el.text = str(texto)
    return el


def _dec(valor: Decimal, casas: int = 2) -> str:
    return f"{valor:.{casas}f}"


def gerar_xml(nota: NotaFiscal, protocolo: bool = True) -> bytes:
    """Gera o XML da NF-e (envolto em `nfeProc` quando `protocolo=True`)."""
    # namespace padrão (sem prefixo): o lxml usa a chave None, que os stubs não aceitam
    raiz = etree.Element(_N + "nfeProc", nsmap={None: NS}, versao="4.00")  # type: ignore[dict-item]
    nfe = _sub(raiz, "NFe")
    inf = _sub(nfe, "infNFe")
    inf.set("Id", f"NFe{nota.chave}")
    inf.set("versao", "4.00")

    ide = _sub(inf, "ide")
    _sub(ide, "cUF", CODIGO_UF[nota.emitente_uf])
    _sub(ide, "cNF", nota.chave[35:43])
    _sub(ide, "natOp", "VENDA DE MERCADORIA")
    _sub(ide, "mod", "55")
    _sub(ide, "serie", nota.serie)
    _sub(ide, "nNF", nota.numero)
    _sub(ide, "dhEmi", f"{nota.data_emissao.isoformat()}T10:00:00-03:00")
    _sub(ide, "tpNF", "1")
    _sub(ide, "cDV", nota.chave[43])

    emit = _sub(inf, "emit")
    _sub(emit, "CNPJ", nota.emitente_cnpj)
    _sub(emit, "xNome", nota.emitente_nome)
    _sub(_sub(emit, "enderEmit"), "UF", nota.emitente_uf)

    dest = _sub(inf, "dest")
    _sub(dest, "CNPJ", nota.destinatario_cnpj)

    for item in nota.itens:
        det = _sub(inf, "det")
        det.set("nItem", str(item.numero))
        prod = _sub(det, "prod")
        _sub(prod, "cProd", item.codigo)
        _sub(prod, "xProd", item.descricao)
        _sub(prod, "NCM", item.ncm)
        _sub(prod, "CFOP", item.cfop)
        _sub(prod, "uCom", "UN")
        _sub(prod, "qCom", _dec(item.quantidade, 4))
        _sub(prod, "vUnCom", _dec(item.valor_unitario, 4))
        _sub(prod, "vProd", _dec(item.valor_total))
        if item.pedido is not None:
            _sub(prod, "xPed", item.pedido)
        if item.item_pedido is not None:
            _sub(prod, "nItemPed", item.item_pedido)
        imposto = _sub(det, "imposto")
        icms = _sub(_sub(imposto, "ICMS"), "ICMS00")
        _sub(icms, "orig", "0")
        _sub(icms, "CST", "00")
        _sub(icms, "modBC", "3")
        _sub(icms, "vBC", _dec(item.base_icms))
        _sub(icms, "pICMS", _dec(item.aliquota_icms * 100, 4))
        _sub(icms, "vICMS", _dec(item.valor_icms))
        ipi = _sub(imposto, "IPI")
        _sub(ipi, "cEnq", "999")
        trib = _sub(ipi, "IPITrib")
        _sub(trib, "CST", "50")
        _sub(trib, "vBC", _dec(item.valor_total))
        _sub(trib, "pIPI", _dec(item.aliquota_ipi * 100, 4))
        _sub(trib, "vIPI", _dec(item.valor_ipi))

    tot = _sub(_sub(inf, "total"), "ICMSTot")
    _sub(tot, "vBC", _dec(sum((i.base_icms for i in nota.itens), Decimal(0))))
    _sub(tot, "vICMS", _dec(nota.totais.valor_icms))
    _sub(tot, "vProd", _dec(nota.totais.valor_produtos))
    _sub(tot, "vIPI", _dec(nota.totais.valor_ipi))
    _sub(tot, "vNF", _dec(nota.totais.valor_nota))

    if protocolo:
        prot = _sub(_sub(raiz, "protNFe"), "infProt")
        _sub(prot, "chNFe", nota.chave)
        _sub(prot, "cStat", "100")
        _sub(prot, "xMotivo", "Autorizado o uso da NF-e")
    return bytes(etree.tostring(raiz, xml_declaration=True, encoding="UTF-8"))


# ============================== leitura ==============================


def _texto(pai: etree._Element, caminho: str, obrigatorio: bool = True) -> str | None:
    el = pai.find("/".join(_N + parte for parte in caminho.split("/")))
    if el is None or el.text is None or el.text.strip() == "":
        if obrigatorio:
            raise ErroLeituraNFe(f"campo obrigatório ausente: {caminho}")
        return None
    return el.text.strip()


def _decimal(pai: etree._Element, caminho: str) -> Decimal:
    texto = _texto(pai, caminho)
    try:
        return Decimal(str(texto))
    except InvalidOperation as erro:
        raise ErroLeituraNFe(f"valor numérico inválido em {caminho}: {texto!r}") from erro


def ler_xml(conteudo: bytes) -> NotaFiscal:
    """Lê a NF-e (com ou sem `nfeProc`). Erros de estrutura viram `ErroLeituraNFe`."""
    try:
        raiz = etree.fromstring(conteudo, parser=_PARSER)
    except etree.XMLSyntaxError as erro:
        raise ErroLeituraNFe(f"XML malformado: {erro}") from erro
    inf = raiz.find(f".//{_N}infNFe")
    if inf is None:
        raise ErroLeituraNFe("não é uma NF-e: elemento infNFe não encontrado")
    ident = inf.get("Id", "")
    if not ident.startswith("NFe") or len(ident) != 47:
        raise ErroLeituraNFe(f"atributo Id inválido: {ident!r}")

    itens = []
    for det in inf.findall(f"{_N}det"):
        item_pedido = _texto(det, "prod/nItemPed", obrigatorio=False)
        itens.append(
            ItemNota(
                numero=int(det.get("nItem", "0")),
                codigo=str(_texto(det, "prod/cProd")),
                descricao=str(_texto(det, "prod/xProd")),
                ncm=str(_texto(det, "prod/NCM")),
                cfop=str(_texto(det, "prod/CFOP")),
                quantidade=_decimal(det, "prod/qCom"),
                valor_unitario=_decimal(det, "prod/vUnCom"),
                valor_total=_decimal(det, "prod/vProd"),
                base_icms=_decimal(det, "imposto/ICMS/ICMS00/vBC"),
                aliquota_icms=_decimal(det, "imposto/ICMS/ICMS00/pICMS") / 100,
                valor_icms=_decimal(det, "imposto/ICMS/ICMS00/vICMS"),
                aliquota_ipi=_decimal(det, "imposto/IPI/IPITrib/pIPI") / 100,
                valor_ipi=_decimal(det, "imposto/IPI/IPITrib/vIPI"),
                pedido=_texto(det, "prod/xPed", obrigatorio=False),
                item_pedido=int(item_pedido) if item_pedido else None,
            )
        )
    if not itens:
        raise ErroLeituraNFe("NF-e sem itens (det)")

    data_emissao = str(_texto(inf, "ide/dhEmi"))[:10]
    try:
        emissao = date.fromisoformat(data_emissao)
    except ValueError as erro:
        raise ErroLeituraNFe(f"data de emissão inválida: {data_emissao!r}") from erro

    return NotaFiscal(
        chave=ident[3:],
        numero=int(str(_texto(inf, "ide/nNF"))),
        serie=int(str(_texto(inf, "ide/serie"))),
        data_emissao=emissao,
        emitente_cnpj=str(_texto(inf, "emit/CNPJ")),
        emitente_nome=str(_texto(inf, "emit/xNome")),
        emitente_uf=str(_texto(inf, "emit/enderEmit/UF")),
        destinatario_cnpj=str(_texto(inf, "dest/CNPJ")),
        itens=tuple(itens),
        totais=Totais(
            valor_produtos=_decimal(inf, "total/ICMSTot/vProd"),
            valor_icms=_decimal(inf, "total/ICMSTot/vICMS"),
            valor_ipi=_decimal(inf, "total/ICMSTot/vIPI"),
            valor_nota=_decimal(inf, "total/ICMSTot/vNF"),
        ),
    )


# ============================== consistência ==============================

TOLERANCIA_ARREDONDAMENTO = Decimal("0.02")


def conferir_nota(nota: NotaFiscal) -> list[Problema]:
    """Confere a nota sozinha (sem pedido): chave, emitente e somas. Lista vazia = ok."""
    problemas: list[Problema] = []
    if not chave_valida(nota.chave):
        problemas.append(Problema("chave_invalida", "dígito verificador da chave não confere"))
    else:
        partes = decompor_chave(nota.chave)
        if partes.cnpj != nota.emitente_cnpj:
            problemas.append(
                Problema("chave_emitente", "CNPJ da chave diferente do CNPJ do emitente")
            )
        if partes.numero != nota.numero or partes.serie != nota.serie:
            problemas.append(Problema("chave_numero", "número/série da chave diferem da nota"))
        if partes.modelo != "55":
            problemas.append(Problema("chave_modelo", f"modelo {partes.modelo} não é NF-e (55)"))

    def diferente(a: Decimal, b: Decimal) -> bool:
        return abs(a - b) > TOLERANCIA_ARREDONDAMENTO

    for item in nota.itens:
        if diferente(item.quantidade * item.valor_unitario, item.valor_total):
            problemas.append(
                Problema("item_total", f"item {item.numero}: quantidade × unitário ≠ total")
            )
        if diferente(item.base_icms * item.aliquota_icms, item.valor_icms):
            problemas.append(
                Problema("item_icms", f"item {item.numero}: base × alíquota de ICMS ≠ valor")
            )
    soma_produtos = sum((i.valor_total for i in nota.itens), Decimal(0))
    if diferente(soma_produtos, nota.totais.valor_produtos):
        problemas.append(Problema("total_produtos", "soma dos itens ≠ total de produtos"))
    if diferente(nota.totais.valor_produtos + nota.totais.valor_ipi, nota.totais.valor_nota):
        problemas.append(Problema("total_nota", "produtos + IPI ≠ valor da nota"))
    return problemas

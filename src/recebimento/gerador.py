"""Gera um "ERP" fictício (fornecedores, pedidos, recebimentos) e NF-e com divergências
plantadas, mais o gabarito do que o motor de 3-way match deve concluir.

Tudo é determinístico pela semente: o mesmo `seed` gera exatamente os mesmos arquivos.
Empresas, CNPJs, produtos e valores são fictícios. ICMS simplificado: 18% dentro de SP e
12% entre SP e Sul/Sudeste.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from recebimento.chave import completar_cnpj, montar_chave
from recebimento.dominio import (
    Fornecedor,
    ItemNota,
    ItemPedido,
    ItemRecebido,
    NotaFiscal,
    PedidoCompra,
    Recebimento,
    SituacaoSefaz,
    Totais,
)
from recebimento.nfe_xml import gerar_xml

CENT = Decimal("0.01")

COMPRADORA_CNPJ = completar_cnpj("424242420001")
COMPRADORA_NOME = "Indústria Exemplo S.A."
COMPRADORA_UF = "SP"

FORNECEDORES_BASE = [
    ("101010100001", "Rolamentos Alfa Ltda.", "SP"),
    ("202020200001", "Ferragens Beta Comercial Ltda.", "MG"),
    ("303030300001", "Lubrificantes Gama S.A.", "PR"),
    ("404040400001", "EPI Delta Distribuidora Ltda.", "RJ"),
    ("505050500001", "Elétrica Ômega Ltda.", "SP"),
    ("606060600001", "Embalagens Sigma Ltda.", "SC"),
]

# código, descrição, NCM (formato válido; dados de exemplo), preço base, IPI
CATALOGO = [
    ("ROL-6204", "Rolamento rígido de esferas 6204", "84821010", "38.90", "0.05"),
    ("PAR-M8", "Parafuso sextavado M8 x 40 (cento)", "73181500", "54.00", "0.05"),
    ("OLE-68", "Óleo hidráulico ISO 68 (balde 20 L)", "27101932", "412.50", "0.00"),
    ("LUV-NIT", "Luva nitrílica (caixa com 100)", "40151900", "64.90", "0.00"),
    ("CAB-2.5", "Cabo flexível 2,5 mm² (rolo 100 m)", "85444900", "289.00", "0.05"),
    ("CX-PAP", "Caixa de papelão ondulado 40x30x30", "48191000", "4.75", "0.00"),
    ("FIT-ADE", "Fita adesiva industrial 45 mm", "39191010", "8.40", "0.10"),
    ("COR-B", "Correia em V perfil B-50", "40103100", "71.20", "0.05"),
]

TIPOS = [
    "ok",
    "dentro_tolerancia",
    "preco_acima",
    "quantidade_maior",
    "icms_divergente",
    "item_sem_pedido",
    "fornecedor_divergente",
    "sem_recebimento",
    "chave_invalida",
    "duplicada",
    "cancelada_sefaz",
]

#: O que o motor deve concluir para cada tipo plantado.
ESPERADO: dict[str, tuple[str, list[str]]] = {
    "ok": ("liberada", []),
    "dentro_tolerancia": ("liberada", []),
    "preco_acima": ("bloqueada", ["preco"]),
    "quantidade_maior": ("bloqueada", ["quantidade"]),
    "icms_divergente": ("bloqueada", ["icms"]),
    "item_sem_pedido": ("bloqueada", ["sem_pedido"]),
    "fornecedor_divergente": ("bloqueada", ["fornecedor"]),
    "sem_recebimento": ("bloqueada", ["sem_recebimento"]),
    "chave_invalida": ("rejeitada", ["chave_invalida"]),
    "duplicada": ("rejeitada", ["duplicada"]),
    "cancelada_sefaz": ("rejeitada", ["cancelada_sefaz"]),
}

TOLERANCIAS = {
    "preco_percentual": "0.02",
    "quantidade_unidades": "0",
    "imposto_reais": "0.05",
}


def _q(valor: Decimal) -> Decimal:
    return valor.quantize(CENT, rounding=ROUND_HALF_UP)


def _aliquota_icms(uf_origem: str) -> Decimal:
    return Decimal("0.18") if uf_origem == COMPRADORA_UF else Decimal("0.12")


@dataclass
class Cenario:
    fornecedores: list[Fornecedor] = field(default_factory=list)
    pedidos: list[PedidoCompra] = field(default_factory=list)
    recebimentos: list[Recebimento] = field(default_factory=list)
    #: (nome do arquivo, nota, chave a gravar no XML) — a chave pode ser adulterada
    notas: list[tuple[str, NotaFiscal]] = field(default_factory=list)
    sefaz: dict[str, str] = field(default_factory=dict)
    gabarito: dict[str, dict[str, Any]] = field(default_factory=dict)


def _fornecedores() -> list[Fornecedor]:
    return [
        Fornecedor(
            cnpj=completar_cnpj(base),
            razao_social=nome,
            uf=uf,
            email=f"faturamento@{nome.split()[1].lower()}.exemplo",
        )
        for base, nome, uf in FORNECEDORES_BASE
    ]


def _montar_nota(
    pedido: PedidoCompra,
    fornecedor: Fornecedor,
    numero: int,
    emissao: date,
    linhas: list[tuple[ItemPedido, Decimal, Decimal, Decimal, int | None]],
) -> NotaFiscal:
    """`linhas`: (item do pedido, quantidade, preço unitário, alíquota ICMS, item referenciado)."""
    cfop = "5102" if fornecedor.uf == COMPRADORA_UF else "6102"
    itens = []
    for n, (ip, qtd, preco, aliq_icms, ref) in enumerate(linhas, start=1):
        total = _q(qtd * preco)
        itens.append(
            ItemNota(
                numero=n,
                codigo=ip.codigo,
                descricao=ip.descricao,
                ncm=ip.ncm,
                cfop=cfop,
                quantidade=qtd,
                valor_unitario=preco,
                valor_total=total,
                base_icms=total,
                aliquota_icms=aliq_icms,
                valor_icms=_q(total * aliq_icms),
                aliquota_ipi=ip.aliquota_ipi,
                valor_ipi=_q(total * ip.aliquota_ipi),
                pedido=pedido.numero,
                item_pedido=ref,
            )
        )
    produtos = sum((i.valor_total for i in itens), Decimal(0))
    ipi = sum((i.valor_ipi for i in itens), Decimal(0))
    chave = montar_chave(
        fornecedor.uf,
        emissao.strftime("%y%m"),
        fornecedor.cnpj,
        serie=1,
        numero=numero,
        codigo_numerico=f"{numero * 7919 % 10**8:08d}",
    )
    return NotaFiscal(
        chave=chave,
        numero=numero,
        serie=1,
        data_emissao=emissao,
        emitente_cnpj=fornecedor.cnpj,
        emitente_nome=fornecedor.razao_social,
        emitente_uf=fornecedor.uf,
        destinatario_cnpj=COMPRADORA_CNPJ,
        itens=tuple(itens),
        totais=Totais(
            valor_produtos=produtos,
            valor_icms=sum((i.valor_icms for i in itens), Decimal(0)),
            valor_ipi=ipi,
            valor_nota=produtos + ipi,
        ),
    )


def gerar_cenario(seed: int = 42, repeticoes: int = 2) -> Cenario:
    """Gera `repeticoes` notas de cada tipo em `TIPOS` (ordem embaralhada pela semente)."""
    rnd = random.Random(seed)
    cen = Cenario(fornecedores=_fornecedores())
    tipos = [t for t in TIPOS for _ in range(repeticoes)]
    rnd.shuffle(tipos)
    inicio = date(2026, 9, 1)

    for i, tipo in enumerate(tipos, start=1):
        fornecedor = cen.fornecedores[i % len(cen.fornecedores)]
        produtos = rnd.sample(CATALOGO, rnd.randint(1, 3))
        aliq = _aliquota_icms(fornecedor.uf)
        itens_pedido = tuple(
            ItemPedido(
                item=n * 10,
                codigo=cod,
                descricao=desc,
                ncm=ncm,
                quantidade=Decimal(rnd.choice([5, 10, 20, 50, 100])),
                preco_unitario=Decimal(preco),
                aliquota_icms=aliq,
                aliquota_ipi=Decimal(ipi),
            )
            for n, (cod, desc, ncm, preco, ipi) in enumerate(produtos, start=1)
        )
        data_pedido = inicio + timedelta(days=i)
        pedido = PedidoCompra(
            numero=f"45000{i:05d}",
            fornecedor_cnpj=fornecedor.cnpj,
            data=data_pedido,
            itens=itens_pedido,
        )
        cen.pedidos.append(pedido)
        if tipo != "sem_recebimento":
            cen.recebimentos.append(
                Recebimento(
                    numero=f"50000{i:05d}",
                    pedido=pedido.numero,
                    data=data_pedido + timedelta(days=3),
                    itens=tuple(
                        ItemRecebido(item_pedido=ip.item, quantidade=ip.quantidade)
                        for ip in itens_pedido
                    ),
                )
            )

        linhas: list[tuple[ItemPedido, Decimal, Decimal, Decimal, int | None]] = [
            (ip, ip.quantidade, ip.preco_unitario, ip.aliquota_icms, ip.item) for ip in itens_pedido
        ]
        alvo = 0
        ip0, qtd0, preco0, aliq0, ref0 = linhas[alvo]
        if tipo == "dentro_tolerancia":
            linhas[alvo] = (ip0, qtd0, _q(preco0 * Decimal("1.01")), aliq0, ref0)
        elif tipo == "preco_acima":
            linhas[alvo] = (ip0, qtd0, _q(preco0 * Decimal("1.06")), aliq0, ref0)
        elif tipo == "quantidade_maior":
            linhas[alvo] = (ip0, qtd0 + 2, preco0, aliq0, ref0)
        elif tipo == "icms_divergente":
            linhas[alvo] = (ip0, qtd0, preco0, aliq0 + Decimal("0.06"), ref0)
        elif tipo == "item_sem_pedido":
            linhas[alvo] = (ip0, qtd0, preco0, aliq0, 990)

        emitente = fornecedor
        if tipo == "fornecedor_divergente":
            # outro fornecedor fatura o pedido; alíquotas seguem as do pedido (só o CNPJ diverge)
            emitente = cen.fornecedores[(i + 1) % len(cen.fornecedores)]
        nota = _montar_nota(pedido, emitente, 1000 + i, data_pedido + timedelta(days=2), linhas)

        if tipo == "chave_invalida":
            dv_errado = (int(nota.chave[43]) + 1) % 10
            nota = nota.model_copy(update={"chave": nota.chave[:43] + str(dv_errado)})

        arquivo = f"nfe-{i:03d}.xml"
        cen.notas.append((arquivo, nota))
        situacao = (
            SituacaoSefaz.CANCELADA if tipo == "cancelada_sefaz" else SituacaoSefaz.AUTORIZADA
        )
        cen.sefaz[nota.chave] = situacao.value
        status, divergencias = ESPERADO[tipo]
        if tipo == "duplicada":
            # a primeira via é válida; a segunda (mesma chave) deve ser rejeitada
            cen.gabarito[arquivo] = {"tipo": "ok", "status": "liberada", "divergencias": []}
            copia = f"nfe-{i:03d}-reenvio.xml"
            cen.notas.append((copia, nota))
            cen.gabarito[copia] = {"tipo": tipo, "status": status, "divergencias": divergencias}
        else:
            cen.gabarito[arquivo] = {"tipo": tipo, "status": status, "divergencias": divergencias}
    return cen


def _json(valor: Any) -> Any:
    if isinstance(valor, Decimal):
        return str(valor)
    if isinstance(valor, date):
        return valor.isoformat()
    raise TypeError(type(valor))


def salvar_cenario(cen: Cenario, pasta: Path) -> None:
    """Grava `erp.json`, `sefaz.json`, `gabarito.json` e os XML em `notas/`."""
    (pasta / "notas").mkdir(parents=True, exist_ok=True)
    erp = {
        "compradora": {
            "cnpj": COMPRADORA_CNPJ,
            "razao_social": COMPRADORA_NOME,
            "uf": COMPRADORA_UF,
        },
        "tolerancias": TOLERANCIAS,
        "fornecedores": [f.model_dump() for f in cen.fornecedores],
        "pedidos": [p.model_dump() for p in cen.pedidos],
        "recebimentos": [r.model_dump() for r in cen.recebimentos],
    }
    for nome, conteudo in [
        ("erp.json", erp),
        ("sefaz.json", cen.sefaz),
        ("gabarito.json", cen.gabarito),
    ]:
        (pasta / nome).write_text(
            json.dumps(conteudo, default=_json, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    for arquivo, nota in cen.notas:
        (pasta / "notas" / arquivo).write_bytes(gerar_xml(nota))

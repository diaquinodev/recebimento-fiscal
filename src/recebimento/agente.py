"""Agente de exceções: investiga uma nota bloqueada com ferramentas e propõe a tratativa.

A IA investiga e redige. O código decide o que é aceitável:
- cada divergência precisa de uma ação da lista permitida para ela (`PERMITIDAS`);
- todo valor em R$ citado no e-mail precisa ter vindo das ferramentas (nada inventado);
- o destinatário do e-mail vem do cadastro do fornecedor, nunca do texto da IA.
Proposta inválida recebe o motivo e uma nova chance; se falhar de novo, vai para um analista.
Nada é executado sozinho: o parecer espera aprovação humana.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from recebimento.erp import Erp, NotaEntradaRow
from recebimento.ia import ProvedorIA
from recebimento.nfe_xml import ErroLeituraNFe, ler_xml

#: Ações que o agente pode propor.
ACOES = {
    "liberar_com_aprovacao": "liberar com aprovação formal (ex.: compras aceita o preço e "
    "atualiza o pedido)",
    "solicitar_cancelamento_reemissao": "fornecedor cancela a NF-e e emite outra correta",
    "recusar_mercadoria": "recusar/devolver a mercadoria ou o excedente",
    "aguardar_recebimento": "aguardar a entrada física da mercadoria antes de liberar",
    "encaminhar_compras": "compras regulariza o pedido de compra",
    "carta_correcao": "carta de correção eletrônica (CC-e), só para dados que não mudam valores",
}

#: Ações aceitas por tipo de divergência. A CC-e não aparece em nenhuma: ela não pode corrigir
#: base de cálculo, alíquota, preço, quantidade, valor nem trocar emitente/destinatário
#: (Convênio SINIEF s/nº de 1970, art. 7º). Nesses casos o caminho é cancelar e reemitir.
PERMITIDAS: dict[str, set[str]] = {
    "preco": {"liberar_com_aprovacao", "solicitar_cancelamento_reemissao", "recusar_mercadoria"},
    "quantidade": {"recusar_mercadoria", "solicitar_cancelamento_reemissao",
                   "aguardar_recebimento"},
    "icms": {"solicitar_cancelamento_reemissao"},
    "ipi": {"solicitar_cancelamento_reemissao"},
    "fornecedor": {"solicitar_cancelamento_reemissao", "recusar_mercadoria", "encaminhar_compras"},
    "sem_pedido": {"encaminhar_compras", "recusar_mercadoria"},
    "sem_recebimento": {"aguardar_recebimento", "recusar_mercadoria"},
}  # fmt: skip

MAX_RODADAS = 8

SISTEMA = f"""Você é analista sênior de recebimento fiscal (contas a pagar) de uma indústria.
Uma NF-e de fornecedor foi BLOQUEADA no 3-way match (pedido × recebimento × nota).

Investigue usando as ferramentas ANTES de concluir: veja as divergências, o pedido, os
recebimentos e o histórico do fornecedor. Não invente números: cite só valores que vieram
das ferramentas. Não decida pagamento: você propõe, um humano aprova.

Ações possíveis (use o código exato): {json.dumps(ACOES, ensure_ascii=False)}
A carta de correção (CC-e) não corrige preço, quantidade, alíquota, base de cálculo nem
emitente: nesses casos peça cancelamento e reemissão.

Ao terminar, responda SÓ com este JSON:
{{"resumo": "2 a 4 frases para o analista",
  "tratativas": [{{"divergencia": "<código>", "acao": "<ação>", "justificativa": "..."}}],
  "email": {{"assunto": "...", "corpo": "texto ao fornecedor, cordial e objetivo"}}}}
Uma tratativa para cada código de divergência da nota."""

FERRAMENTAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "listar_divergencias",
            "description": "Divergências do 3-way match nesta nota, com impacto em R$.",
            "parameters": {
                "type": "object",
                "properties": {"nota_id": {"type": "integer"}},
                "required": ["nota_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "itens_da_nota",
            "description": "Itens da NF-e: quantidade, preço, impostos e pedido/item referenciado.",
            "parameters": {
                "type": "object",
                "properties": {"nota_id": {"type": "integer"}},
                "required": ["nota_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_pedido",
            "description": "Pedido de compra no ERP: fornecedor e itens com preço e alíquotas.",
            "parameters": {
                "type": "object",
                "properties": {"numero": {"type": "string"}},
                "required": ["numero"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consultar_recebimentos",
            "description": "Entradas físicas de mercadoria registradas para um pedido.",
            "parameters": {
                "type": "object",
                "properties": {"pedido": {"type": "string"}},
                "required": ["pedido"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "historico_fornecedor",
            "description": "Notas anteriores do fornecedor: quantidade por situação e "
            "divergências mais comuns.",
            "parameters": {
                "type": "object",
                "properties": {"cnpj": {"type": "string"}},
                "required": ["cnpj"],
            },
        },
    },
]


# ============================== ferramentas ==============================


class Ferramentas:
    """Executa as ferramentas sobre o ERP e guarda todo valor numérico que mostrou à IA."""

    def __init__(self, erp: Erp) -> None:
        self.erp = erp
        self.valores_vistos: set[Decimal] = set()
        self.usadas: list[str] = []

    def _anotar(self, dados: Any) -> Any:
        if isinstance(dados, dict):
            for v in dados.values():
                self._anotar(v)
        elif isinstance(dados, list):
            for v in dados:
                self._anotar(v)
        elif isinstance(dados, str) and re.fullmatch(r"-?\d+(\.\d+)?", dados):
            self.valores_vistos.add(Decimal(dados))
        return dados

    def executar(self, nome: str, argumentos: dict[str, Any]) -> dict[str, Any]:
        self.usadas.append(nome)
        metodo = getattr(self, f"_{nome}", None)
        if metodo is None:
            return {"erro": f"ferramenta desconhecida: {nome}"}
        try:
            resultado: dict[str, Any] = metodo(**argumentos)
        except TypeError as erro:
            return {"erro": f"argumentos inválidos: {erro}"}
        return dict(self._anotar(resultado))

    def _nota(self, nota_id: int) -> NotaEntradaRow | None:
        return self.erp.s.get(NotaEntradaRow, int(nota_id))

    def _listar_divergencias(self, nota_id: int) -> dict[str, Any]:
        nota = self._nota(nota_id)
        if nota is None:
            return {"erro": "nota não encontrada"}
        return {
            "status": nota.status,
            "emitente_cnpj": nota.emitente_cnpj,
            "valor_nota": str(nota.valor_nota),
            "impacto_total": str(nota.impacto_reais),
            "divergencias": json.loads(nota.divergencias_json),
        }

    def _itens_da_nota(self, nota_id: int) -> dict[str, Any]:
        nota = self._nota(nota_id)
        if nota is None:
            return {"erro": "nota não encontrada"}
        try:
            nfe = ler_xml(nota.xml)
        except ErroLeituraNFe:
            return {"erro": "XML da nota ilegível"}
        return {
            "numero": nfe.numero,
            "emitente": nfe.emitente_nome,
            "itens": [
                {
                    "item": i.numero,
                    "codigo": i.codigo,
                    "descricao": i.descricao,
                    "quantidade": str(i.quantidade),
                    "valor_unitario": str(i.valor_unitario),
                    "valor_total": str(i.valor_total),
                    "aliquota_icms": str(i.aliquota_icms),
                    "valor_icms": str(i.valor_icms),
                    "pedido": i.pedido,
                    "item_pedido": i.item_pedido,
                }
                for i in nfe.itens
            ],
        }

    def _consultar_pedido(self, numero: str) -> dict[str, Any]:
        pedido = self.erp.pedido(str(numero))
        if pedido is None:
            return {"erro": f"pedido {numero} não existe no ERP"}
        fornecedor = self.erp.fornecedor(pedido.fornecedor_cnpj)
        return {
            "numero": pedido.numero,
            "fornecedor_cnpj": pedido.fornecedor_cnpj,
            "fornecedor_nome": fornecedor.razao_social if fornecedor else None,
            "data": pedido.data.isoformat(),
            "itens": [
                {
                    "item": i.item,
                    "codigo": i.codigo,
                    "quantidade": str(i.quantidade),
                    "preco_unitario": str(i.preco_unitario),
                    "aliquota_icms": str(i.aliquota_icms),
                    "aliquota_ipi": str(i.aliquota_ipi),
                }
                for i in pedido.itens
            ],
        }

    def _consultar_recebimentos(self, pedido: str) -> dict[str, Any]:
        recebimentos = self.erp.recebimentos(str(pedido))
        return {
            "pedido": pedido,
            "recebimentos": [
                {
                    "numero": r.numero,
                    "data": r.data.isoformat(),
                    "itens": [
                        {"item_pedido": i.item_pedido, "quantidade": str(i.quantidade)}
                        for i in r.itens
                    ],
                }
                for r in recebimentos
            ],
        }

    def _historico_fornecedor(self, cnpj: str) -> dict[str, Any]:
        notas = [n for n in self.erp.notas() if n.emitente_cnpj == str(cnpj)]
        por_status = Counter(n.status for n in notas)
        codigos: Counter[str] = Counter()
        for n in notas:
            codigos.update({d["codigo"] for d in json.loads(n.divergencias_json)})
        fornecedor = self.erp.fornecedor(str(cnpj))
        return {
            "cnpj": cnpj,
            "razao_social": fornecedor.razao_social if fornecedor else None,
            "notas_recebidas": len(notas),
            "por_status": dict(por_status),
            "divergencias_mais_comuns": dict(codigos.most_common(5)),
        }


# ============================== parecer ==============================


@dataclass
class Tratativa:
    divergencia: str
    acao: str
    justificativa: str


@dataclass
class Parecer:
    nota_id: int
    valido: bool
    resumo: str = ""
    tratativas: list[Tratativa] = field(default_factory=list)
    email_para: str = ""
    email_assunto: str = ""
    email_corpo: str = ""
    ferramentas_usadas: list[str] = field(default_factory=list)
    correcoes: list[str] = field(default_factory=list)  # travas de negócio acionadas
    formatacoes: int = 0  # conclusões em texto livre convertidas para JSON
    rodadas: int = 0


#: Divergências em que a CC-e é proibida (mudam valor, imposto ou emitente).
SEM_CCE = {"preco", "quantidade", "icms", "ipi", "fornecedor"}
_CCE = re.compile(r"carta\s+de\s+corre[cç][aã]o|\bCC-?e\b", re.IGNORECASE)
_NEGACAO = re.compile(
    r"\bn[aã]o\b|\bnunca\b|\bvedad|\bproibid|\binaplic|\bimpossib|\bimped", re.IGNORECASE
)


def sugere_cce(texto: str) -> bool:
    """Alguma frase recomenda CC-e? Frases que a negam ("não pode ser corrigida por carta de
    correção") não contam: barrar a resposta certa é tão ruim quanto deixar passar a errada."""
    for frase in re.split(r"(?<=[.!?;])\s+|\n+", texto):
        if _CCE.search(frase) and not _NEGACAO.search(frase):
            return True
    return False


_NUMERO_RS = re.compile(r"R\$\s*([\d.]+,\d{2}|\d+(?:\.\d{2})?)")


def _decimal_br(texto: str) -> Decimal:
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    return Decimal(texto)


def validar_proposta(
    proposta: dict[str, Any], codigos: list[str], valores_vistos: set[Decimal]
) -> list[str]:
    """Erros da proposta segundo as regras de negócio. Lista vazia = aceita."""
    erros: list[str] = []
    tratativas = proposta.get("tratativas")
    if not isinstance(tratativas, list) or not tratativas:
        return ["faltou a lista 'tratativas'"]
    cobertas = set()
    for t in tratativas:
        codigo, acao = str(t.get("divergencia", "")), str(t.get("acao", ""))
        cobertas.add(codigo)
        if codigo not in codigos:
            erros.append(f"divergência '{codigo}' não existe nesta nota ({codigos})")
        elif acao not in PERMITIDAS.get(codigo, set()):
            permitidas = sorted(PERMITIDAS.get(codigo, set()))
            erros.append(f"ação '{acao}' não é aceita para '{codigo}'; use uma de {permitidas}")
    faltando = sorted(set(codigos) - cobertas)
    if faltando:
        erros.append(f"faltou tratativa para {faltando}")
    email = proposta.get("email") or {}
    corpo = str(email.get("corpo", ""))
    if not corpo.strip():
        erros.append("faltou o corpo do e-mail")
    texto = " ".join(
        [str(proposta.get("resumo", "")), corpo, str(email.get("assunto", ""))]
        + [str(t.get("justificativa", "")) for t in tratativas]
    )
    proibidas = sorted(SEM_CCE & set(codigos))
    if proibidas and sugere_cce(texto):
        erros.append(
            f"o texto sugere carta de correção, que não pode corrigir {proibidas}: "
            "peça cancelamento e reemissão (ou outra ação permitida)"
        )
    aceitos = {v.quantize(Decimal("0.01")) for v in valores_vistos}
    for bruto in _NUMERO_RS.findall(corpo):
        valor = _decimal_br(bruto).quantize(Decimal("0.01"))
        if valor not in aceitos and -valor not in aceitos:
            erros.append(f"o e-mail cita R$ {bruto}, valor que não veio das ferramentas")
    return erros


def _json_da_resposta(texto: str) -> dict[str, Any]:
    cerca = re.search(r"```(?:json)?\s*(.*?)```", texto, re.DOTALL)
    dados = json.loads(cerca.group(1) if cerca else texto)
    if not isinstance(dados, dict):
        raise ValueError("resposta não é um objeto JSON")
    return dados


def analisar_nota(erp: Erp, ia: ProvedorIA, nota_id: int, id_chamada: str | None = None) -> Parecer:
    nota = erp.s.get(NotaEntradaRow, nota_id)
    if nota is None or nota.status != "bloqueada":
        raise ValueError("o agente só analisa notas bloqueadas")
    codigos = sorted({d["codigo"] for d in json.loads(nota.divergencias_json)})
    fornecedor = erp.fornecedor(nota.emitente_cnpj)
    ferramentas = Ferramentas(erp)
    parecer = Parecer(nota_id=nota_id, valido=False)
    mensagens: list[dict[str, Any]] = [
        {"role": "system", "content": SISTEMA},
        {
            "role": "user",
            "content": f"Analise a nota {nota_id} (NF {nota.numero}, emitente "
            f"{nota.emitente_cnpj}). Códigos de divergência: {codigos}.",
        },
    ]
    tentativas_finais = 0
    for rodada in range(1, MAX_RODADAS + 1):
        parecer.rodadas = rodada
        resposta = ia.completar(
            mensagens,
            ferramentas=FERRAMENTAS,
            id_chamada=f"{id_chamada}-r{rodada}" if id_chamada else None,
        )
        mensagens.append({k: v for k, v in resposta.items() if v is not None})
        chamadas = resposta.get("tool_calls") or []
        if chamadas:
            for chamada in chamadas:
                funcao = chamada["function"]
                try:
                    argumentos = json.loads(funcao.get("arguments") or "{}")
                except json.JSONDecodeError:
                    argumentos = {}
                resultado = ferramentas.executar(funcao["name"], argumentos)
                mensagens.append(
                    {
                        "role": "tool",
                        "tool_call_id": chamada["id"],
                        "content": json.dumps(resultado, ensure_ascii=False),
                    }
                )
            continue

        tentativas_finais += 1
        try:
            proposta = _json_da_resposta(str(resposta.get("content") or ""))
        except (json.JSONDecodeError, ValueError):
            # conclusão em texto livre: uma chamada só de formatação, em modo JSON
            parecer.formatacoes += 1
            mensagens.append(
                {"role": "user", "content": "Converta sua conclusão para o JSON pedido."}
            )
            formatada = ia.completar(
                mensagens,
                modo_json=True,
                id_chamada=f"{id_chamada}-r{rodada}-json" if id_chamada else None,
            )
            mensagens.append({k: v for k, v in formatada.items() if v is not None})
            try:
                proposta = _json_da_resposta(str(formatada.get("content") or ""))
            except (json.JSONDecodeError, ValueError) as erro:
                proposta = {}
                parecer.correcoes.append(f"resposta não é o JSON pedido: {erro}")
        erros = (
            validar_proposta(proposta, codigos, ferramentas.valores_vistos)
            if proposta
            else ["resposta não é o JSON pedido"]
        )
        if len(ferramentas.usadas) < 2:
            erros.append("investigue com as ferramentas antes de concluir (use pelo menos 2)")
        if not erros:
            parecer.valido = True
            parecer.resumo = str(proposta.get("resumo", ""))
            parecer.tratativas = [
                Tratativa(str(t["divergencia"]), str(t["acao"]), str(t.get("justificativa", "")))
                for t in proposta["tratativas"]
            ]
            email = proposta.get("email") or {}
            parecer.email_para = fornecedor.email if fornecedor else ""
            parecer.email_assunto = str(email.get("assunto", ""))
            parecer.email_corpo = str(email.get("corpo", ""))
            break
        parecer.correcoes += erros
        if tentativas_finais >= 2:
            break  # duas propostas inválidas: vai para um analista
        mensagens.append(
            {
                "role": "user",
                "content": "Proposta recusada pelas regras do sistema:\n- "
                + "\n- ".join(erros)
                + "\nCorrija e responda de novo só com o JSON.",
            }
        )
    parecer.ferramentas_usadas = ferramentas.usadas
    return parecer

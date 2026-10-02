"""Tela do analista de recebimento fiscal (Streamlit).

    streamlit run app.py

Modo demonstração (padrão): reproduz respostas reais da IA gravadas para o cenário de exemplo,
sem chave e sem custo. Modo real: chama o OpenRouter (precisa de OPENROUTER_API_KEY).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

import streamlit as st

from recebimento.agente import ACOES, analisar_nota
from recebimento.danfe import gerar_imagem
from recebimento.erp import Erp, NotaEntradaRow, carregar_erp_json, criar_engine, sessao
from recebimento.gerador import gerar_cenario
from recebimento.ia import Gravado, OpenRouter, ProvedorIA
from recebimento.ocr import PROMPT_VERSAO
from recebimento.servico import SefazEmMemoria, codigos, receber_imagem, receber_xml

RAIZ = Path(__file__).parent
DADOS = RAIZ / "data" / "exemplo"
GRAVACOES = RAIZ / "tests" / "gravacoes"
AGENTE_VERSAO = "3"

STATUS_ROTULO = {
    "liberada": "✅ Liberada",
    "bloqueada": "⛔ Bloqueada",
    "rejeitada": "🚫 Rejeitada",
}

st.set_page_config(page_title="Recebimento Fiscal", page_icon="🧾", layout="wide")


def brl(valor: Decimal | str) -> str:
    texto = f"{Decimal(valor):,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def sem_latex(texto: str) -> str:
    """O Markdown do Streamlit lê texto entre dois "$" como fórmula: escapa o cifrão."""
    return texto.replace("$", r"\$")


def brl_curto(valor: Decimal) -> str:
    """Valor curto para indicadores: R$ 117,2 mil (o exato vai na dica)."""
    if abs(valor) >= 1000:
        return (
            "R$ "
            + f"{valor / 1000:,.1f}".replace(",", "X").replace(".", ",").replace("X", ".")
            + " mil"
        )
    return brl(valor)


def ambiente() -> dict[str, Any]:
    """Banco em memória com o ERP de exemplo, um por visitante: no link público, as notas e
    decisões de um visitante não podem aparecer para outro."""
    if "ambiente" not in st.session_state:
        engine = criar_engine()
        carregar_erp_json(engine, DADOS / "erp.json")
        sefaz = SefazEmMemoria.de_arquivo(DADOS / "sefaz.json")
        st.session_state["ambiente"] = {"engine": engine, "sefaz": sefaz}
    amb: dict[str, Any] = st.session_state["ambiente"]
    return amb


def provedor(modo: str, pasta: str) -> ProvedorIA:
    if modo == "real":
        return Gravado(GRAVACOES / pasta, real=OpenRouter(), modo="gravar")
    return Gravado(GRAVACOES / pasta)


def processar_exemplo() -> None:
    amb = ambiente()
    with sessao(amb["engine"]) as s:
        erp = Erp(s)
        if erp.notas():
            return
        for xml in sorted((DADOS / "notas").glob("*.xml"), key=lambda p: p.stem):
            receber_xml(erp, amb["sefaz"], xml.read_bytes(), xml.name)


# ============================== barra lateral ==============================

with st.sidebar:
    st.header("Entrada de notas")
    modo = st.radio(
        "IA",
        ["demonstração", "real"],
        help="Demonstração reproduz respostas reais gravadas (sem custo). Real chama o OpenRouter.",
        horizontal=True,
    )
    if modo == "real" and not os.environ.get("OPENROUTER_API_KEY"):
        st.warning("Defina OPENROUTER_API_KEY para usar o modo real.")
        modo = "demonstração"
    if st.button("Processar as 24 NF-e de exemplo", type="primary", width="stretch"):
        processar_exemplo()
        st.toast("Notas de exemplo processadas")

    arquivos = st.file_uploader("Enviar XML de NF-e", type=["xml"], accept_multiple_files=True)
    if arquivos and st.button("Receber XML enviados", width="stretch"):
        amb = ambiente()
        with sessao(amb["engine"]) as s:
            for arq in arquivos:
                row = receber_xml(Erp(s), amb["sefaz"], arq.getvalue(), arq.name, origem="upload")
                st.write(f"{arq.name}: {STATUS_ROTULO[row.status]}")

    st.divider()
    st.subheader("DANFE por imagem (OCR)")
    cen = gerar_cenario(seed=42)
    exemplos = [a for a, _ in cen.notas if not a.endswith("-reenvio.xml")]
    escolhida = st.selectbox("Foto de exemplo", exemplos, index=exemplos.index("nfe-021.xml"))
    nota_exemplo = dict(cen.notas)[escolhida]
    imagem, mime = gerar_imagem(nota_exemplo, foto=True)
    st.image(imagem, caption="DANFE fotografada (fictícia)")
    if st.button("Ler com IA e receber", width="stretch"):
        amb = ambiente()
        with sessao(amb["engine"]) as s:
            row = receber_imagem(
                Erp(s),
                amb["sefaz"],
                provedor("demonstração" if modo != "real" else "real", "ocr"),
                imagem,
                mime,
                f"foto-{escolhida[:-4]}.jpg",
                id_chamada=f"seed42-{escolhida[:-4]}-foto-p{PROMPT_VERSAO}",
            )
            st.write(f"Lida e recebida: {STATUS_ROTULO[row.status]} {codigos(row)}")


# ============================== painel ==============================

st.title("🧾 Recebimento fiscal — 3-way match")
st.caption(
    "Pedido de compra × recebimento × NF-e. Dados fictícios. "
    "A IA lê DANFE e propõe tratativas; quem decide é o analista."
)

amb = ambiente()
with sessao(amb["engine"]) as s:
    notas = Erp(s).notas()
    linhas = [
        {
            "id": n.id,
            "arquivo": n.arquivo,
            "origem": n.origem,
            "status": n.status,
            "valor": n.valor_nota,
            "impacto": n.impacto_reais,
            "motivos": ", ".join(codigos(n)) or "—",
            "decisão": n.decisao or "",
        }
        for n in notas
    ]

if not linhas:
    st.info("Nenhuma nota ainda. Use **Processar as 24 NF-e de exemplo** na barra lateral.")
    st.stop()

bloqueadas = [r for r in linhas if r["status"] == "bloqueada"]
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Notas recebidas", len(linhas))
c2.metric("Liberadas", sum(r["status"] == "liberada" for r in linhas))
c3.metric("Bloqueadas", len(bloqueadas))
c4.metric("Rejeitadas", sum(r["status"] == "rejeitada" for r in linhas))
retido = sum((r["valor"] for r in bloqueadas), Decimal(0))
c5.metric("Pagamento retido", brl_curto(retido), help=f"Valor exato: {brl(retido)}")

filtro = st.segmented_control(
    "Mostrar", ["bloqueada", "rejeitada", "liberada", "todas"], default="bloqueada"
)
visiveis = [r for r in linhas if filtro in (None, "todas") or r["status"] == filtro]
st.dataframe(
    [
        {
            **r,
            "status": STATUS_ROTULO[r["status"]],
            "valor": brl(r["valor"]),
            "impacto": brl(r["impacto"]),
        }
        for r in visiveis
    ],
    hide_index=True,
    width="stretch",
)

st.divider()
opcoes = {f"#{r['id']} · {r['arquivo']} · {STATUS_ROTULO[r['status']]}": r["id"] for r in visiveis}
if not opcoes:
    st.stop()
rotulo = st.selectbox("Abrir nota", list(opcoes))
nota_id = opcoes[str(rotulo)]

with sessao(amb["engine"]) as s:
    nota = s.get(NotaEntradaRow, nota_id)
    assert nota is not None
    divergencias = json.loads(nota.divergencias_json)
    status, arquivo, decisao = nota.status, nota.arquivo, nota.decisao

esq, dir_ = st.columns([3, 2])
with esq:
    st.subheader(f"{arquivo} — {STATUS_ROTULO[status]}")
    if divergencias:
        st.table(
            [
                {
                    "divergência": d["codigo"],
                    "detalhe": sem_latex(d["mensagem"]),
                    "impacto": brl(d["impacto_reais"]),
                }
                for d in divergencias
            ]
        )
    else:
        st.success("Sem divergências: segue para pagamento.")
    if decisao:
        st.info(f"Decisão registrada: **{decisao}**")

with dir_:
    if status == "bloqueada":
        st.subheader("Parecer do agente")
        chave_parecer = f"parecer-{nota_id}"
        if st.button("Pedir parecer ao agente", type="primary"):
            with st.spinner("Investigando com as ferramentas do ERP…"), sessao(amb["engine"]) as s:
                st.session_state[chave_parecer] = analisar_nota(
                    Erp(s),
                    provedor(modo, "agente"),
                    nota_id,
                    id_chamada=f"seed42-{arquivo[:-4]}-a{AGENTE_VERSAO}",
                )
        parecer = st.session_state.get(chave_parecer)
        if parecer is not None:
            if not parecer.valido:
                st.error("O agente não chegou a uma proposta válida: encaminhar a um analista.")
                for c in parecer.correcoes:
                    st.caption(f"trava: {c}")
            else:
                st.write(sem_latex(parecer.resumo))
                for t in parecer.tratativas:
                    st.markdown(f"- **{t.divergencia}** → `{t.acao}`: {sem_latex(ACOES[t.acao])}")
                st.caption(f"Ferramentas usadas: {', '.join(parecer.ferramentas_usadas)}")
                if parecer.correcoes:
                    st.caption(f"Travas acionadas e corrigidas: {len(parecer.correcoes)}")
                st.text_input("Para", parecer.email_para, disabled=True)
                st.text_input("Assunto", parecer.email_assunto, key=f"assunto-{nota_id}")
                corpo = st.text_area(
                    "E-mail ao fornecedor (editável)",
                    parecer.email_corpo,
                    height=220,
                    key=f"corpo-{nota_id}",
                )
                a, b = st.columns(2)
                if a.button("✔ Aprovar tratativa", width="stretch"):
                    with sessao(amb["engine"]) as s:
                        Erp(s).decidir(nota_id, "aprovada", corpo)
                    st.rerun()
                if b.button("✖ Recusar", width="stretch"):
                    with sessao(amb["engine"]) as s:
                        Erp(s).decidir(nota_id, "recusada", "parecer recusado pelo analista")
                    st.rerun()

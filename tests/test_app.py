"""Tela de ponta a ponta com o AppTest do Streamlit (cliques simulados, modo demonstração)."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).parent.parent / "app.py")


def _botao(at: AppTest, rotulo: str):  # type: ignore[no-untyped-def]
    return next(b for b in at.button if b.label == rotulo)


def test_tela_inicial_sem_notas_orienta_o_usuario() -> None:
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    assert not at.exception
    assert any("Processar as 24 NF-e" in i.value for i in at.info)


def test_cada_visitante_tem_o_proprio_banco() -> None:
    primeiro = AppTest.from_file(APP, default_timeout=120)
    primeiro.run()
    _botao(primeiro, "Processar as 24 NF-e de exemplo").click().run()
    assert {m.label: m.value for m in primeiro.metric}["Notas recebidas"] == "24"

    segundo = AppTest.from_file(APP, default_timeout=120)
    segundo.run()
    assert not segundo.exception
    assert any("Processar as 24 NF-e" in i.value for i in segundo.info)


def test_fluxo_completo_processar_parecer_e_aprovar() -> None:
    at = AppTest.from_file(APP, default_timeout=120)
    at.run()
    _botao(at, "Processar as 24 NF-e de exemplo").click().run()
    assert not at.exception
    metricas = {m.label: m.value for m in at.metric}
    assert metricas["Notas recebidas"] == "24"
    assert metricas["Bloqueadas"] == "12"
    assert metricas["Liberadas"] == "6" and metricas["Rejeitadas"] == "6"

    # a primeira nota bloqueada (filtro padrão) já vem selecionada
    _botao(at, "Pedir parecer ao agente").click().run()
    assert not at.exception
    assert any("Ferramentas usadas" in c.value for c in at.caption)
    corpo = next(t for t in at.text_area if t.label.startswith("E-mail ao fornecedor"))
    assert "Prezad" in corpo.value

    _botao(at, "✔ Aprovar tratativa").click().run()
    assert not at.exception
    assert any("Decisão registrada: **aprovada**" in i.value for i in at.info)

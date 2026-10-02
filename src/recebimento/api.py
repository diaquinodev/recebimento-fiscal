"""API HTTP: SOAP de recebimento, WSDL, SEFAZ simulado e consulta JSON das notas.

    uvicorn "recebimento.api:app_demo" --factory     (ou `python -m recebimento api`)

Rotas:
    GET  /saude
    GET  /soap/recebimento?wsdl          contrato WSDL
    POST /soap/recebimento               operação receberNFe (SOAP 1.1)
    POST /sefaz/NFeConsultaProtocolo4    SEFAZ simulado (consulta de situação)
    GET  /notas[?status=bloqueada]       notas recebidas (JSON)
    GET  /notas/{id}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from sqlalchemy.engine import Engine

from recebimento.erp import Erp, NotaEntradaRow, carregar_erp_json, criar_engine, sessao
from recebimento.servico import ConsultaSefaz, SefazEmMemoria, receber_xml
from recebimento.soap import (
    WSDL_RECEBIMENTO,
    RespostaRecebimento,
    atender_recebimento,
    atender_sefaz,
)

XML = "text/xml; charset=utf-8"


def _nota_json(row: NotaEntradaRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "chave": row.chave,
        "arquivo": row.arquivo,
        "origem": row.origem,
        "emitente_cnpj": row.emitente_cnpj,
        "numero": row.numero,
        "valor_nota": str(row.valor_nota),
        "status": row.status,
        "impacto_reais": str(row.impacto_reais),
        "divergencias": json.loads(row.divergencias_json),
        "decisao": row.decisao,
        "recebida_em": row.recebida_em.isoformat(),
    }


def criar_app(engine: Engine, sefaz: ConsultaSefaz, situacoes_sefaz: dict[str, str]) -> FastAPI:
    """`sefaz` é quem o recebimento consulta; `situacoes_sefaz` alimenta o SEFAZ simulado."""
    app = FastAPI(title="Recebimento Fiscal", version="0.1.0")

    @app.get("/saude")
    def saude() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/soap/recebimento")
    def wsdl(request: Request) -> Response:
        if "wsdl" not in request.query_params:
            raise HTTPException(404, "use ?wsdl para obter o contrato")
        endereco = str(request.url.replace(query=""))
        return Response(WSDL_RECEBIMENTO.replace("{endereco}", endereco), media_type=XML)

    @app.post("/soap/recebimento")
    async def soap_recebimento(request: Request) -> Response:
        def receber(xml_nfe: bytes, arquivo: str) -> RespostaRecebimento:
            with sessao(engine) as s:
                row = receber_xml(Erp(s), sefaz, xml_nfe, arquivo, origem="soap")
                return RespostaRecebimento(
                    status=row.status,
                    chave=row.chave,
                    impacto_reais=f"{row.impacto_reais:.2f}",
                    divergencias=[
                        (d["codigo"], d["mensagem"]) for d in json.loads(row.divergencias_json)
                    ],
                )

        status, corpo = atender_recebimento(await request.body(), receber)
        return Response(corpo, status_code=status, media_type=XML)

    @app.post("/sefaz/NFeConsultaProtocolo4")
    async def sefaz_simulado(request: Request) -> Response:
        status, corpo = atender_sefaz(await request.body(), situacoes_sefaz)
        return Response(corpo, status_code=status, media_type=XML)

    @app.get("/notas")
    def listar(status: str | None = None) -> list[dict[str, Any]]:
        with sessao(engine) as s:
            return [_nota_json(n) for n in Erp(s).notas(status)]

    @app.get("/notas/{nota_id}")
    def detalhe(nota_id: int) -> dict[str, Any]:
        with sessao(engine) as s:
            row = s.get(NotaEntradaRow, nota_id)
            if row is None:
                raise HTTPException(404, "nota não encontrada")
            return _nota_json(row)

    return app


def app_demo(dados: Path = Path("data/exemplo"), banco: str = "sqlite:///saida/demo.db") -> FastAPI:
    """App com o ERP de exemplo; o recebimento consulta o SEFAZ simulado em memória."""
    if banco.startswith("sqlite:///") and not banco.endswith(":memory:"):
        Path(banco.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = criar_engine(banco)
    carregar_erp_json(engine, dados / "erp.json")
    situacoes = json.loads((dados / "sefaz.json").read_text(encoding="utf-8"))
    return criar_app(engine, SefazEmMemoria(situacoes), situacoes)

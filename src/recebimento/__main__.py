"""Linha de comando: `python -m recebimento <comando>`."""

from __future__ import annotations

import argparse
from pathlib import Path

from recebimento.avaliacao import avaliar
from recebimento.erp import Erp, carregar_erp_json, criar_engine, sessao
from recebimento.gerador import gerar_cenario, salvar_cenario
from recebimento.servico import SefazEmMemoria, codigos, receber_xml


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recebimento", description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)

    gerar = sub.add_parser("gerar", help="gera ERP fictício, NF-e com divergências e gabarito")
    gerar.add_argument("--saida", type=Path, default=Path("data/exemplo"))
    gerar.add_argument("--seed", type=int, default=42)
    gerar.add_argument("--repeticoes", type=int, default=2, help="notas por tipo de cenário")

    proc = sub.add_parser("processar", help="processa os XML de uma pasta gerada")
    proc.add_argument("--dados", type=Path, default=Path("data/exemplo"))
    proc.add_argument("--banco", default="sqlite:///:memory:", help="URL SQLAlchemy")

    aval = sub.add_parser("avaliar", help="eval do motor contra o gabarito")
    aval.add_argument("--cenarios", type=int, default=20)

    api = sub.add_parser(
        "api", help="sobe a API (SOAP + SEFAZ simulado + JSON) com o ERP de exemplo"
    )
    api.add_argument("--porta", type=int, default=8000)

    args = parser.parse_args(argv)
    if args.comando == "gerar":
        cenario = gerar_cenario(seed=args.seed, repeticoes=args.repeticoes)
        salvar_cenario(cenario, args.saida)
        print(f"{len(cenario.notas)} NF-e, {len(cenario.pedidos)} pedidos em {args.saida}")
    elif args.comando == "processar":
        engine = criar_engine(args.banco)
        carregar_erp_json(engine, args.dados / "erp.json")
        sefaz = SefazEmMemoria.de_arquivo(args.dados / "sefaz.json")
        with sessao(engine) as s:
            erp = Erp(s)
            for xml in sorted((args.dados / "notas").glob("*.xml"), key=lambda p: p.stem):
                row = receber_xml(erp, sefaz, xml.read_bytes(), xml.name)
                motivos = ", ".join(codigos(row)) or "-"
                print(f"{xml.name:24} {row.status:10} R$ {row.valor_nota:>12,.2f}  {motivos}")
    elif args.comando == "avaliar":
        r = avaliar(args.cenarios)
        print(f"Cenários: {args.cenarios} | notas: {r.notas} | acertos: {r.acertos}")
        print(f"Detecção: {r.deteccao:.1%} ({r.barradas_certo}/{r.esperadas_barradas})")
        print(
            f"Falso bloqueio: {r.taxa_falso_bloqueio:.1%} "
            f"({r.falsos_bloqueios}/{r.esperadas_liberadas})"
        )
        for exemplo in r.exemplos_erro:
            print("  ✕", exemplo)
        return 0 if r.perfeito else 1
    elif args.comando == "api":
        import uvicorn

        uvicorn.run("recebimento.api:app_demo", factory=True, host="127.0.0.1", port=args.porta)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

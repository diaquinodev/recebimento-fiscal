"""Linha de comando: `python -m recebimento <comando>`."""

from __future__ import annotations

import argparse
from pathlib import Path

from recebimento.gerador import gerar_cenario, salvar_cenario


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="recebimento", description=__doc__)
    sub = parser.add_subparsers(dest="comando", required=True)

    gerar = sub.add_parser("gerar", help="gera ERP fictício, NF-e com divergências e gabarito")
    gerar.add_argument("--saida", type=Path, default=Path("data/exemplo"))
    gerar.add_argument("--seed", type=int, default=42)
    gerar.add_argument("--repeticoes", type=int, default=2, help="notas por tipo de cenário")

    args = parser.parse_args(argv)
    if args.comando == "gerar":
        cenario = gerar_cenario(seed=args.seed, repeticoes=args.repeticoes)
        salvar_cenario(cenario, args.saida)
        print(f"{len(cenario.notas)} NF-e, {len(cenario.pedidos)} pedidos em {args.saida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

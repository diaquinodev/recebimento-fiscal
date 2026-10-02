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

    aocr = sub.add_parser("avaliar-ocr", help="eval da leitura de DANFE (respostas gravadas)")
    aocr.add_argument("--gravar", action="store_true", help="chama a IA e grava o que faltar")
    aocr.add_argument("--png", action="store_true", help="imagem limpa em vez de foto")

    ocr = sub.add_parser("ocr", help="lê uma DANFE (imagem) com IA e confere em código")
    ocr.add_argument("imagem", type=Path)

    aag = sub.add_parser("avaliar-agente", help="eval do agente nas notas bloqueadas (gravado)")
    aag.add_argument("--gravar", action="store_true", help="chama a IA e grava o que faltar")
    aag.add_argument("--mostrar", type=int, default=1, help="quantos pareceres imprimir")

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
    elif args.comando == "avaliar-ocr":
        from recebimento.avaliacao import avaliar_ocr
        from recebimento.ia import Gravado, OpenRouter

        pasta = Path("tests/gravacoes/ocr")
        ia = Gravado(pasta, real=OpenRouter(), modo="gravar") if args.gravar else Gravado(pasta)
        o = avaliar_ocr(ia, foto=not args.png)
        print(f"Notas: {o.notas} | campos fiscais corretos: {o.fiscal_correto}")
        print(f"Idênticas (até acento): {o.identicas} | só acento diferente: {o.so_acento}")
        print(f"Conferidas: {o.conferidas} | chaves reconstruídas: {o.chaves_reconstruidas}")
        print(f"Erradas detectadas: {o.erradas_detectadas} | erradas aceitas: {o.erradas_aceitas}")
        for exemplo in o.exemplos:
            print("  ·", exemplo)
        return 0 if o.erradas_aceitas == 0 else 1
    elif args.comando == "ocr":
        from recebimento.ia import OpenRouter
        from recebimento.ocr import ler_danfe

        mime = "image/png" if args.imagem.suffix.lower() == ".png" else "image/jpeg"
        leitura = ler_danfe(args.imagem.read_bytes(), mime, OpenRouter())
        if leitura.nota:
            n = leitura.nota
            print(f"Chave {n.chave} | NF {n.numero} | emitente {n.emitente_cnpj}")
            print(f"{len(n.itens)} itens | total R$ {n.totais.valor_nota:,.2f}")
        for ajuste in leitura.ajustes:
            print("  ajuste:", ajuste)
        print("CONFERIDA" if leitura.conferida else "REVISAR:")
        for p in leitura.problemas:
            print("  ✕", p.mensagem)
        return 0 if leitura.conferida else 1
    elif args.comando == "avaliar-agente":
        from recebimento.avaliacao import avaliar_agente
        from recebimento.ia import Gravado, OpenRouter

        pasta = Path("tests/gravacoes/agente")
        ia = Gravado(pasta, real=OpenRouter(), modo="gravar") if args.gravar else Gravado(pasta)
        a = avaliar_agente(ia)
        print(f"Notas bloqueadas: {a.notas} | pareceres válidos: {a.validos}")
        print(
            f"Sem acionar trava de negócio: {a.validos_de_primeira} | formatações: {a.formatacoes}"
        )
        print(f"Ferramentas por nota: {a.ferramentas_por_nota}")
        for acao, qtd in sorted(a.acoes.items()):
            print(f"  {qtd}x {acao}")
        for correcao in a.correcoes:
            print("  trava:", correcao)
        for parecer in a.pareceres[: args.mostrar]:
            print(f"\n--- nota {parecer.nota_id} ---\n{parecer.resumo}")
            for t in parecer.tratativas:
                print(f"  {t.divergencia} → {t.acao}: {t.justificativa}")
            print(f"Para: {parecer.email_para}\nAssunto: {parecer.email_assunto}")
            print(parecer.email_corpo)
        return 0 if a.validos == a.notas else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""
Premodern Screener - atualiza os precos e gera o painel (painel.html).

O que este programa faz, em ordem:
  1. Baixa o catalogo e os precos do dia do Scryfall e guarda um snapshot no banco (premodern.db).
  2. Na primeira vez (e a cada ~30 dias), importa ~90 dias de historico do TCGplayer via MTGJSON.
  3. Gera candidatas.csv (opcional, para planilha) e painel.html (a pagina com os dados ja dentro).
  4. Abre o painel no navegador (a menos que use --sem-abrir).

Uso (normalmente voce so da dois cliques em iniciar.bat):
    python atualizar.py                 # tudo
    python atualizar.py --sem-download  # so refaz o painel a partir do banco que ja existe
    python atualizar.py --mtgjson       # forca a importacao do historico MTGJSON
    python atualizar.py --sem-abrir     # nao abre o navegador (usado no agendamento)
"""

import argparse
import csv
import gzip
import json
import lzma
import os
import sqlite3
import subprocess
import sys
import time
import traceback
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from urllib.parse import quote_plus

# ----------------------------------------------------------------- configuracao
PISO_USD = 5.0              # so entram no painel impressoes com preco >= este valor (US$)
CAMBIO_MANUAL = 5.50        # usado se a busca automatica do cambio falhar
ERA_INICIO = "1995-04-01"   # 4th Edition
ERA_FIM = "2003-06-30"      # Scourge
MTGJSON_MIN_DATAS = 30      # abaixo disso, importa o historico do MTGJSON
MTGJSON_REFRESCAR_DIAS = 30 # reimporta o MTGJSON se a ultima importacao for mais antiga que isso
IDADE_MAXIMA_PRECO_DIAS = 14  # ignora carta cujo ultimo preco e mais velho que isso

PASTA = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(PASTA, "premodern.db")
CACHE = os.path.join(PASTA, "cache")
LOG_PATH = os.path.join(PASTA, "atualizar.log")
MODELO = os.path.join(PASTA, "painel_modelo.html")
PAINEL = os.path.join(PASTA, "painel.html")
CSV_SAIDA = os.path.join(PASTA, "candidatas.csv")

HEADERS = {"User-Agent": "premodern-screener/1.0", "Accept": "application/json"}
MTGJSON_BASE = "https://mtgjson.com/api/v5/"


# ----------------------------------------------------------------------- util
def log(msg):
    linha = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(linha, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(linha + "\n")
    except OSError:
        pass


def para_float(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def baixar(url, destino, tentativas=3):
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    for n in range(1, tentativas + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=120) as r, open(destino + ".part", "wb") as f:
                feito, marca = 0, 0
                while True:
                    bloco = r.read(1 << 20)
                    if not bloco:
                        break
                    f.write(bloco)
                    feito += len(bloco)
                    if feito - marca >= (25 << 20):
                        log(f"  ... {feito / 1e6:.0f} MB baixados")
                        marca = feito
            os.replace(destino + ".part", destino)
            return destino
        except Exception as erro:
            log(f"Falha no download (tentativa {n}/{tentativas}): {erro}")
            if n == tentativas:
                raise
            time.sleep(5 * n)


def json_get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def baixado_hoje(caminho):
    return os.path.exists(caminho) and date.fromtimestamp(os.path.getmtime(caminho)) == date.today()


def remover(caminho):
    try:
        os.remove(caminho)
    except OSError:
        pass


# ---------------------------------------------------------------------- banco
def criar_tabelas(con):
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS impressoes (
            scryfall_id TEXT PRIMARY KEY, oracle_id TEXT, nome TEXT, set_code TEXT,
            set_nome TEXT, numero TEXT, lancamento TEXT, raridade TEXT,
            reserved_list INTEGER, era_premodern INTEGER, tipo TEXT, cmc REAL
        );
        CREATE TABLE IF NOT EXISTS precos (
            scryfall_id TEXT, data TEXT, usd REAL, usd_foil REAL, eur REAL,
            PRIMARY KEY (scryfall_id, data)
        );
        CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT);
        CREATE INDEX IF NOT EXISTS idx_impressoes_oracle ON impressoes(oracle_id);
        """
    )


def meta_get(con, chave):
    r = con.execute("SELECT valor FROM meta WHERE chave = ?", (chave,)).fetchone()
    return r[0] if r else None


def meta_set(con, chave, valor):
    con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (chave, valor))


def n_datas(con):
    return con.execute("SELECT COUNT(DISTINCT data) FROM precos").fetchone()[0]


# -------------------------------------------------------------------- scryfall
def processar_scryfall(con, caminho, jsonl=True, hoje=None):
    """Le o bulk do Scryfall (gz ou texto) e grava impressoes legais no Premodern + preco do dia."""
    hoje = hoje or date.today().isoformat()
    with open(caminho, "rb") as f:
        gz = f.read(2) == b"\x1f\x8b"
    abrir = (lambda: gzip.open(caminho, "rt", encoding="utf-8")) if gz else (
        lambda: open(caminho, "r", encoding="utf-8"))

    def cartas():
        with abrir() as f:
            if jsonl:
                for linha in f:
                    linha = linha.strip()
                    if linha:
                        yield json.loads(linha)
            else:
                yield from json.load(f)

    imp, pre, total = [], [], 0
    for c in cartas():
        if c.get("legalities", {}).get("premodern") != "legal":
            continue
        if c.get("layout") in ("token", "emblem") or c.get("digital"):
            continue
        lanc = c.get("released_at", "") or ""
        era = int(ERA_INICIO <= lanc <= ERA_FIM)
        imp.append((
            c["id"], c.get("oracle_id"), c["name"], c["set"], c["set_name"],
            c.get("collector_number"), lanc, c.get("rarity"),
            int(c.get("reserved", False)), era, c.get("type_line"), c.get("cmc"),
        ))
        p = c.get("prices") or {}
        pre.append((c["id"], hoje, para_float(p.get("usd")),
                    para_float(p.get("usd_foil")), para_float(p.get("eur"))))
        total += 1
        if len(imp) >= 5000:
            con.executemany("INSERT OR REPLACE INTO impressoes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", imp)
            con.executemany("INSERT OR REPLACE INTO precos VALUES (?,?,?,?,?)", pre)
            imp, pre = [], []
    if imp:
        con.executemany("INSERT OR REPLACE INTO impressoes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", imp)
        con.executemany("INSERT OR REPLACE INTO precos VALUES (?,?,?,?,?)", pre)
    con.commit()
    return total


def atualizar_scryfall(con):
    log("Scryfall: buscando o arquivo de cartas do dia...")
    dados = json_get("https://api.scryfall.com/bulk-data")
    item = next((b for b in dados.get("data", []) if b.get("type") == "default_cards"), None)
    if item is None:
        raise RuntimeError("Scryfall: nao achei 'default_cards' na lista de arquivos.")
    url = item.get("jsonl_download_uri") or item.get("download_uri")
    if not url:
        raise RuntimeError(f"Scryfall: sem link de download. Campos: {list(item.keys())}")
    jsonl = url.split("?")[0].endswith((".jsonl", ".jsonl.gz"))
    arq = os.path.join(CACHE, "scryfall_default_cards" + (".jsonl.gz" if jsonl else ".json"))
    log(f"Scryfall: baixando {url}")
    baixar(url, arq)
    try:
        n = processar_scryfall(con, arq, jsonl=jsonl)
    finally:
        remover(arq)
    log(f"Scryfall: {n} impressoes legais no Premodern, snapshot de hoje salvo.")


# --------------------------------------------------------------------- mtgjson
def garantir_ijson():
    try:
        import ijson
        return ijson
    except ImportError:
        pass
    log("Instalando a biblioteca 'ijson' (leitura de arquivos grandes)...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "ijson"])
        import ijson
        return ijson
    except Exception as erro:
        log(f"Nao consegui instalar o ijson: {erro}")
        return None


def itens_data(caminho, ijson):
    """Gera (chave, valor) do campo 'data' de um arquivo .json.xz, sem carregar tudo na memoria."""
    with lzma.open(caminho, "rb") as f:
        if ijson is not None:
            yield from ijson.kvitems(f, "data")
        elif os.environ.get("PM_PERMITIR_JSON_COMPLETO"):   # so para testes
            yield from json.load(f)["data"].items()
        else:
            raise RuntimeError("ijson indisponivel")


def importar_mtgjson(con):
    ijson = garantir_ijson()
    if ijson is None and not os.environ.get("PM_PERMITIR_JSON_COMPLETO"):
        log("MTGJSON: pulado (precisa da biblioteca ijson). Rode:  pip install ijson")
        return False
    nossos = {r[0] for r in con.execute("SELECT scryfall_id FROM impressoes WHERE era_premodern = 1")}
    if not nossos:
        log("MTGJSON: pulado (banco ainda sem impressoes da era Premodern).")
        return False

    arq_id = os.path.join(CACHE, "AllIdentifiers.json.xz")
    arq_pr = os.path.join(CACHE, "AllPrices.json.xz")
    try:
        if not baixado_hoje(arq_id):
            log("MTGJSON: baixando AllIdentifiers (grande, pode demorar)...")
            baixar(MTGJSON_BASE + "AllIdentifiers.json.xz", arq_id)
        mapa = {}
        for uuid, carta in itens_data(arq_id, ijson):
            sid = (carta.get("identifiers") or {}).get("scryfallId")
            if sid in nossos:
                mapa[uuid] = sid
        log(f"MTGJSON: {len(mapa)} impressoes ligadas ao Scryfall.")

        if not baixado_hoje(arq_pr):
            log("MTGJSON: baixando AllPrices (grande, pode demorar)...")
            baixar(MTGJSON_BASE + "AllPrices.json.xz", arq_pr)
        pontos, cartas, lote = 0, 0, []
        for uuid, precos in itens_data(arq_pr, ijson):
            sid = mapa.get(uuid)
            if not sid:
                continue
            serie = ((((precos.get("paper") or {}).get("tcgplayer") or {})
                      .get("retail") or {}).get("normal") or {})
            if not serie:
                continue
            cartas += 1
            for data, preco in serie.items():
                lote.append((sid, data, float(preco)))
                pontos += 1
            if len(lote) >= 20000:
                con.executemany("INSERT OR IGNORE INTO precos (scryfall_id, data, usd) VALUES (?,?,?)", lote)
                lote = []
        if lote:
            con.executemany("INSERT OR IGNORE INTO precos (scryfall_id, data, usd) VALUES (?,?,?)", lote)
        meta_set(con, "mtgjson_ultimo", date.today().isoformat())
        con.commit()
        log(f"MTGJSON: {pontos} pontos de preco de {cartas} cartas importados.")
        return True
    finally:
        remover(arq_id)
        remover(arq_pr)


def precisa_mtgjson(con):
    ultimo = meta_get(con, "mtgjson_ultimo")
    if n_datas(con) < MTGJSON_MIN_DATAS:
        return True
    if ultimo is None:           # banco ja tem bastante historico (ex.: veio do Colab)
        meta_set(con, "mtgjson_ultimo", date.today().isoformat())
        con.commit()
        return False
    return (date.today() - date.fromisoformat(ultimo)).days >= MTGJSON_REFRESCAR_DIAS


# ---------------------------------------------------------------------- saidas
def obter_cambio():
    try:
        d = json_get("https://economia.awesomeapi.com.br/json/last/USD-BRL")
        cambio = float(d["USDBRL"]["bid"])
        log(f"Cambio do dia (USD-BRL): {cambio:.4f}")
        return cambio
    except Exception as erro:
        log(f"Nao consegui buscar o cambio ({erro}). Usando {CAMBIO_MANUAL}")
        return CAMBIO_MANUAL


def reduzir_serie(pontos):
    """Mantem tudo dos ultimos 120 dias e 1 ponto por semana no que for mais antigo."""
    if len(pontos) <= 150:
        return pontos
    corte = (date.today() - timedelta(days=120)).isoformat()
    saida, ultima_semana = [], None
    for d, p in pontos:
        if d >= corte:
            saida.append([d, p])
        else:
            semana = date.fromisoformat(d).isocalendar()[:2]
            if semana != ultima_semana:
                saida.append([d, p])
                ultima_semana = semana
    return saida


def gerar_saidas(con, cambio, piso):
    ref = con.execute("SELECT MAX(data) FROM precos WHERE usd IS NOT NULL").fetchone()[0]
    if ref is None:
        raise RuntimeError("Banco sem precos. Rode sem --sem-download.")
    limite = (date.fromisoformat(ref) - timedelta(days=IDADE_MAXIMA_PRECO_DIAS)).isoformat()

    base = (
        "WITH ult AS (SELECT scryfall_id, MAX(data) AS d FROM precos "
        "             WHERE usd IS NOT NULL GROUP BY scryfall_id) "
    )
    mais_barata = {
        r[0]: r[1] for r in con.execute(
            base + "SELECT i.oracle_id, MIN(p.usd) FROM impressoes i "
            "JOIN ult u ON u.scryfall_id = i.scryfall_id "
            "JOIN precos p ON p.scryfall_id = u.scryfall_id AND p.data = u.d "
            "WHERE u.d >= ? GROUP BY i.oracle_id", (limite,))
    }
    reprints = {
        r[0]: (r[1], r[2]) for r in con.execute(
            "SELECT oracle_id, COUNT(*), MAX(lancamento) FROM impressoes "
            "WHERE lancamento > ? GROUP BY oracle_id", (ERA_FIM,))
    }
    linhas = con.execute(
        base + "SELECT i.scryfall_id, i.oracle_id, i.nome, i.set_code, i.set_nome, i.raridade, "
        "i.reserved_list, p.usd, p.eur FROM impressoes i "
        "JOIN ult u ON u.scryfall_id = i.scryfall_id "
        "JOIN precos p ON p.scryfall_id = u.scryfall_id AND p.data = u.d "
        "WHERE i.era_premodern = 1 AND p.usd >= ? AND u.d >= ?", (piso, limite)).fetchall()

    escolhidas = {}                       # uma impressao por (set, nome): a mais cara
    for r in linhas:
        k = (r[3], r[2])
        if k not in escolhidas or r[7] > escolhidas[k][7]:
            escolhidas[k] = r

    cartas = []
    for (set_code, nome), r in escolhidas.items():
        n_rep, ultimo = reprints.get(r[1], (0, ""))
        cartas.append({
            "sid": r[0], "nome": nome, "edicao": r[4], "set": set_code, "raridade": r[5],
            "reserved_list": "sim" if r[6] else "nao", "usd": r[7],
            "eur": r[8] if r[8] is not None else "",
            "usd_mais_barata_outra_edicao": mais_barata.get(r[1], ""),
            "reprints_pos_2003": n_rep, "ultimo_reprint": ultimo or "",
            "link_liga": "https://www.ligamagic.com.br/?view=cards/search&card=" + quote_plus(nome),
        })
    cartas.sort(key=lambda x: (x["reserved_list"] != "sim", x["reprints_pos_2003"], -x["usd"]))

    # historico das candidatas
    por_sid = {c["sid"]: c for c in cartas}
    hist_tmp = {}
    ids = list(por_sid)
    for i in range(0, len(ids), 500):
        parte = ids[i:i + 500]
        q = ",".join("?" * len(parte))
        for sid, data, usd in con.execute(
                f"SELECT scryfall_id, data, usd FROM precos WHERE usd IS NOT NULL "
                f"AND scryfall_id IN ({q}) ORDER BY scryfall_id, data", parte):
            hist_tmp.setdefault(sid, []).append([data, usd])
    hist = {}
    for sid, pontos in hist_tmp.items():
        c = por_sid[sid]
        hist[f"{c['set']}|{c['nome']}"] = reduzir_serie(pontos)

    # CSV (opcional, para quem quiser abrir em planilha)
    campos = ["nome", "edicao", "set", "raridade", "reserved_list", "usd", "usd_em_brl", "eur",
              "usd_mais_barata_outra_edicao", "reprints_pos_2003", "ultimo_reprint", "link_liga"]
    with open(CSV_SAIDA, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=campos, extrasaction="ignore")
        w.writeheader()
        for c in cartas:
            w.writerow({**c, "usd_em_brl": round(c["usd"] * cambio, 2)})

    datas = con.execute("SELECT COUNT(DISTINCT data), MIN(data), MAX(data) FROM precos").fetchone()
    campos_json = ["nome", "edicao", "set", "raridade", "reserved_list", "usd",
                   "usd_mais_barata_outra_edicao", "reprints_pos_2003", "ultimo_reprint", "link_liga"]
    dados = {
        "gerado": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "cambio": round(cambio, 4), "n_cartas": len(cartas),
        "n_datas": datas[0], "primeira": datas[1], "ultima": datas[2],
        "c": [{k: c[k] for k in campos_json} for c in cartas],
        "h": hist,
    }
    return dados


def gerar_painel(dados):
    with open(MODELO, encoding="utf-8") as f:
        html = f.read()
    if "<!--PM_DATA-->" not in html:
        raise RuntimeError("painel_modelo.html esta sem o marcador <!--PM_DATA-->.")
    js = json.dumps(dados, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("<!--PM_DATA-->", "<script>window.PM_DATA=" + js + ";</script>")
    with open(PAINEL + ".tmp", "w", encoding="utf-8") as f:
        f.write(html)
    os.replace(PAINEL + ".tmp", PAINEL)


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sem-download", action="store_true")
    ap.add_argument("--mtgjson", action="store_true")
    ap.add_argument("--sem-abrir", action="store_true")
    ap.add_argument("--piso", type=float, default=PISO_USD)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass

    log("=== Premodern Screener ===")
    con = sqlite3.connect(DB_PATH)
    criar_tabelas(con)

    if not args.sem_download:
        atualizar_scryfall(con)
        if args.mtgjson or precisa_mtgjson(con):
            try:
                importar_mtgjson(con)
            except Exception as erro:       # historico e opcional: nao derruba o resto
                log(f"MTGJSON falhou ({erro}). Seguindo sem ele.")
                log(traceback.format_exc())
        else:
            log("MTGJSON: historico ja esta em dia, pulando.")

    cambio = obter_cambio() if not args.sem_download else CAMBIO_MANUAL
    dados = gerar_saidas(con, cambio, args.piso)
    con.close()
    gerar_painel(dados)
    log(f"Painel gerado: {dados['n_cartas']} cartas, {dados['n_datas']} datas de preco "
        f"({dados['primeira']} a {dados['ultima']}).")
    log(f"Arquivo: {PAINEL}")

    if not args.sem_abrir:
        webbrowser.open("file:///" + PAINEL.replace("\\", "/"))


if __name__ == "__main__":
    try:
        main()
    except Exception as erro:
        log(f"ERRO: {erro}")
        log(traceback.format_exc())
        sys.exit(1)

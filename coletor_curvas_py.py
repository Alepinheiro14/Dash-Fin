#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
coletor_curvas.py
=================
Coleta curvas de juros REAIS de três regiões, mantém histórico append-only e
regenera `dados.js` (window.DADOS = {...}) consumido pelo dashboard HTML estático.

Fontes:
  BR  -> Tesouro Transparente (CSV)  : Tesouro IPCA+ (NTN-B Principal, sem juros semestrais) = taxa REAL
  US  -> US Treasury (CSV)           : Daily Treasury PAR Real Yield Curve (TIPS) = REAL, vértices 5/7/10/20/30a
  EU  -> BCE (API SDMX)              : curva nominal AAA  −  breakeven (sintético) = REAL aproximada

Saídas (na mesma pasta do script):
  historico_curvas.csv   -> histórico longo: data_coleta, regiao, data_ref, prazo_anos, taxa_real, coletado_em
  dados.js               -> window.DADOS = { meta, snapshots: { "YYYY-MM-DD": {BR:[[mat,taxa]],US:[...],EU:[...]} } }

Uso:
  pip install requests pandas
  python coletor_curvas.py
"""

import io
import os
import sys
import json
import datetime as dt

import requests
import pandas as pd

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
PASTA = os.path.dirname(os.path.abspath(__file__))
HIST_CSV = os.path.join(PASTA, "historico_curvas.csv")
OUT_JS = os.path.join(PASTA, "dados.js")

HEADERS = {"User-Agent": "Mozilla/5.0 (curvas-juros-collector)"}
TIMEOUT = 60

HOJE = dt.date.today().isoformat()
AGORA = dt.datetime.now().isoformat(timespec="seconds")

# URLs ---------------------------------------------------------------------- #
URL_BR = ("https://www.tesourotransparente.gov.br/ckan/dataset/"
          "df56aa42-484a-4a59-8184-7676580c81e3/resource/"
          "796d2059-14e9-44e3-80c9-2d9e30b405c1/download/precotaxatesourodireto.csv")

def url_us(ano: int) -> str:
    return ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
            f"daily-treasury-rates.csv/{ano}/all"
            f"?type=daily_treasury_real_yield_curve&field_tdr_date_value={ano}&page&_format=csv")

# BCE: uma única chamada com OR (+) na dimensão de maturidade
ECB_MATS = {
    "SR_3M": 0.25, "SR_6M": 0.5, "SR_1Y": 1, "SR_2Y": 2, "SR_3Y": 3,
    "SR_5Y": 5, "SR_7Y": 7, "SR_10Y": 10, "SR_15Y": 15, "SR_20Y": 20, "SR_30Y": 30,
}
URL_EU = ("https://data-api.ecb.europa.eu/service/data/YC/"
          "B.U2.EUR.4F.G_N_A.SV_C_YM." + "+".join(ECB_MATS.keys()) +
          "?lastNObservations=1&format=csvdata")

# Breakeven da área do euro (ancorado em níveis de mercado) -> proxy automático.
# Para usar swaps de inflação reais, preencha fetch_breakeven_market() abaixo.
BE_ANCHORS = [(0.25, 1.90), (1, 1.95), (2, 2.00), (5, 2.05),
              (10, 2.10), (20, 2.18), (30, 2.20)]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r

def _achar_coluna(cols, *chaves):
    """Encontra a 1ª coluna cujo nome (sem acento/maiúsc) contém todas as chaves."""
    def norm(s):
        return (s.lower().replace("ã", "a").replace("á", "a").replace("ç", "c")
                 .replace("é", "e").strip())
    for c in cols:
        nc = norm(c)
        if all(norm(k) in nc for k in chaves):
            return c
    return None

def _interp(anchors, x):
    """Interpolação linear sobre lista [(x,y)] ordenada por x."""
    if x <= anchors[0][0]:
        return anchors[0][1]
    if x >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in zip(anchors, anchors[1:]):
        if x0 <= x <= x1:
            f = (x - x0) / (x1 - x0)
            return y0 + f * (y1 - y0)
    return anchors[-1][1]


# --------------------------------------------------------------------------- #
# BR — Tesouro IPCA+ (NTN-B Principal)
# --------------------------------------------------------------------------- #
def coletar_brasil() -> dict | None:
    print("[BR] baixando CSV do Tesouro Transparente...")
    r = _get(URL_BR)
    # arquivo costuma ser latin-1; tenta latin-1 e cai pra utf-8
    try:
        txt = r.content.decode("latin-1")
    except UnicodeDecodeError:
        txt = r.content.decode("utf-8", errors="replace")

    df = pd.read_csv(io.StringIO(txt), sep=";", decimal=",")
    col_tipo = _achar_coluna(df.columns, "tipo", "titulo")
    col_venc = _achar_coluna(df.columns, "data", "vencimento")
    col_base = _achar_coluna(df.columns, "data", "base")
    col_taxa = _achar_coluna(df.columns, "taxa", "compra")  # taxa de compra (manhã)
    if not all([col_tipo, col_venc, col_base, col_taxa]):
        raise RuntimeError(f"[BR] colunas não encontradas: {list(df.columns)}")

    # NTN-B Principal = "Tesouro IPCA+"  (exclui "com Juros Semestrais")
    df = df[df[col_tipo].astype(str).str.strip().str.lower() == "tesouro ipca+"].copy()
    df[col_venc] = pd.to_datetime(df[col_venc], dayfirst=True, errors="coerce")
    df[col_base] = pd.to_datetime(df[col_base], dayfirst=True, errors="coerce")
    df = df.dropna(subset=[col_venc, col_base, col_taxa])

    data_ref = df[col_base].max()
    snap = df[df[col_base] == data_ref].copy()
    snap["prazo"] = (snap[col_venc] - snap[col_base]).dt.days / 365.25
    snap = snap[snap["prazo"] > 1].sort_values("prazo")  # exclui < 1 ano (instrumento de mercado monetário)

    curva = [[round(float(p), 2), round(float(t), 3)]
             for p, t in zip(snap["prazo"], snap[col_taxa])]
    print(f"[BR] {data_ref.date()} — {len(curva)} vértices (real)")
    return {"data_ref": data_ref.date().isoformat(), "curva": curva}


# --------------------------------------------------------------------------- #
# US — Daily Treasury Par Real Yield Curve (TIPS)
# --------------------------------------------------------------------------- #
def coletar_eua() -> dict | None:
    ano = dt.date.today().year
    print(f"[US] baixando Real Yield Curve {ano}...")
    try:
        r = _get(url_us(ano))
        df = pd.read_csv(io.StringIO(r.text))
        if df.empty:  # virada de ano sem dados ainda
            raise ValueError("vazio")
    except Exception:
        r = _get(url_us(ano - 1))
        df = pd.read_csv(io.StringIO(r.text))

    col_data = _achar_coluna(df.columns, "date") or df.columns[0]
    df[col_data] = pd.to_datetime(df[col_data], errors="coerce")
    df = df.dropna(subset=[col_data]).sort_values(col_data)
    linha = df.iloc[-1]
    data_ref = linha[col_data].date().isoformat()

    curva = []
    for c in df.columns:
        if "yr" in c.lower():  # ex.: "5 YR", "10 YR"
            try:
                mat = float("".join(ch for ch in c if ch.isdigit() or ch == "."))
            except ValueError:
                continue
            val = linha[c]
            if pd.notna(val):
                curva.append([mat, round(float(val), 3)])
    curva.sort()
    print(f"[US] {data_ref} — {len(curva)} vértices (real)")
    return {"data_ref": data_ref, "curva": curva}


# --------------------------------------------------------------------------- #
# EU — nominal AAA (BCE) − breakeven (sintético)
# --------------------------------------------------------------------------- #
def fetch_breakeven_market(maturidades_anos):
    """
    Gancho para breakeven de mercado (ex.: swaps de inflação da área do euro).
    Retorne dict {anos: breakeven_%} ou None para usar o proxy ancorado.
    """
    return None  # proxy ancorado por padrão (automático, sem input manual)

def coletar_euro() -> dict | None:
    print("[EU] baixando curva nominal AAA do BCE (SDMX)...")
    r = _get(URL_EU)
    df = pd.read_csv(io.StringIO(r.text))
    col_key = _achar_coluna(df.columns, "key") or "KEY"
    col_per = _achar_coluna(df.columns, "time", "period") or "TIME_PERIOD"
    col_val = _achar_coluna(df.columns, "obs", "value") or "OBS_VALUE"

    nominal = {}
    data_ref = None
    for _, row in df.iterrows():
        token = str(row[col_key]).split(".")[-1]      # ex.: "SR_10Y"
        anos = ECB_MATS.get(token)
        if anos is None or pd.isna(row[col_val]):
            continue
        nominal[anos] = float(row[col_val])
        data_ref = str(row[col_per])

    if not nominal:
        raise RuntimeError("[EU] nenhuma observação nominal retornada pelo BCE")

    anos_ord = sorted(nominal.keys())
    be_mkt = fetch_breakeven_market(anos_ord)
    curva = []
    for a in anos_ord:
        be = be_mkt[a] if (be_mkt and a in be_mkt) else _interp(BE_ANCHORS, a)
        real = nominal[a] - be
        curva.append([a, round(real, 3)])
    fonte_be = "swaps de mercado" if be_mkt else "breakeven ancorado (proxy)"
    print(f"[EU] {data_ref} — {len(curva)} vértices (real sintética; {fonte_be})")
    return {"data_ref": data_ref, "curva": curva}


# --------------------------------------------------------------------------- #
# Persistência + geração do dados.js
# --------------------------------------------------------------------------- #
def salvar_historico(snaps: dict):
    linhas = []
    for regiao, info in snaps.items():
        if not info:
            continue
        for prazo, taxa in info["curva"]:
            linhas.append({
                "data_coleta": HOJE,
                "regiao": regiao,
                "data_ref": info["data_ref"],
                "prazo_anos": prazo,
                "taxa_real": taxa,
                "coletado_em": AGORA,
            })
    novo = pd.DataFrame(linhas)
    if os.path.exists(HIST_CSV):
        antigo = pd.read_csv(HIST_CSV)
        # remove a coleta de hoje p/ regiões recoletadas (idempotente no mesmo dia)
        mask = ~((antigo["data_coleta"] == HOJE) & (antigo["regiao"].isin(snaps.keys())))
        df = pd.concat([antigo[mask], novo], ignore_index=True)
    else:
        df = novo
    df.to_csv(HIST_CSV, index=False)
    print(f"[hist] {len(novo)} linhas adicionadas — total {len(df)} em {os.path.basename(HIST_CSV)}")

def gerar_dados_js():
    df = pd.read_csv(HIST_CSV)
    snapshots = {}
    for data_coleta, g_data in df.groupby("data_coleta"):
        snap = {}
        for regiao, g_reg in g_data.groupby("regiao"):
            g_reg = g_reg.sort_values("prazo_anos")
            snap[regiao] = [[float(p), float(t)]
                            for p, t in zip(g_reg["prazo_anos"], g_reg["taxa_real"])]
        snapshots[str(data_coleta)] = snap

    payload = {
        "meta": {"source": "coletor_curvas.py", "generated": AGORA,
                 "note": "BR=NTN-B real; US=TIPS real; EU=real sintetica (nominal BCE - breakeven)"},
        "snapshots": snapshots,
    }
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.DADOS = ")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    print(f"[js] {len(snapshots)} snapshots -> {os.path.basename(OUT_JS)}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    snaps = {}
    for nome, fn in [("BR", coletar_brasil), ("US", coletar_eua), ("EU", coletar_euro)]:
        try:
            snaps[nome] = fn()
        except Exception as e:
            print(f"[!] {nome} falhou: {e}", file=sys.stderr)
            snaps[nome] = None

    if not any(snaps.values()):
        print("[x] nenhuma fonte coletada. Abortando.", file=sys.stderr)
        sys.exit(1)

    salvar_historico({k: v for k, v in snaps.items() if v})
    gerar_dados_js()
    print("\nOK — atualize/abra o dashboard.html para ver os dados.")

if __name__ == "__main__":
    main()

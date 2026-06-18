#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
coletor_mercado.py
==================
Coleta fechamento de índices de bolsa e câmbio, mantém histórico e
regenera `dados_mercado.js` (window.DADOS_MERCADO = {...}).

Fontes:
  Índices -> yfinance (Yahoo Finance)
  Câmbio  -> BCB PTAX (API oficial, sem chave)

Saídas:
  historico_mercado.csv
  dados_mercado.js
"""

import io
import os
import sys
import json
import datetime as dt

import requests
import pandas as pd

try:
    import yfinance as yf
except ImportError:
    print("[!] yfinance não instalado. Execute: pip install yfinance", file=sys.stderr)
    sys.exit(1)

PASTA  = os.path.dirname(os.path.abspath(__file__))
HIST   = os.path.join(PASTA, "historico_mercado.csv")
OUT_JS = os.path.join(PASTA, "dados_mercado.js")

HOJE  = dt.date.today().isoformat()
AGORA = dt.datetime.now().isoformat(timespec="seconds")

# Janela histórica gravada (dias corridos)
JANELA_DIAS = 180

INDICES = {
    "IBOV":   {"ticker": "^BVSP",    "regiao": "BR", "nome": "Ibovespa",    "moeda": "BRL", "escala": 1},
    "SP500":  {"ticker": "^GSPC",    "regiao": "US", "nome": "S&P 500",     "moeda": "USD", "escala": 1},
    "NASDAQ": {"ticker": "^IXIC",    "regiao": "US", "nome": "Nasdaq Comp.", "moeda": "USD", "escala": 1},
    "DAX":    {"ticker": "^GDAXI",   "regiao": "EU", "nome": "DAX",         "moeda": "EUR", "escala": 1},
    "STOXX50":{"ticker": "^STOXX50E","regiao": "EU", "nome": "Euro Stoxx 50","moeda":"EUR",  "escala": 1},
}

HEADERS = {"User-Agent": "Mozilla/5.0 (curvas-juros-collector)"}


# --------------------------------------------------------------------------- #
# Índices via yfinance
# --------------------------------------------------------------------------- #
def coletar_indices() -> pd.DataFrame:
    print("[idx] baixando índices via yfinance...")
    inicio = (dt.date.today() - dt.timedelta(days=JANELA_DIAS + 10)).isoformat()
    tickers = [v["ticker"] for v in INDICES.values()]
    raw = yf.download(tickers, start=inicio, auto_adjust=True, progress=False)

    # yfinance retorna MultiIndex quando > 1 ticker
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]]
        close.columns = [tickers[0]]

    # Renomeia colunas para nossos códigos
    ticker_to_code = {v["ticker"]: k for k, v in INDICES.items()}
    close = close.rename(columns=ticker_to_code)
    close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
    close = close.dropna(how="all")

    linhas = []
    for data, row in close.iterrows():
        for codigo, valor in row.items():
            if pd.isna(valor):
                continue
            meta = INDICES[codigo]
            linhas.append({
                "data": data.date().isoformat(),
                "codigo": codigo,
                "regiao": meta["regiao"],
                "nome": meta["nome"],
                "moeda": meta["moeda"],
                "fechamento": round(float(valor), 2),
            })

    df = pd.DataFrame(linhas)
    if not df.empty:
        print(f"[idx] {len(df)} obs de {df['codigo'].nunique()} indices "
              f"({df['data'].min()} a {df['data'].max()})")
    return df


# --------------------------------------------------------------------------- #
# Câmbio via BCB PTAX
# --------------------------------------------------------------------------- #
def _ptax_fetch(url: str) -> pd.DataFrame:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    dados = r.json().get("value", [])
    rows = []
    for d in dados:
        ts = pd.to_datetime(d["dataHoraCotacao"])
        rows.append({"data": ts.date().isoformat(), "taxa": float(d["cotacaoVenda"])})
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("data").drop_duplicates("data", keep="last")
    return df

def _ptax_dolar(inicio: str, fim: str) -> pd.DataFrame:
    url = (
        "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
        f"CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
        f"?@dataInicial='{inicio}'&@dataFinalCotacao='{fim}'"
        f"&$format=json&$select=cotacaoVenda,dataHoraCotacao"
    )
    return _ptax_fetch(url)

def _ptax_moeda(moeda: str, inicio: str, fim: str) -> pd.DataFrame:
    url = (
        "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
        f"CotacaoMoedaPeriodo(codigoMoeda=@codigoMoeda,dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
        f"?@codigoMoeda='{moeda}'&@dataInicial='{inicio}'&@dataFinalCotacao='{fim}'"
        f"&$format=json&$select=cotacaoVenda,dataHoraCotacao"
    )
    return _ptax_fetch(url)

def coletar_cambio() -> pd.DataFrame:
    print("[fx] baixando cambio (BCB PTAX para USD, yfinance para EUR)...")
    inicio_yf  = (dt.date.today() - dt.timedelta(days=JANELA_DIAS + 10)).isoformat()
    inicio_bcb = (dt.date.today() - dt.timedelta(days=JANELA_DIAS + 10)).strftime("%m-%d-%Y")
    fim_bcb    = dt.date.today().strftime("%m-%d-%Y")

    linhas = []

    # USD/BRL — BCB PTAX (fonte oficial)
    try:
        df_usd = _ptax_dolar(inicio_bcb, fim_bcb)
        for _, row in df_usd.iterrows():
            linhas.append({"data": row["data"], "codigo": "USDBRL",
                           "regiao": "FX", "nome": "USD/BRL (PTAX)",
                           "moeda": "BRL", "fechamento": round(row["taxa"], 4)})
    except Exception as e:
        print(f"[!] FX USDBRL (BCB) falhou: {e}", file=sys.stderr)

    # EUR/BRL — yfinance
    try:
        raw = yf.download("EURBRL=X", start=inicio_yf, auto_adjust=True, progress=False)
        if isinstance(raw.columns, pd.MultiIndex):
            close = raw["Close"].iloc[:, 0]
        else:
            close = raw["Close"]
        close.index = pd.to_datetime(close.index).tz_localize(None).normalize()
        for data, val in close.dropna().items():
            linhas.append({"data": data.date().isoformat(), "codigo": "EURBRL",
                           "regiao": "FX", "nome": "EUR/BRL",
                           "moeda": "BRL", "fechamento": round(float(val), 4)})
    except Exception as e:
        print(f"[!] FX EURBRL (yfinance) falhou: {e}", file=sys.stderr)

    df = pd.DataFrame(linhas)
    if not df.empty:
        print(f"[fx] {len(df)} obs de {df['codigo'].nunique()} pares "
              f"({df['data'].min()} a {df['data'].max()})")
    return df


# --------------------------------------------------------------------------- #
# Persistência
# --------------------------------------------------------------------------- #
def salvar_historico(novo: pd.DataFrame):
    if os.path.exists(HIST):
        antigo = pd.read_csv(HIST)
        df = pd.concat([antigo, novo], ignore_index=True)
    else:
        df = novo

    # dedup por (data, codigo) — mantém a entrada mais recente
    df = df.drop_duplicates(subset=["data", "codigo"], keep="last")

    corte = (dt.date.today() - dt.timedelta(days=JANELA_DIAS + 30)).isoformat()
    df = df[df["data"] >= corte]
    df = df.sort_values(["codigo", "data"]).reset_index(drop=True)
    df.to_csv(HIST, index=False)
    print(f"[hist] total {len(df)} linhas em {os.path.basename(HIST)}")
    return df


def gerar_js(df: pd.DataFrame):
    series = {}
    for codigo, g in df.groupby("codigo"):
        g = g.sort_values("data")
        meta = g.iloc[-1]
        series[codigo] = {
            "regiao": meta["regiao"],
            "nome":   meta["nome"],
            "moeda":  meta["moeda"],
            "serie":  [[row["data"], row["fechamento"]] for _, row in g.iterrows()],
        }

    payload = {
        "meta": {"source": "coletor_mercado.py", "generated": AGORA},
        "series": series,
    }
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write("window.DADOS_MERCADO = ")
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")
    print(f"[js] {len(series)} séries -> {os.path.basename(OUT_JS)}")


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    partes = []

    try:
        partes.append(coletar_indices())
    except Exception as e:
        print(f"[!] índices falharam: {e}", file=sys.stderr)

    try:
        partes.append(coletar_cambio())
    except Exception as e:
        print(f"[!] câmbio falhou: {e}", file=sys.stderr)

    partes = [p for p in partes if not p.empty]
    if not partes:
        print("[x] nenhum dado coletado.", file=sys.stderr)
        sys.exit(1)

    novo = pd.concat(partes, ignore_index=True)
    df   = salvar_historico(novo)
    gerar_js(df)
    print("\nOK — dados_mercado.js atualizado.")

if __name__ == "__main__":
    main()

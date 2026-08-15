#!/usr/bin/env python3
"""Consolidated NSE F&O breakout screener over all fetched Kite historical data.

Reads every per-ticker JSON file in data/nse_historical/2026-08-15/, applies the
same 5-condition breakout screen as the original yfinance script, and writes a
single consolidated Breakout_Stocks_2026-08-15.xlsx/.csv.
"""
import json
import os
import pandas as pd

DATA_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.dirname(os.path.abspath(__file__))

results = []
skipped = []
resolved = []

for fname in sorted(os.listdir(DATA_DIR)):
    if not fname.endswith(".json") or fname == "_combined.json":
        continue
    ticker = fname[:-5]
    with open(os.path.join(DATA_DIR, fname)) as f:
        raw = json.load(f)

    candles = raw["candles"] if isinstance(raw, dict) else raw
    if len(candles) < 200:
        skipped.append((ticker, f"only {len(candles)} candles"))
        continue

    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)

    df["DMA_30"] = df["close"].rolling(window=30).mean()
    df["DMA_50"] = df["close"].rolling(window=50).mean()
    df["DMA_200"] = df["close"].rolling(window=200).mean()

    latest_close = float(df["close"].iloc[-1])
    dma_30 = float(df["DMA_30"].iloc[-1])
    dma_50 = float(df["DMA_50"].iloc[-1])
    dma_200 = float(df["DMA_200"].iloc[-1])

    dist_200dma = ((latest_close - dma_200) / dma_200) * 100

    year_high_idx = df["close"].idxmax()
    data_from_high = df.loc[year_high_idx:]
    car_series = data_from_high["close"].pct_change().cumsum()
    positive_car_10_days = (car_series.tail(10) > 0).all() if len(car_series) >= 10 else False

    resolved.append(ticker)

    if (
        latest_close > dma_30
        and latest_close > dma_50
        and latest_close > dma_200
        and 0 <= dist_200dma <= 10
        and positive_car_10_days
    ):
        results.append(
            {
                "Ticker": ticker,
                "Current Price": round(latest_close, 2),
                "30 DMA": round(dma_30, 2),
                "50 DMA": round(dma_50, 2),
                "200 DMA": round(dma_200, 2),
                "Distance to 200 DMA (%)": round(dist_200dma, 2),
                "CAR (10 Days)": "Positive",
            }
        )

df_results = pd.DataFrame(results)
if not df_results.empty:
    df_results = df_results.sort_values(by="Distance to 200 DMA (%)")

xlsx_path = os.path.join(OUT_DIR, "Breakout_Stocks_2026-08-15.xlsx")
csv_path = os.path.join(OUT_DIR, "Breakout_Stocks_2026-08-15.csv")
df_results.to_excel(xlsx_path, index=False)
df_results.to_csv(csv_path, index=False)

print(f"Resolved & screened: {len(resolved)}")
print(f"Skipped: {len(skipped)} -> {skipped}")
print(f"Passed screen: {len(df_results)}")
print(df_results.to_string(index=False) if not df_results.empty else "No stocks met all breakout criteria.")

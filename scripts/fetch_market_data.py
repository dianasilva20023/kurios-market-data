import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

TIINGO_TOKEN = os.getenv("TIINGO_API_TOKEN")

ROOT = Path(__file__).resolve().parents[1]
TICKERS_PATH = ROOT / "source" / "tickers.json"
OUTPUT_DIR = ROOT / "public" / "processed"
ASSETS_DIR = OUTPUT_DIR / "assets"

ASSETS_DIR.mkdir(parents=True, exist_ok=True)

DATA_VERSION = "tiingo-static-2015-2026"
START_DATE = "2015-01-01"

# None = hasta el último dato disponible en Tiingo.
END_DATE = None

REQUEST_DELAY_SECONDS = 1.0


def fetch_tiingo_prices(ticker: str):
    if not TIINGO_TOKEN:
        raise RuntimeError("Falta TIINGO_API_TOKEN en .env")

    symbol = ticker.lower()

    url = f"https://api.tiingo.com/tiingo/daily/{symbol}/prices"

    params = {
        "startDate": START_DATE,
        "token": TIINGO_TOKEN
    }

    if END_DATE:
        params["endDate"] = END_DATE

    print(f"Descargando Tiingo: {ticker} desde {START_DATE}")

    response = requests.get(
        url,
        params=params,
        timeout=60,
        headers={
            "Content-Type": "application/json"
        }
    )

    response.raise_for_status()

    data = response.json()

    if isinstance(data, dict):
        raise RuntimeError(f"Tiingo devolvió error para {ticker}: {data}")

    if not isinstance(data, list):
        raise RuntimeError(f"Respuesta inesperada de Tiingo para {ticker}: {data}")

    if len(data) == 0:
        raise RuntimeError(f"Tiingo no devolvió precios para {ticker}")

    return data


def normalize_prices(tiingo_rows):
    rows = []

    for item in tiingo_rows:
        try:
            date_raw = item.get("date", "")
            date_str = date_raw[:10]

            # Usamos precios ajustados si existen para reflejar splits/dividendos mejor.
            open_price = item.get("adjOpen", item.get("open"))
            high_price = item.get("adjHigh", item.get("high"))
            low_price = item.get("adjLow", item.get("low"))
            close_price = item.get("adjClose", item.get("close"))

            if open_price is None or high_price is None or low_price is None or close_price is None:
                continue

            rows.append([
                date_str,
                round(float(open_price), 2),
                round(float(high_price), 2),
                round(float(low_price), 2),
                round(float(close_price), 2)
            ])

        except Exception:
            continue

    rows.sort(key=lambda x: x[0])
    return rows


def build_asset_payload(asset, prices):
    if len(prices) == 0:
        raise RuntimeError(f"No hay precios normalizados para {asset['ticker']}")

    last = prices[-1]
    previous = prices[-2] if len(prices) > 1 else prices[-1]

    current_price = last[4]
    previous_price = previous[4]

    change_value = round(current_price - previous_price, 2)

    if previous_price != 0:
        change_pct = round((change_value / previous_price) * 100, 2)
    else:
        change_pct = 0

    return {
        "schema": "kurios.market.asset.v1",
        "source": "Tiingo",
        "version": DATA_VERSION,
        "id": asset["id"],
        "ticker": asset["ticker"],
        "name": asset["name"],
        "logo": asset.get("logo", ""),
        "type": asset.get("type", "stock"),
        "currentPrice": current_price,
        "previousPrice": previous_price,
        "changeValue": change_value,
        "changePct": change_pct,
        "prices": prices
    }


def main():
    with open(TICKERS_PATH, "r", encoding="utf-8") as file:
        assets = json.load(file)

    manifest_assets = []

    for index, asset in enumerate(assets):
        ticker = asset["ticker"]

        try:
            raw_prices = fetch_tiingo_prices(ticker)
            prices = normalize_prices(raw_prices)
            payload = build_asset_payload(asset, prices)

            file_name = f"{ticker}.json"
            output_path = ASSETS_DIR / file_name

            with open(output_path, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, separators=(",", ":"))

            manifest_assets.append({
                "id": asset["id"],
                "ticker": asset["ticker"],
                "name": asset["name"],
                "logo": asset.get("logo", ""),
                "type": asset.get("type", "stock"),
                "file": f"assets/{file_name}",
                "currentPrice": payload["currentPrice"],
                "previousPrice": payload["previousPrice"],
                "changeValue": payload["changeValue"],
                "changePct": payload["changePct"],
                "priceCount": len(prices)
            })

            print(f"OK {ticker}: {len(prices)} velas guardadas en {output_path}")

        except Exception as error:
            print(f"ERROR {ticker}: {error}")

        if index < len(assets) - 1:
            time.sleep(REQUEST_DELAY_SECONDS)

    generated_at = datetime.now(timezone.utc).isoformat()

    manifest = {
        "schema": "kurios.market.manifest.v1",
        "source": "Tiingo",
        "version": DATA_VERSION,
        "generatedAt": generated_at,
        "startDate": START_DATE,
        "endDate": END_DATE,
        "turnLengthMonths": 1,
        "assets": manifest_assets
    }

    manifest_path = OUTPUT_DIR / "investment_manifest.json"

    with open(manifest_path, "w", encoding="utf-8") as file:
        json.dump(manifest, file, ensure_ascii=False, separators=(",", ":"))

    print(f"\nManifest generado en: {manifest_path}")
    print(f"Activos incluidos: {len(manifest_assets)}")


if __name__ == "__main__":
    main()

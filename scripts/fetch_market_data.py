import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timezone
from dotenv import load_dotenv
from dateutil.relativedelta import relativedelta

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
SUMMARY_FILE_NAME = "market_turn_summary.json"
TURN_LENGTH_MONTHS = 1
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

def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d")


def get_closest_price_on_or_before(prices, target_date):
    selected = None

    for row in prices:
        row_date = parse_date(row[0])

        if row_date <= target_date:
            selected = row
        else:
            break

    return selected


def build_market_turn_summary(assets_payloads):
    """
    Crea un resumen mensual por marketTurn para que Unity pueda pintar
    las tarjetas sin descargar los 50 JSON completos.
    """

    start_dt = parse_date(START_DATE)

    # Buscamos hasta qué fecha llega la data.
    last_dates = []

    for item in assets_payloads:
        prices = item["payload"].get("prices", [])

        if prices:
            last_dates.append(parse_date(prices[-1][0]))

    if not last_dates:
        return {
            "schema": "kurios.market.summary.v1",
            "source": "Tiingo",
            "version": DATA_VERSION,
            "startDate": START_DATE,
            "turnLengthMonths": TURN_LENGTH_MONTHS,
            "assets": []
        }

    global_last_date = min(last_dates)

    # Cantidad de turnos mensuales posibles desde START_DATE hasta la menor fecha final común.
    total_months = (global_last_date.year - start_dt.year) * 12 + (global_last_date.month - start_dt.month)

    summary_assets = []

    for item in assets_payloads:
        asset = item["asset"]
        payload = item["payload"]
        prices = payload.get("prices", [])

        turns = []

        previous_close = None

        for market_turn in range(0, total_months + 1):
            target_date = start_dt + relativedelta(months=market_turn * TURN_LENGTH_MONTHS)

            price_row = get_closest_price_on_or_before(prices, target_date)

            if price_row is None:
                continue

            date_str = price_row[0]
            close_price = round(float(price_row[4]), 2)

            if previous_close is None:
                change_value = 0
                change_pct = 0
            else:
                change_value = round(close_price - previous_close, 2)
                change_pct = round((change_value / previous_close) * 100, 2) if previous_close != 0 else 0

            turns.append({
                "marketTurn": market_turn,
                "date": date_str,
                "price": close_price,
                "previousPrice": previous_close if previous_close is not None else close_price,
                "changeValue": change_value,
                "changePct": change_pct
            })

            previous_close = close_price

        summary_assets.append({
            "id": asset["id"],
            "ticker": asset["ticker"],
            "name": asset["name"],
            "logo": asset.get("logo", ""),
            "type": asset.get("type", "stock"),
            "turns": turns
        })

    return {
        "schema": "kurios.market.summary.v1",
        "source": "Tiingo",
        "version": DATA_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "startDate": START_DATE,
        "turnLengthMonths": TURN_LENGTH_MONTHS,
        "assets": summary_assets
    }

def main():
    with open(TICKERS_PATH, "r", encoding="utf-8") as file:
        assets = json.load(file)

    manifest_assets = []
    assets_payloads = []

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
            
            assets_payloads.append({
   		 "asset": asset,
   		 "payload": payload
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
    summary = build_market_turn_summary(assets_payloads)

    summary_path = OUTPUT_DIR / SUMMARY_FILE_NAME

    with open(summary_path, "w", encoding="utf-8") as file:
        json.dump(summary, file, ensure_ascii=False, separators=(",", ":"))

    print(f"Market turn summary generado en: {summary_path}")
    print(f"\nManifest generado en: {manifest_path}")
    print(f"Activos incluidos: {len(manifest_assets)}")


if __name__ == "__main__":
    main()

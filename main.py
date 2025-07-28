import asyncio
import pandas as pd
import pandas_ta as ta
from datetime import datetime
from binance.client import Client
import requests
# ONE MTS TIMEFRAME PLAY BOT.
# ------------------- CONFIG -------------------
SYMBOL = "XRPUSDT"
ENTRY_INTERVAL = '1m'
CANDLES_ENTRY = 70
CHECK_INTERVAL_SECONDS = 15

EMA_SHORT = 3
EMA_LONG = 18

FIXED_QTY = 1.7
STOP_LOSS_BUFFER = 0.019
SL_BUFFER_GAIN = 0.003
RISK_REWARD_RATIO = 2.1
MIN_GAP_PERCENT = 0.029

API_KEY = "22jj7Y6jJoBdWA64qzlxoFlfAM2b6UIf7Kva3VbNFNn0VWVvWTQRVb5sKLlunjLv"
API_SECRET = "wHcp9Gke2YzzEFIIBMzY86aSgCADroGIgf2muBAOwe49VfUCyogHYoaulnMv0AWK"
TELEGRAM_TOKEN = "7285134088:AAHPd1nsJ2Bsv3esWcyxFizsBYj6cJ9xi8U"
TELEGRAM_CHAT_ID = 7053958805

client = Client(API_KEY, API_SECRET)

# ------------------- STATE -------------------
signal_locked = None
active_trade = None
waiting_for_gap = None
prev_cross_state = None
breakeven_applied = False

total_trades = 0
win_trades = 0
loss_trades = 0
last_stats_time = datetime.utcnow()

# ------------------- FUNCTIONS -------------------
def send_telegram(msg):
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg}
        )
    except Exception as e:
        print("Telegram Error:", e)

def klines_to_df(klines):
    df = pd.DataFrame(klines, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base_asset_volume", "taker_buy_quote_asset_volume", "ignore"
    ])
    df["close"] = pd.to_numeric(df["close"])
    return df

def calculate_emas(df):
    df["EMA_SHORT"] = ta.ema(df["close"], length=EMA_SHORT)
    df["EMA_LONG"] = ta.ema(df["close"], length=EMA_LONG)
    return df

def get_latest_price():
    ticker = client.futures_symbol_ticker(symbol=SYMBOL)
    return float(ticker['price'])

def place_market_order(side, reduce_only=False):
    try:
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type="MARKET",
            quantity=FIXED_QTY,
            reduceOnly=reduce_only
        )
        msg = f"✅ Order placed: {side} | Qty: {FIXED_QTY}"
        print(msg)
        send_telegram(msg)
        return order
    except Exception as e:
        print("Order Error:", e)
        send_telegram(f"Order Error: {e}")
        return None

def exit_trade(result=None):
    global active_trade, total_trades, win_trades, loss_trades
    if active_trade:
        side = "SELL" if active_trade["type"] == "buy" else "BUY"
        place_market_order(side, reduce_only=True)
        send_telegram(f"🔚 Trade closed: {result.upper() if result else 'manual'}")

        total_trades += 1
        if result == "win":
            win_trades += 1
        elif result == "loss":
            loss_trades += 1

        active_trade.clear()

def print_stats():
    balance = client.futures_account_balance()
    usdt_balance = next((b for b in balance if b['asset'] == 'USDT'), {}).get('balance', '0')
    stats = (
        f"\n📊 STATS @ {datetime.utcnow().strftime('%H:%M:%S')} UTC"
        f"\n💰 Balance: {usdt_balance[:7]} USDT"
        f"\n📈 Trades — Total: {total_trades}, Wins: {win_trades}, Losses: {loss_trades}"
    )
    print(stats)
    send_telegram(stats)

# ------------------- MAIN -------------------
async def signal_bot():
    global signal_locked, active_trade, waiting_for_gap, prev_cross_state, last_stats_time, breakeven_applied

    while True:
        try:
            now = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            print(f"\n🔍 Checking signal at {now} UTC")
            price = get_latest_price()

            if (datetime.utcnow() - last_stats_time).total_seconds() > 200:
                print_stats()
                last_stats_time = datetime.utcnow()

            df_entry = klines_to_df(client.futures_klines(symbol=SYMBOL, interval=ENTRY_INTERVAL, limit=CANDLES_ENTRY))
            df_entry = calculate_emas(df_entry)
            e = df_entry.iloc[-1]

            print(f"[{ENTRY_INTERVAL}] 📊 EMA{EMA_SHORT}: {e['EMA_SHORT']:.4f}, EMA{EMA_LONG}: {e['EMA_LONG']:.4f}, Price: {price:.4f}")

            # Detect crossover
            if prev_cross_state is None:
                if e["EMA_SHORT"] > e["EMA_LONG"]:
                    prev_cross_state = "buy"
                elif e["EMA_SHORT"] < e["EMA_LONG"]:
                    prev_cross_state = "sell"
                else:
                    prev_cross_state = "neutral"
                print(f"🔄 Initialized crossover: {prev_cross_state.upper()}")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            current_cross = None
            if e["EMA_SHORT"] > e["EMA_LONG"]:
                current_cross = "buy"
            elif e["EMA_SHORT"] < e["EMA_LONG"]:
                current_cross = "sell"

            # Manage active trade
            if active_trade:
                entry = active_trade["entry"]
                sl = active_trade["sl"]
                tp = active_trade["tp"]
                typ = active_trade["type"]

                if not breakeven_applied:
                    if typ == "buy" and price >= entry + STOP_LOSS_BUFFER:
                        active_trade["sl"] = entry + SL_BUFFER_GAIN
                        breakeven_applied = True
                        send_telegram(f"🔄 SL moved to breakeven for BUY: {active_trade['sl']:.4f}")
                    elif typ == "sell" and price <= entry - STOP_LOSS_BUFFER:
                        active_trade["sl"] = entry - SL_BUFFER_GAIN
                        breakeven_applied = True
                        send_telegram(f"🔄 SL moved to breakeven for SELL: {active_trade['sl']:.4f}")

                if typ == "buy" and price <= sl:
                    print(f"🛑 SL hit BUY: {price}")
                    exit_trade("loss")
                    signal_locked = None
                    breakeven_applied = False
                elif typ == "buy" and price >= tp:
                    print(f"✅ TP hit BUY: {price}")
                    exit_trade("win")
                    signal_locked = None
                    breakeven_applied = False
                elif typ == "sell" and price >= sl:
                    print(f"🛑 SL hit SELL: {price}")
                    exit_trade("loss")
                    signal_locked = None
                    breakeven_applied = False
                elif typ == "sell" and price <= tp:
                    print(f"✅ TP hit SELL: {price}")
                    exit_trade("win")
                    signal_locked = None
                    breakeven_applied = False

            # Entry logic
            if signal_locked is None:
                if current_cross != prev_cross_state:
                    waiting_for_gap = current_cross
                    print(f"📈 New trend detected: {current_cross.upper()}, waiting for EMA gap...")

            if waiting_for_gap == current_cross:
                gap = abs(e["EMA_SHORT"] - e["EMA_LONG"])
                gap_pct = (gap / price) * 100
                print(f"📏 EMA Gap: {gap_pct:.4f}%")

                if gap_pct >= MIN_GAP_PERCENT:
                    entry = price
                    sl = entry - STOP_LOSS_BUFFER if current_cross == "buy" else entry + STOP_LOSS_BUFFER
                    tp = entry + STOP_LOSS_BUFFER * RISK_REWARD_RATIO if current_cross == "buy" else entry - STOP_LOSS_BUFFER * RISK_REWARD_RATIO
                    place_market_order("BUY" if current_cross == "buy" else "SELL")
                    active_trade = {"type": current_cross, "entry": entry, "sl": sl, "tp": tp}
                    signal_locked = current_cross
                    waiting_for_gap = None
                    breakeven_applied = False
                    msg = f"🚀 {current_cross.upper()} ENTRY\nEntry: {entry:.4f}\nSL: {sl:.4f}\nTP: {tp:.4f}"
                    print(msg)
                    send_telegram(msg)

            # Reset if trend flips before gap is met
            if waiting_for_gap and current_cross != waiting_for_gap:
                print(f"⚠️ Trend changed before entry. Resetting to {current_cross.upper()}.")
                waiting_for_gap = current_cross

            prev_cross_state = current_cross

        except Exception as e:
            print("❌ Error:", e)
            send_telegram(f"❌ Error: {e}")

        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


# ------------------- RUN -------------------
if __name__ == "__main__":
    asyncio.run(signal_bot())

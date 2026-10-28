BOT_TOKEN      — токен от @BotFather (обязательно)
  CHAT_IDS       — id чатов через запятую (необязательно; можно просто написать боту /start)
  EXCHANGES      — по умолчанию okx,bitget,mexc
  TRADE_USD      — сумма сделки для расчёта глубины стакана, по умолчанию 300
  MIN_NET_PCT    — минимальный чистый спред в %, по умолчанию 0.5
  MAX_VOL_PCT    — макс. волатильность (стд. откл. 1-мин доходности, %), по умолчанию 0.15
  TAKER_FEE      — комиссия тейкера на одну сторону, по умолчанию 0.001 (0.1%)
  SCAN_SECONDS   — пауза между сканами, по умолчанию 30
"""
import asyncio
import logging
import os
import statistics
import time

import ccxt.async_support as ccxt
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("arb")

BOT_TOKEN = os.environ["BOT_TOKEN"]
EXCHANGES = [e.strip() for e in os.getenv("EXCHANGES", "okx,bitget,mexc").split(",") if e.strip()]
TRADE_USD = float(os.getenv("TRADE_USD", "300"))
MIN_NET_PCT = float(os.getenv("MIN_NET_PCT", "0.5"))
MAX_VOL_PCT = float(os.getenv("MAX_VOL_PCT", "0.15"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.001"))
SCAN_SECONDS = int(os.getenv("SCAN_SECONDS", "30"))
CONFIRM_SCANS = 2          # сколько сканов подряд спред должен держаться
COOLDOWN_SECONDS = 600     # не повторять одну и ту же связку чаще, чем раз в 10 минут

# Монеты, у которых быстрые и дешёвые сети. Комиссия вывода в МОНЕТАХ — ПРИМЕРНАЯ,
# проверь на своих биржах и поправь. Если комиссия выше — связка станет невыгодной.
WITHDRAW_FEE_COIN = {
    "XRP": 0.25,
    "LTC": 0.001,
    "TRX": 1.0,
    "DOGE": 4.0,
    "XLM": 0.02,
    "TON": 0.05,
    "ADA": 1.0,
    "SOL": 0.01,
}
SYMBOLS = [f"{c}/USDT" for c in WITHDRAW_FEE_COIN]

subscribers = {int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x.strip()}
streak = {}        # (symbol, buy_ex, sell_ex) -> сколько сканов подряд подходит
last_sent = {}     # (symbol, buy_ex, sell_ex) -> время последней отправки
last_top = []      # последний результат для /top

bot = Bot(BOT_TOKEN)
dp = Dispatcher()


# ---------- биржевые данные ----------
async def get_tickers(ex):
    try:
        data = await ex.fetch_tickers(SYMBOLS)
    except Exception as e:
        log.warning("%s: tickers error: %s", ex.id, e)
        return {}
    out = {}
    for s, t in data.items():
        if s in SYMBOLS and t.get("bid") and t.get("ask"):
            out[s] = (t["bid"], t["ask"])
    return out


def buy_vwap(asks, usd):
    """Тратим usd на покупку по стакану asks. Возвращает (средняя цена, количество монет)."""
    spent, coins = 0.0, 0.0
    for price, amount in asks:
        level_usd = price * amount
        take = min(level_usd, usd - spent)
        spent += take
        coins += take / price
        if spent >= usd - 1e-9:
            return spent / coins, coins
    return None, 0.0


def sell_vwap(bids, coins):
    """Продаём coins по стакану bids. Возвращает среднюю цену или None, если не хватает глубины."""
    left, got = coins, 0.0
    for price, amount in bids:
        take = min(amount, left)
        got += take * price
        left -= take
        if left <= 1e-12:
            return got / coins
    return None


async def volatility_pct(ex, symbol):
    """Стд. отклонение 1-минутной доходности за ~30 минут, в процентах."""
    try:
        candles = await ex.fetch_ohlcv(symbol, "1m", limit=31)
        closes = [c[4] for c in candles if c[4]]
        rets = [(closes[i] / closes[i - 1] - 1) * 100 for i in range(1, len(closes))]
        return statistics.pstdev(rets) if len(rets) >= 10 else None
    except Exception as e:
        log.warning("%s %s: ohlcv error: %s", ex.id, symbol, e)
        return None


# ---------- сканирование ----------
async def scan(exs):
    names = list(exs)
    results = await asyncio.gather(*(get_tickers(exs[n]) for n in names))
    tickers = dict(zip(names, results))

    candidates = []
    for sym in SYMBOLS:
        coin = sym.split("/")[0]
        for a in names:
            for b in names:
                if a == b or sym not in tickers[a] or sym not in tickers[b]:
                    continue
                ask_a = tickers[a][sym][1]
                bid_b = tickers[b][sym][0]
                gross = (bid_b / ask_a - 1) * 100
                fee_w_pct = WITHDRAW_FEE_COIN[coin] * ask_a / TRADE_USD * 100
                pre_net = gross - 2 * TAKER_FEE * 100 - fee_w_pct
                if pre_net >= MIN_NET_PCT * 0.6:   # грубый предфильтр, потом проверяем стаканом
                    candidates.append((pre_net, sym, a, b))

    candidates.sort(reverse=True)
    verified = []
    for _, sym, a, b in candidates[:8]:
        coin = sym.split("/")[0]
        try:
            ob_a, ob_b = await asyncio.gather(
                exs[a].fetch_order_book(sym, 30), exs[b].fetch_order_book(sym, 30)
            )
        except Exception as e:
            log.warning("orderbook error %s %s %s: %s", sym, a, b, e)
            continue
        buy_price, coins = buy_vwap(ob_a["asks"], TRADE_USD)
        if not buy_price:
            continue
        coins_after_withdraw = coins - WITHDRAW_FEE_COIN[coin]
        if coins_after_withdraw <= 0:
            continue
        sell_price = sell_vwap(ob_b["bids"], coins_after_withdraw)
        if not sell_price:
            continue
        spent = TRADE_USD * (1 + TAKER_FEE)
        received = coins_after_withdraw * sell_price * (1 - TAKER_FEE)
        net_pct = (received / spent - 1) * 100
        if net_pct < MIN_NET_PCT:
            continue
        vol = await volatility_pct(exs[a], sym)
        if vol is None or vol > MAX_VOL_PCT:
            continue
        verified.append({
            "symbol": sym, "buy": a, "sell": b, "net": net_pct, "vol": vol,
            "buy_price": buy_price, "sell_price": sell_price,
            "profit_usd": received - spent,
            "score": net_pct / max(vol, 0.02),
        })
    verified.sort(key=lambda x: x["score"], reverse=True)
    return verified


def fmt(r):
    return (
        f"🔁 {r['symbol']}\n"
        f"Купить: {r['buy']} ~{r['buy_price']:.6g}\n"
        f"Продать: {r['sell']} ~{r['sell_price']:.6g}\n"
        f"Чистый спред: {r['net']:.2f}% (~{r['profit_usd']:.2f}$ на {TRADE_USD:.0f}$)\n"
        f"Волатильность (1м, 30м): {r['vol']:.3f}%\n"
        f"Учтены: комиссии торговли, вывод, глубина стакана.\n"
        f"⚠️ Перед сделкой проверь сеть, мин. вывод и доступность вывода/ввода монеты на обеих биржах."
    )


async def scanner_loop():
    exs = {}
    for n in EXCHANGES:
        try:
            exs[n] = getattr(ccxt, n)({"enableRateLimit": True})
        except AttributeError:
            log.error("биржа %s не поддерживается ccxt", n)
    if len(exs) < 2:
        log.error("нужно минимум 2 биржи")
        return
    while True:
        try:
            global last_top
            found = await scan(exs)
            last_top = found
            now = time.time()
            active = set()
            for r in found:
                key = (r["symbol"], r["buy"], r["sell"])
                active.add(key)
                streak[key] = streak.get(key, 0) + 1
                if streak[key] >= CONFIRM_SCANS and now - last_sent.get(key, 0) > COOLDOWN_SECONDS:
                    last_sent[key] = now
                    for chat in list(subscribers):
                        try:
                            await bot.send_message(chat, fmt(r))
                        except Exception as e:
                            log.warning("send error %s: %s", chat, e)
            for k in list(streak):
                if k not in active:
                    del streak[k]
        except Exception as e:
            log.exception("scan error: %s", e)
        await asyncio.sleep(SCAN_SECONDS)


# ---------- команды бота ----------
@dp.message(Command("start"))
async def cmd_start(m: Message):
    subscribers.add(m.chat.id)
    await m.answer(
        "Подписал на сигналы. Команды:\n/top — лучшие связки сейчас\n/stop — отписаться\n"
        f"Порог: чистый спред ≥ {MIN_NET_PCT}%, волатильность ≤ {MAX_VOL_PCT}%, сумма {TRADE_USD:.0f}$."
    )


@dp.message(Command("stop"))
async def cmd_stop(m: Message):
    subscribers.discard(m.chat.id)
    await m.answer("Отписал.")


@dp.message(Command("top"))
async def cmd_top(m: Message):
    if not last_top:
        await m.answer("Сейчас подходящих связок нет.")
        return
    await m.answer("\n\n".join(fmt(r) for r in last_top[:3]))


async def main():
    asyncio.create_task(scanner_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())

"""Одноразовый запуск сканера (для GitHub Actions): сканирует, шлёт сигналы, выходит."""
import asyncio
import json
import os
import time

import ccxt.async_support as ccxt

import bot as core

STATE = "state.json"
COOLDOWN = 1800  # не повторять одну связку чаще, чем раз в 30 минут


async def main():
    chats = [int(x) for x in os.getenv("CHAT_IDS", "").split(",") if x.strip()]
    if not chats:
        raise SystemExit("CHAT_IDS не задан")
    try:
        with open(STATE) as f:
            state = json.load(f)
    except Exception:
        state = {}
    now = time.time()
    state = {k: v for k, v in state.items() if now - v < COOLDOWN}

    exs = {n: getattr(ccxt, n)({"enableRateLimit": True}) for n in core.EXCHANGES}
    try:
        found = await core.scan(exs)
    finally:
        for e in exs.values():
            await e.close()

    sent = 0
    for r in found[:3]:
        key = f"{r['symbol']}|{r['buy']}|{r['sell']}"
        if key in state:
            continue
        for c in chats:
            await core.bot.send_message(c, core.fmt(r))
        state[key] = now
        sent += 1

    with open(STATE, "w") as f:
        json.dump(state, f)
    await core.bot.session.close()
    print(f"found={len(found)} sent={sent}")


if __name__ == "__main__":
    asyncio.run(main())

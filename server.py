#!/usr/bin/env python3
"""《我是庄家》— 零依赖实时股票博弈原型服务器。"""
from __future__ import annotations

import json
import random
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).parent
STARTING_CASH = 1_000_000.0
STARTING_SHARES = 10_000
TICK = 0.01
LOWER_LIMIT, UPPER_LIMIT = 9.00, 11.00


@dataclass
class Account:
    id: str
    name: str
    cash: float = STARTING_CASH
    shares: int = STARTING_SHARES
    starting_equity: float = STARTING_CASH + STARTING_SHARES * 10
    frozen_cash: float = 0.0
    frozen_shares: int = 0
    is_bot: bool = False
    unlimited_funds: bool = False
    avg_cost: float = 10.0


@dataclass
class Order:
    id: str
    owner: str
    side: str
    price: float
    qty: int
    remaining: int
    created: float


class Market:
    def __init__(self):
        self.lock = threading.RLock()
        self.accounts: dict[str, Account] = {}
        self.orders: dict[str, Order] = {}
        self.trades = deque(maxlen=80)
        self.last_price = 10.00
        self.open_price = 10.00
        self.high = 10.00
        self.low = 10.00
        self.volume = 0
        self.candles = deque(maxlen=80)
        self.current_bucket = int(time.time() // 60)
        self.candles.append({"time": self.current_bucket * 60, "open": 10, "high": 10, "low": 10, "close": 10, "volume": 0})
        self.missions = [
            ("控盘稳定", "让价格在 9.85–10.15 区间内维持成交", "稳定指数"),
            ("吸筹挑战", "在不冲破涨停的前提下扩大持仓", "持仓进度"),
            ("活跃主力", "促成市场成交并保持正收益", "成交贡献"),
        ]
        self.bot_ids = []
        self._seed_bots()

    def _seed_bots(self):
        styles = ["北极做市", "南风做市", "追涨客", "抄底王", "量化均值", "趋势猎手", "短线游资", "稳健基金", "消息灵通", "耐心大户"]
        for n, style in enumerate(styles):
            # 机器人承担长期流动性，给予足够库存，避免连续市场单边耗尽。
            bot = Account(f"bot-{n}", style, STARTING_CASH * (1.2 + n * .08), STARTING_SHARES * 10, is_bot=True)
            self.accounts[bot.id] = bot
            self.bot_ids.append(bot.id)
        # 初始双边流动性，所有后续成交均经同一撮合引擎。
        for i in range(5):
            self._place_bot_order(self.bot_ids[i], "buy", round(9.99 - i * .02, 2), 500)
            self._place_bot_order(self.bot_ids[i + 5], "sell", round(10.01 + i * .02, 2), 500)

    def join(self, name: str):
        clean = "".join(c for c in name.strip()[:14] if c.isalnum() or '\u4e00' <= c <= '\u9fff' or c in "_- ") or "匿名投资者"
        with self.lock:
            ident = uuid.uuid4().hex[:12]
            # 当前公开市场的首位真人是“庄家”，拥有无限虚拟资金；其他玩家保持普通起始资金。
            founder = not any(not account.is_bot for account in self.accounts.values())
            cash = STARTING_CASH
            self.accounts[ident] = Account(ident, clean, cash=cash, starting_equity=cash + STARTING_SHARES * self.last_price, unlimited_funds=founder, avg_cost=self.last_price)
            mission = random.choice(self.missions)
            return {"player_id": ident, "founder": founder, "mission": {"title": mission[0], "text": mission[1], "metric": mission[2]}}

    def _book(self, side: str):
        data = [o for o in self.orders.values() if o.side == side and o.remaining]
        return sorted(data, key=lambda o: ((-o.price if side == "buy" else o.price), o.created))

    def _remove_order(self, order: Order):
        """撤掉订单并完整释放其尚未成交的冻结资产。"""
        account = self.accounts[order.owner]
        if order.side == "buy" and not account.unlimited_funds:
            account.frozen_cash -= order.price * order.remaining
        else:
            account.frozen_shares -= order.remaining
        order.remaining = 0
        self.orders.pop(order.id, None)

    def _reconcile_crossed_book(self):
        """订单簿不变量：最佳买价必须严格小于最佳卖价。"""
        while True:
            bids, asks = self._book("buy"), self._book("sell")
            if not bids or not asks or bids[0].price < asks[0].price:
                return
            bid, ask = bids[0], asks[0]
            if bid.owner == ask.owner:
                # 自成交保护：撤销较新的那张单，保留先挂出的流动性。
                self._remove_order(bid if bid.created > ask.created else ask)
            else:
                self._match(bid)

    def _place_bot_order(self, owner, side, price, qty):
        account = self.accounts[owner]
        if side == "buy":
            if account.cash - account.frozen_cash < price * qty: return
            account.frozen_cash += price * qty
        else:
            if account.shares - account.frozen_shares < qty: return
            account.frozen_shares += qty
        order = Order(uuid.uuid4().hex[:10], owner, side, price, qty, qty, time.time())
        self.orders[order.id] = order
        self._match(order)
        self._reconcile_crossed_book()

    def place(self, owner: str, side: str, price: float, qty: int):
        with self.lock:
            if owner not in self.accounts: raise ValueError("登录已失效，请重新进入市场")
            if side not in ("buy", "sell"): raise ValueError("交易方向无效")
            if not LOWER_LIMIT <= price <= UPPER_LIMIT: raise ValueError("价格超出当日涨跌停范围")
            if qty < 100 or qty % 100: raise ValueError("数量必须为 100 股的整数倍")
            account = self.accounts[owner]
            if side == "buy":
                required = price * qty
                if not account.unlimited_funds:
                    if account.cash - account.frozen_cash + 1e-7 < required: raise ValueError("可用资金不足")
                    account.frozen_cash += required
            else:
                if account.shares - account.frozen_shares < qty: raise ValueError("可用持仓不足")
                account.frozen_shares += qty
            order = Order(uuid.uuid4().hex[:10], owner, side, round(price, 2), qty, qty, time.time())
            self.orders[order.id] = order
            self._match(order)
            self._reconcile_crossed_book()
            return order.id

    def _match(self, taker: Order):
        opposite = "sell" if taker.side == "buy" else "buy"
        for maker in self._book(opposite):
            if not taker.remaining: break
            if (taker.side == "buy" and taker.price < maker.price) or (taker.side == "sell" and taker.price > maker.price): break
            # 同一账户不能用自己的挂单制造虚假成交；撤掉新单剩余部分，盘口不会交叉。
            if maker.owner == taker.owner:
                self._remove_order(taker)
                break
            qty = min(taker.remaining, maker.remaining)
            price = maker.price
            buyer = self.accounts[taker.owner if taker.side == "buy" else maker.owner]
            seller = self.accounts[taker.owner if taker.side == "sell" else maker.owner]
            if not buyer.unlimited_funds and taker.side == "buy":
                buyer.frozen_cash -= taker.price * qty
            # Release the buyer's reserved limit amount and debit actual price.
            if buyer.unlimited_funds:
                pass
            elif taker.side == "buy":
                buyer.cash -= price * qty
            else:
                buyer.cash -= price * qty
                buyer.frozen_cash -= price * qty
            # 持仓成本只由实际成交更新，采用加权平均法。
            previous_shares = buyer.shares
            buyer.shares += qty
            buyer.avg_cost = ((previous_shares * buyer.avg_cost) + (price * qty)) / buyer.shares
            seller.shares -= qty
            seller.frozen_shares -= qty
            seller.cash += price * qty
            taker.remaining -= qty
            maker.remaining -= qty
            self.last_price = price
            self.high, self.low, self.volume = max(self.high, price), min(self.low, price), self.volume + qty
            now = datetime.now().strftime("%H:%M:%S")
            self.trades.appendleft({"time": now, "price": price, "qty": qty, "side": taker.side})
            self._update_candle(price, qty)
            if not maker.remaining: self.orders.pop(maker.id, None)
        if not taker.remaining: self.orders.pop(taker.id, None)

    def cancel(self, owner: str, order_id: str):
        with self.lock:
            order = self.orders.get(order_id)
            if not order or order.owner != owner: raise ValueError("找不到可撤委托")
            self._remove_order(order)

    def cancel_all(self, owner: str):
        with self.lock:
            if owner not in self.accounts: raise ValueError("登录已失效，请重新进入市场")
            orders = [order for order in self.orders.values() if order.owner == owner]
            for order in orders:
                self._remove_order(order)
            return len(orders)

    def _update_candle(self, price, qty):
        bucket = int(time.time() // 60)
        if bucket != self.current_bucket:
            self.current_bucket = bucket
            self.candles.append({"time": bucket * 60, "open": self.last_price, "high": self.last_price, "low": self.last_price, "close": self.last_price, "volume": 0})
        c = self.candles[-1]
        c["high"], c["low"], c["close"], c["volume"] = max(c["high"], price), min(c["low"], price), price, c["volume"] + qty

    def snapshot(self, player_id: str | None):
        with self.lock:
            bids, asks = self._book("buy"), self._book("sell")
            def levels(items):
                out = []
                for o in items:
                    if out and out[-1]["price"] == o.price: out[-1]["qty"] += o.remaining
                    elif len(out) < 5: out.append({"price": o.price, "qty": o.remaining})
                return out
            player = self.accounts.get(player_id or "")
            pending = [asdict(o) for o in self.orders.values() if player and o.owner == player.id]
            equity = None if player and player.unlimited_funds else (player.cash + player.shares * self.last_price) if player else 0
            return {"symbol":"ZJ001", "name":"庄家控盘", "last":self.last_price, "change":round((self.last_price / 10 - 1) * 100, 2), "open":self.open_price, "high":self.high, "low":self.low, "volume":self.volume, "limits":[LOWER_LIMIT, UPPER_LIMIT], "bids":levels(bids), "asks":levels(asks), "trades":list(self.trades), "candles":list(self.candles), "account": asdict(player) if player else None, "equity":round(equity,2) if equity is not None else None, "pending":sorted(pending, key=lambda o:o["created"], reverse=True), "online":len([x for x in self.accounts.values() if not x.is_bot])}

    def robots(self):
        with self.lock:
            self._reconcile_crossed_book()
            # 机器人是市场流动性提供者：成交后及时补回模拟库存，避免被一张大单耗尽后停市。
            for bot_id in self.bot_ids:
                account = self.accounts[bot_id]
                if account.cash - account.frozen_cash < 100_000:
                    account.cash += STARTING_CASH
                if account.shares - account.frozen_shares < 5_000:
                    account.shares += STARTING_SHARES * 10
            # Cancel stale bot orders to keep spreads alive and prevent book accumulation.
            for oid, order in list(self.orders.items()):
                if order.owner.startswith("bot-") and time.time() - order.created > 15:
                    acct = self.accounts[order.owner]
                    if order.side == "buy": acct.frozen_cash -= order.price * order.remaining
                    else: acct.frozen_shares -= order.remaining
                    self.orders.pop(oid)
            # 涨跌停时不让机器人在同一极限价持续堆出会立刻吃掉卖盘/买盘的订单。
            for oid, order in list(self.orders.items()):
                if not order.owner.startswith("bot-"): continue
                locked_buy = self.last_price >= UPPER_LIMIT and order.side == "buy" and order.price >= UPPER_LIMIT
                locked_sell = self.last_price <= LOWER_LIMIT and order.side == "sell" and order.price <= LOWER_LIMIT
                if locked_buy or locked_sell:
                    acct = self.accounts[order.owner]
                    if order.side == "buy": acct.frozen_cash -= order.price * order.remaining
                    else: acct.frozen_shares -= order.remaining
                    self.orders.pop(oid)
            for i, bot_id in enumerate(self.bot_ids):
                if random.random() > .82: continue
                bias = (-1 if i in (3,4,7) else 1 if i in (2,5,6) else 0)
                price = round(max(LOWER_LIMIT, min(UPPER_LIMIT, self.last_price + (random.choice([-2,-1,1,2]) + bias) * TICK)), 2)
                # 接近极端价格时由均值回归机器人主动向反方向成交，防止市场长期钉死涨停/跌停。
                if self.last_price >= 10.40:
                    side = "sell"
                    price = round(max(LOWER_LIMIT, self.last_price - random.choice([.01, .02, .03])), 2)
                elif self.last_price <= 9.60:
                    side = "buy"
                    price = round(min(UPPER_LIMIT, self.last_price + random.choice([.01, .02, .03])), 2)
                elif price >= UPPER_LIMIT:
                    side = "sell"
                elif price <= LOWER_LIMIT:
                    side = "buy"
                else:
                    side = "buy" if (price <= self.last_price or random.random() < .5) else "sell"
                # 不让机器人卖单直接耗尽玩家的大额买墙；卖盘始终留在最佳买价上方，
                # 其余机器人仍可用主动买入来形成真实成交。
                if side == "sell":
                    bids = self._book("buy")
                    if bids:
                        price = max(price, round(bids[0].price + TICK, 2))
                    price = min(UPPER_LIMIT, price)
                self._place_bot_order(bot_id, side, price, random.choice([100,200,300,500]))
            # 无论行情涨跌，保留至少五档可见的机器人流动性。
            active_bids = {o.price for o in self._book("buy")}
            active_asks = {o.price for o in self._book("sell")}
            for i in range(5):
                bid = round(max(LOWER_LIMIT, self.last_price - (i + 1) * TICK), 2)
                ask = round(min(UPPER_LIMIT, self.last_price + (i + 1) * TICK), 2)
                if bid not in active_bids:
                    self._place_bot_order(self.bot_ids[i], "buy", bid, 500)
                    active_bids.add(bid)
                if ask not in active_asks:
                    self._place_bot_order(self.bot_ids[i + 5], "sell", ask, 500)
                    active_asks.add(ask)
            # 定时由另一位机器人主动吃掉一档卖盘，所有 K 线变化仍来自真实订单撮合。
            if random.random() < .45:
                asks = self._book("sell")
                if asks:
                    maker = asks[0]
                    buyer = next((bot_id for bot_id in self.bot_ids if bot_id != maker.owner), None)
                    if buyer:
                        self._place_bot_order(buyer, "buy", maker.price, random.choice([100, 200, 300]))
            self._reconcile_crossed_book()


MARKET = Market()

def robot_loop():
    while True:
        time.sleep(.55)
        MARKET.robots()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {fmt % args}")

    def end_headers(self):
        # 开发阶段每次打开手机页面都获取最新界面，避免 Safari 缓存旧脚本。
        if self.path.endswith((".html", ".css", ".js")):
            self.send_header("Cache-Control", "no-store, max-age=0")
        super().end_headers()

    def _json(self, status, body):
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)

    def _body(self):
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/snapshot":
            return self._json(200, MARKET.snapshot(parse_qs(parsed.query).get("player_id", [None])[0]))
        if parsed.path == "/api/health": return self._json(200, {"ok":True, "market":"open"})
        if parsed.path == "/": self.path = "/index.html"
        return super().do_GET()

    def do_POST(self):
        try:
            data = self._body()
            if self.path == "/api/join": return self._json(200, MARKET.join(str(data.get("name", ""))))
            if self.path == "/api/order":
                oid = MARKET.place(data["player_id"], data["side"], float(data["price"]), int(data["qty"]))
                return self._json(200, {"ok":True,"order_id":oid})
            if self.path == "/api/cancel":
                MARKET.cancel(data["player_id"], data["order_id"]); return self._json(200, {"ok":True})
            if self.path == "/api/cancel-all":
                count = MARKET.cancel_all(data["player_id"]); return self._json(200, {"ok":True, "count":count})
            self._json(404, {"error":"not found"})
        except (ValueError, KeyError, TypeError) as e: self._json(400, {"error":str(e)})


if __name__ == "__main__":
    threading.Thread(target=robot_loop, daemon=True).start()
    print("我是庄家已启动：http://127.0.0.1:8088")
    ThreadingHTTPServer(("0.0.0.0", 8088), Handler).serve_forever()

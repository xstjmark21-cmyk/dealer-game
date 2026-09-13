import http.client
import json
import threading
import time
import unittest

import server


class MarketTests(unittest.TestCase):
    def setUp(self):
        self.market = server.Market()

    def test_founder_cancel_does_not_reduce_sellable_shares(self):
        player = self.market.join("庄家")["player_id"]
        self.market.place(player, "buy", 9.50, 100)
        self.assertEqual(self.market.accounts[player].frozen_shares, 0)
        self.market.cancel_all(player)
        self.assertEqual(self.market.accounts[player].frozen_shares, 0)

    def test_invalid_order_values_are_rejected(self):
        player = self.market.join("玩家")["player_id"]
        with self.assertRaisesRegex(ValueError, "0.01"):
            self.market.place(player, "buy", 10.001, 100)
        with self.assertRaisesRegex(ValueError, "不超过"):
            self.market.place(player, "buy", 10.00, server.MAX_ORDER_QTY + 100)

    def test_expired_session_cancels_pending_orders(self):
        player = self.market.join("玩家")["player_id"]
        self.market.place(player, "buy", 9.50, 100)
        self.market.accounts[player].last_seen -= server.SESSION_TTL_SECONDS + 1
        snapshot = self.market.snapshot(player)
        self.assertIsNone(snapshot["account"])
        self.assertFalse([order for order in self.market.orders.values() if order.owner == player])


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_market = server.MARKET
        server.MARKET = server.Market()
        server.Handler.rate_buckets = {}
        cls.httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join(timeout=2)
        cls.httpd.server_close()
        server.MARKET = cls.original_market

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        raw = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        request_headers = headers or {}
        if raw is not None: request_headers.setdefault("Content-Type", "application/json")
        conn.request(method, path, raw, request_headers)
        response = conn.getresponse()
        payload = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, payload

    def test_join_and_snapshot_use_opaque_session(self):
        status, joined = self.request("POST", "/api/join", {"name": "测试"})
        self.assertEqual(status, 200)
        self.assertRegex(joined["player_id"], r"^[0-9a-f]{32}$")
        status, snapshot = self.request("GET", f"/api/snapshot?player_id={joined['player_id']}")
        self.assertEqual(status, 200)
        self.assertEqual(snapshot["account"]["id"], joined["player_id"])

    def test_rejects_malformed_and_oversized_json(self):
        status, _ = self.request("POST", "/api/join", b"{")
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/join", {"name": "x" * (server.MAX_BODY_BYTES + 20)})
        self.assertEqual(status, 413)

    def test_rejects_bot_identifier_and_bad_order_fields(self):
        status, _ = self.request("POST", "/api/order", {"player_id": "bot-0", "side": "buy", "price": 10, "qty": 100})
        self.assertEqual(status, 400)
        status, _ = self.request("POST", "/api/order", {"player_id": "not-a-session", "side": "buy", "price": "NaN", "qty": 100})
        self.assertEqual(status, 400)

    def test_post_rate_limit_returns_429(self):
        original_limits = server.Handler.rate_limits
        try:
            server.Handler.rate_limits = {"snapshot": (180, 30), "post": (1, 30)}
            server.Handler.rate_buckets = {}
            self.assertEqual(self.request("POST", "/api/join", {"name": "限流甲"})[0], 200)
            self.assertEqual(self.request("POST", "/api/join", {"name": "限流乙"})[0], 429)
        finally:
            server.Handler.rate_limits = original_limits
            server.Handler.rate_buckets = {}


if __name__ == "__main__":
    unittest.main()

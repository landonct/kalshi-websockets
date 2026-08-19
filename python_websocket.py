import asyncio
import websockets
import json
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from dotenv import load_dotenv
load_dotenv(".env")
KALSHI_ACCESS_KEY = os.getenv("KALSHI_ACCESS_KEY")
PRIVATE_KEY_PATH = os.getenv("PRIVATE_KEY_PATH")
PRIVATE_KEY = functions.load_private_key(PRIVATE_KEY_PATH)
WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
AUTH_HEADERS = {
    "KALSHI-ACCESS-KEY": "your_api_key_id",
    "KALSHI-ACCESS-SIGNATURE": "generated_signature",
    "KALSHI-ACCESS-TIMESTAMP": "timestamp_in_milliseconds",
}


class Side(Enum):
    Ask = 1
    Bid = 2


@dataclass
class Trade:
    price: float
    volume: int
    taker_side: Side

    def __init__(self, price: float, volume: int, taker_side: Side) -> None:
        self.price = price
        self.volume = volume
        self.taker_side = taker_side

    def __str__(self) -> str:
        side_str = "ask" if self.taker_side == 1 else "bid"
        return f"{self.price} cent {side_str}, {self.volume} contracts"

    def __repr__(self) -> str:
        return (
            f"price: {self.price}, volume: {self.volume}, taker_side: {self.taker_side}"
        )


@dataclass
class Row:
    def __init__(
        self,
        market_ticker: str,
        yes_price: float,
        volume: int,
        taker_side: Side,
        created_time: datetime,
    ) -> None:
        self.market_ticker = market_ticker
        self.yes_price = yes_price
        self.volume = volume
        self.taker_side = taker_side
        self.created_time = created_time

    def row2trade(self) -> Trade:
        return Trade(self.yes_price, self.volume, self.taker_side)


@dataclass
class TradeQueue:
    def __init__(self, max_size=10) -> None:
        self._trade_queue: deque[Trade] = deque(maxlen=max_size)
        self._total_price: float = 0.0
        self._count: int = 0
        self._average: float = (
            self._total_price / self._count if self._count != 0 else 0
        )
        self._max_size = max_size

    def push(self, trade: Trade) -> None:
        self._trade_queue.append(trade)
        self._total_price = sum(trade.price for trade in self._trade_queue)
        self._count = len(self._trade_queue)
        self._average = self._total_price / self._count

    def get_average(self) -> float:
        return self._average


async def connect() -> None:
    async with websockets.connect(WS_URL, additional_headers=AUTH_HEADERS) as websocket:
        print("Connected to Kalshi websocket server")

        async for message in websocket:
            print(f"Recieved: {message}")


async def subscribe_to_ticker(websocket) -> None:
    subscription = {"id": 1, "cmd": "subscribe", "params": {"channels": ["ticker"]}}
    await websocket.send(json.dumps(subscription))


async def subscribe_to_orderbook(websocket, market_tickers) -> None:
    subscription = {
        "id": 2,
        "cmd": "subscribe",
        "params": {"channels": ["orderbook_delta"], "market_tickers": market_tickers},
    }
    await websocket.send(json.dumps(subscription))


async def process_message(message):
    """Process incoming WebSocket messages"""
    data = json.loads(message)
    msg_type = data.get("type")

    if msg_type == "ticker":
        # Handle ticker update
        market = data["msg"]["market_ticker"]
        bid = data["msg"]["yes_bid_dollars"]
        ask = data["msg"]["yes_ask_dollars"]
        print(f"{market}: Yes Bid ${bid}, Yes Ask ${ask}")

    elif msg_type == "orderbook_snapshot":
        # Handle full orderbook state
        print(f"Orderbook snapshot for {data['msg']['market_ticker']}")

    elif msg_type == "orderbook_delta":
        # Handle orderbook changes
        print(f"Orderbook update for {data['msg']['market_ticker']}")
        # Note: client_order_id field is optional - present only when you caused this change
        if "client_order_id" in data["msg"]:
            print(f"  Your order {data['msg']['client_order_id']} caused this change")

    elif msg_type == "error":
        error_code = data.get("msg", {}).get("code")
        error_msg = data.get("msg", {}).get("msg")
        print(f"Error {error_code}: {error_msg}")


def main():
    trade = Trade(0.99, 10, Side.Bid)
    print(trade)

    rolling_window = TradeQueue(10)

    subscribe_to_orderbook()


if __name__ == "__main__":
    main()

from dataclasses import dataclass

import numpy as np


class NonEmptyBook(Exception):
    pass

class InvalidMarket(Exception):
    pass



class OrderBook:
    """Maintains a local Kalshi order book from snapshot + delta messages.

    Kalshi expresses the book as two sets of *bids*:
      - "yes" side: resting orders to BUY Yes at price p            -> a Yes bid at p
      - "no"  side: resting orders to BUY No  at price q, which is
                    equivalent to an offer to SELL Yes at (1 - q)   -> a Yes ask

    So to view the market in normal bid/ask terms (denominated in Yes):
        best_yes_bid = max(yes prices)
        best_yes_ask = 1 - max(no prices)

    Prices are kept as strings (exact, no float rounding) keyed to a running
    quantity. delta_fp is the *change* in resting size at that price level.
    """

    def __init__(self, market_ticker: str) -> None:
        self.market_ticker = market_ticker
        self.yes: dict[str, float] = {}  # price_str -> qty
        self.no: dict[str, float] = {}
        self.ready = False  # True once the snapshot has been applied

    @staticmethod
    def _apply_levels(book: dict[str, float], levels: list[list[str]] | None) -> None:
        """Overwrite `book` from a snapshot's list of [price, qty] pairs."""
        book.clear()
        if levels is None:
            return

        for level in levels:
            price, qty = level[0], float(level[1])
            book[str(price)] = qty

    def apply_snapshot(self, msg: dict) -> None:
        # snapshot keys *_dollars_fp
        self._apply_levels(self.yes, msg.get("yes_dollars_fp"))
        self._apply_levels(self.no, msg.get("no_dollars_fp"))
        self.ready = True

    def apply_delta(self, msg: dict) -> None:
        side = msg["side"]
        price = str(msg.get("price_dollars", msg.get("price")))
        delta = float(msg.get("delta_fp", msg.get("delta", 0)))
        book = self.yes if side == "yes" else self.no
        new_qty = book.get(price, 0.0) + delta
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

    # --- derived quantities, all denominated in Yes ---------------------

    def best_yes_bid(self):
        """(price, qty) of the highest Yes bid, or None."""
        if not self.yes:
            return None
        yes_bids = {k: v for k, v in self.yes.items() if v >= 1}
        if not yes_bids:
            yes_bids = {"empty": -np.inf}
        p = max(yes_bids, key=float)
        return float(p), self.yes[p]

    def best_yes_ask(self):
        """(price, qty) of the lowest Yes ask, derived from the No book."""
        if not self.no:
            return None
        # Best No bid = highest No price -> tightest Yes ask = 1 - that price.
        yes_ask = {k: v for k, v in self.no.items() if v >= 1}
        if not yes_ask:
            yes_ask = {"empty": -np.inf}
        p = max(yes_ask, key=float)
        return round(1.0 - float(p), 4), self.no[p]

    def book_imbalance(self) -> float | None:
        """Static top-of-book imbalance in [-1, 1]: +1 = all bid, -1 = all ask."""
        bid, ask = self.best_yes_bid(), self.best_yes_ask()
        if not bid or not ask:
            return None
        qb, qa = bid[1], ask[1]
        total = qb + qa
        return (qb - qa) / total if total else None


class OFITracker:
    """Order-flow imbalance (Cont-Kukanov-Stoikov), event-by-event.

    Compares best bid/ask price & size between consecutive book states and
    accumulates the signed contribution of each event. Reset the window when
    you want a fresh reading (e.g. per second / per N events).
    """

    def __init__(self) -> None:
        self._prev = None  # (bid_px, bid_qty, ask_px, ask_qty)
        self.ofi = 0.0

    def update(self, book: "OrderBook") -> None:
        bid, ask = book.best_yes_bid(), book.best_yes_ask()
        if not bid or not ask:
            return
        cur = (bid[0], bid[1], ask[0], ask[1])
        if self._prev is not None:
            pbp, pbq, pap, paq = self._prev
            bp, bq, ap, aq = cur
            # Bid-side contribution
            if bp > pbp:
                self.ofi += bq
            elif bp == pbp:
                self.ofi += bq - pbq
            else:
                self.ofi -= pbq
            # Ask-side contribution (sign flipped)
            if ap < pap:
                self.ofi -= aq
            elif ap == pap:
                self.ofi -= aq - paq
            else:
                self.ofi += paq
        self._prev = cur

    def reset(self) -> float:
        val = self.ofi
        self.ofi = 0.0
        return val


@dataclass(frozen=True)
class Trade:
    price: int
    time_ms: int
    size: float
    taker_outcome_side: str
    taker_book_side: str

@dataclass
class MarketState:
    ticker: str
    book: OrderBook
    ofi: OFITracker
    stats: dict[str, float | None]
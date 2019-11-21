#!/usr/bin/env python
import asyncio
import logging
import time

import aiohttp
import pandas as pd
import hummingbot.market.hitbtc.hitbtc_constants as constants

from typing import Optional, List, Dict, Any
from hummingbot.core.data_type.order_book import OrderBook
from hummingbot.core.data_type.order_book_message import OrderBookMessage
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.order_book_tracker_entry import OrderBookTrackerEntry, HitbtcOrderBookTrackerEntry
from hummingbot.core.utils import async_ttl_cache
from hummingbot.core.utils.async_utils import safe_gather
from hummingbot.logger import HummingbotLogger
from hummingbot.market.hitbtc.hitbtc_active_order_tracker import HitbtcActiveOrderTracker
from hummingbot.market.hitbtc.hitbtc_order_book import HitbtcOrderBook
from hummingbot.market.hitbtc.hitbtc_websocket import HitbtcWebsocket


MAX_RETRIES = 20

class HitbtcAPIOrderBookDataSource(OrderBookTrackerDataSource):

    MESSAGE_TIMEOUT = 30.0
    SNAPSHOT_TIMEOUT = 10.0

    _hbaot_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._hbaot_logger is None:
            cls._hbaot_logger = logging.getLogger(__name__)
        return cls._hbaot_logger

    def __init__(self, symbols: Optional[List[str]] = None):
        super().__init__()
        self._symbols: Optional[List[str]] = symbols
        self._snapshot_msg: Dict[str, any] = {}

    @classmethod
    @async_ttl_cache(ttl=60 * 30, maxsize=1)
    async def get_active_exchange_markets(cls) -> pd.DataFrame:
        """
        Returned data frame should have symbol as index and include USDVolume, baseAsset and quoteAsset
        """
        async with aiohttp.ClientSession() as client:

            markets_response, tickers_response = await safe_gather(
                client.get(constants.REST_SYMBOL_URL),
                client.get(constants.REST_TICKERS_URL)
            )

            markets_response: aiohttp.ClientResponse = markets_response
            tickers_response: aiohttp.ClientResponse = tickers_response

            if markets_response.status != 200:
                raise IOError(
                    f"Error fetching active {constants.EXCHANGE_NAME} markets information. " f"HTTP status is {markets_response.status}."
                )
            if tickers_response.status != 200:
                raise IOError(
                    f"Error fetching active {constants.EXCHANGE_NAME} tickers information. " f"HTTP status is {tickers_response.status}."
                )

            markets_data, tickers_data = await safe_gather(
                markets_response.json(), tickers_response.json()
            )

            markets_data: Dict[str, Any] = {item["id"]: item for item in markets_data}
            tickers_data: Dict[str, Any] = {item["symbol"]: item for item in tickers_data}

            def merge(source, destination):
                for key, value in source.items():
                    if isinstance(value, dict):
                        # get node or create one
                        node = destination.setdefault(key, {})
                        merge(value, node)
                    else:
                        destination[key] = value

                return destination

            data_union = merge(tickers_data, markets_data)

            all_markets: pd.DataFrame = pd.DataFrame.from_records(data=list(data_union.values()), index="symbol")
            all_markets.rename(
                {"baseCurrency": "baseAsset", "quoteCurrency": "quoteAsset"}, axis="columns", inplace=True
            )

            btc_usd_price: float = float(all_markets.loc["BTCUSD"]["last"])
            eth_usd_price: float = float(all_markets.loc["ETHUSD"]["last"])

            usd_volume: List[float] = [
                (
                    volume * last if symbol.endswith(("USD", "USDT")) else
                    volume * last * btc_usd_price if symbol.endswith("BTC") else
                    volume * last * eth_usd_price if symbol.endswith("ETH") else
                    volume
                )
                for symbol, volume, last in zip(all_markets.index,
                                                all_markets.volume.astype("float"),
                                                all_markets["last"].astype("float")
                                                )
            ]

            all_markets.loc[:, "USDVolume"] = usd_volume
            await client.close()

            return all_markets.sort_values("USDVolume", ascending=False)

    async def get_trading_pairs(self) -> List[str]:
        """
        Return list of trading pairs
        """
        if not self._symbols:
            try:
                active_markets: pd.DataFrame = await self.get_active_exchange_markets()
                self._symbols = active_markets.index.tolist()
            except Exception:
                self._symbols = []
                self.logger().network(
                    f"Error getting active exchange information.",
                    exc_info=True,
                    app_warning_msg=f"Error getting active exchange information. Check network connection.",
                )

        return self._symbols

    @staticmethod
    async def get_orderbook(trading_pair: str) -> Dict[str, any]:
        """
        Get whole orderbook
        """
        client = aiohttp.ClientSession()
        orderbook_response = await client.get(f"{constants.REST_ORDERBOOK_URL}/{trading_pair}", params={"limit": 0})

        if orderbook_response.status != 200:
            raise IOError(
                f"Error fetching OrderBook for {trading_pair} at {constants.EXCHANGE_NAME}. " f"HTTP status is {orderbook_response.status}."
            )

        orderbook_data: List[Dict[str, Any]] = await safe_gather(orderbook_response.json())

        await client.close()

        if len(orderbook_data) > 0:
            return orderbook_data[0]

        return {}

    async def get_tracking_pairs(self) -> Dict[str, OrderBookTrackerEntry]:
        trading_pairs: List[str] = await self.get_trading_pairs()
        tracking_pairs: Dict[str, OrderBookTrackerEntry] = {}

        number_of_pairs: int = len(trading_pairs)
        for index, trading_pair in enumerate(trading_pairs):
            try:
                snapshot: Dict[str, any] = await self.get_orderbook(trading_pair)
                snapshot_timestamp: float = pd.Timestamp(snapshot["timestamp"]).timestamp()
                snapshot_msg: OrderBookMessage = HitbtcOrderBook.snapshot_message_from_exchange(
                    snapshot,
                    snapshot_timestamp,
                    metadata={"trading_pair": trading_pair}
                )
                order_book: OrderBook = self.order_book_create_function()
                active_order_tracker: HitbtcActiveOrderTracker = HitbtcActiveOrderTracker()
                bids, asks = active_order_tracker.convert_snapshot_message_to_order_book_row(snapshot_msg)
                order_book.apply_snapshot(bids, asks, snapshot_msg.update_id)

                tracking_pairs[trading_pair] = HitbtcOrderBookTrackerEntry(
                    trading_pair,
                    snapshot_timestamp,
                    order_book,
                    active_order_tracker
                )
                self.logger().info(f"Initialized order book for {trading_pair}. "
                                   f"{index+1}/{number_of_pairs} completed.")
                await asyncio.sleep(0.6)
            except IOError:
                self.logger().network(
                    f"Error getting snapshot for {trading_pair}.",
                    exc_info=True,
                    app_warning_msg=f"Error getting snapshot for {trading_pair}. Check network connection."
                )
            except Exception:
                self.logger().error(f"Error initializing order book for {trading_pair}. ", exc_info=True)

            return tracking_pairs

    async def listen_for_trades(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        """
        Listen for trades using websocket "updateTrades" method
        """
        while True:
            try:
                ws = HitbtcWebsocket()
                trading_pairs: List[str] = await self.get_trading_pairs()

                for trading_pair in trading_pairs:
                    await ws.subscribe("subscribeTrades", {
                        "symbol": trading_pair,
                        "limit": 1  # we only care about updates, this sets the initial snapshot limit
                    })

                    async for msg in ws.on("updateTrades"):
                        trades = msg["data"]

                        for trade in trades:
                            trade_timestamp: float = pd.Timestamp(trade["timestamp"]).timestamp()
                            trade_msg: OrderBookMessage = HitbtcOrderBook.trade_message_from_exchange(trade, trade_timestamp, metadata={"trading_pair": trading_pair})
                            output.put_nowait(trade_msg)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error.", exc_info=True)
                await asyncio.sleep(5.0)

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        """
        Listen for orderbook diffs using websocket "updateOrderbook" method
        """
        while True:
            try:
                ws = HitbtcWebsocket()
                trading_pairs: List[str] = await self.get_trading_pairs()

                for trading_pair in trading_pairs:
                    await ws.subscribe("subscribeOrderbook", {
                        "symbol": trading_pair
                    })

                    async for msg in ws.on("updateOrderbook"):
                        orderbook_timestamp: float = pd.Timestamp(msg["timestamp"]).timestamp()
                        orderbook_msg: OrderBookMessage = HitbtcOrderBook.diff_message_from_exchange(msg, orderbook_timestamp)
                        output.put_nowait(orderbook_msg)

            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().network(
                    f"Unexpected error with WebSocket connection.",
                    exc_info=True,
                    app_warning_msg=f"Unexpected error with WebSocket connection. Retrying in 30 seconds. "
                                    f"Check network connection."
                )
                await asyncio.sleep(30.0)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        """
        Listen for orderbook snapshots by fetching orderbook
        """
        while True:
            try:
                trading_pairs: List[str] = await self.get_trading_pairs()

                for trading_pair in trading_pairs:
                    try:
                        snapshot: Dict[str, any] = await self.get_orderbook(trading_pair)
                        snapshot_timestamp: float = pd.Timestamp(snapshot["timestamp"]).timestamp()
                        snapshot_msg: OrderBookMessage = HitbtcOrderBook.snapshot_message_from_exchange(
                            snapshot,
                            snapshot_timestamp,
                            metadata={"trading_pair": trading_pair}
                        )
                        output.put_nowait(snapshot_msg)
                        self.logger().debug(f"Saved order book snapshot for {trading_pair}")
                        # Be careful not to go above API rate limits.
                        await asyncio.sleep(5.0)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        self.logger().network(
                            f"Unexpected error with WebSocket connection.",
                            exc_info=True,
                            app_warning_msg=f"Unexpected error with WebSocket connection. Retrying in 5 seconds. "
                                            f"Check network connection."
                        )
                        await asyncio.sleep(5.0)
                this_hour: pd.Timestamp = pd.Timestamp.utcnow().replace(minute=0, second=0, microsecond=0)
                next_hour: pd.Timestamp = this_hour + pd.Timedelta(hours=1)
                delta: float = next_hour.timestamp() - time.time()
                await asyncio.sleep(delta)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error.", exc_info=True)
                await asyncio.sleep(5.0)
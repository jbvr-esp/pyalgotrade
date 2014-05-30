# PyAlgoTrade
#
# Copyright 2011-2014 Gabriel Martin Becedillas Ruiz
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
.. moduleauthor:: Gabriel Martin Becedillas Ruiz <gabriel.becedillas@gmail.com>
"""

import time
import datetime
import threading
import Queue
import hmac
import hashlib
import urllib
import urllib2
import json

import wsclient
from pyalgotrade import observer
from pyalgotrade.utils import dt
import pyalgotrade.logger


logger = pyalgotrade.logger.getLogger("bitstamp")


class WSClient(wsclient.WebSocketClient):

    # Events
    ON_TRADE = 1
    ON_ORDER_BOOK_UPDATE = 2
    ON_CONNECTED = 3
    ON_DISCONNECTED = 4

    def __init__(self):
        wsclient.WebSocketClient.__init__(self)
        self.__queue = Queue.Queue()

    def getQueue(self):
        return self.__queue

    # WebSocketClientBase events.
    def onOpened(self):
        pass

    def onClosed(self, code, reason):
        logger.info("Closed. Code: %s. Reason: %s." % (code, reason))

    def onDisconnectionDetected(self):
        logger.info("Disconnection detected.")
        try:
            self.stopClient()
        except Exception, e:
            logger.error("Error stopping client: %s" % str(e))
        self.__queue.put((WSClient.ON_DISCONNECTED, None))

    # Pusher specific events.
    def onConnectionEstablished(self, event):
        logger.info("Connection established.")
        self.subscribeChannel("live_trades")
        self.subscribeChannel("order_book")
        self.__queue.put((WSClient.ON_CONNECTED, None))

    def onSubscriptionError(self, event):
        logger.error("Channel subscription error: %s" % (event))

    def onError(self, event):
        logger.error("Error: %s" % (event))

    def onUnknownEvent(self, event):
        logger.warning("Unknown event: %s" % (event))

    # Bitstamp specific
    def onTrade(self, trade):
        self.__queue.put((WSClient.ON_TRADE, trade))

    def onOrderBookUpdate(self, orderBookUpdate):
        self.__queue.put((WSClient.ON_ORDER_BOOK_UPDATE, orderBookUpdate))


class Client(observer.Subject):
    """This class is responsible for the interaction with Bitstamp. In order to get the client running
    it has to be included in the dispatch loop. Check the example code to get this done."""

    QUEUE_TIMEOUT = 0.01

    def __init__(self):
        self.__thread = None
        self.__initializationOk = None
        self.__wsClient = None
        self.__enableReconnection = False
        self.__stopped = False
        self.__tradeEvent = observer.Event()
        self.__orderBookUpdateEvent = observer.Event()

    def __threadMain(self):
        logger.debug("Thread started.")
        self.__wsClient.startClient()
        logger.debug("Thread finished.")

    def __initializeClient(self):
        self.__initializationOk = None
        logger.info("Initializing client.")

        try:
            # Try to connect
            self.__thread = None
            self.__wsClient = WSClient()
            self.__wsClient.connect()

            # Start the thread that runs the client.
            self.__thread = threading.Thread(target=self.__threadMain)
            self.__thread.start()
        except Exception, e:
            self.__initializationOk = False
            logger.error("Error connecting : %s" % str(e))

        # Wait for initialization to complete.
        while self.__initializationOk is None and self.__thread.is_alive():
            self.__dispatchImpl([WSClient.ON_CONNECTED])

        if self.__initializationOk:
            logger.info("Initialization ok.")
        else:
            logger.error("Initialization failed.")
        return self.__initializationOk

    def __onConnected(self):
        self.__initializationOk = True

    def __onDisconnected(self):
        if self.__enableReconnection:
            initialized = False
            while not self.__stopped and not initialized:
                logger.info("Reconnecting")
                initialized = self.__initializeClient()
                if not initialized:
                    time.sleep(5)
        else:
            self.__stopped = True

    def __dispatchImpl(self, eventFilter):
        ret = False
        try:
            eventType, eventData = self.__wsClient.getQueue().get(True, Client.QUEUE_TIMEOUT)
            if eventFilter is not None and eventType not in eventFilter:
                return False

            ret = True
            if eventType == WSClient.ON_TRADE:
                self.__tradeEvent.emit(eventData)
            elif eventType == WSClient.ON_ORDER_BOOK_UPDATE:
                self.__orderBookUpdateEvent.emit(eventData)
            elif eventType == WSClient.ON_CONNECTED:
                self.__onConnected()
            elif eventType == WSClient.ON_DISCONNECTED:
                self.__onDisconnected()
            else:
                ret = False
                logger.error("Invalid event received to dispatch: %s - %s" % (eventType, eventData))
        except Queue.Empty:
            pass
        return ret

    # This may raise.
    def start(self):
        if self.__thread is not None:
            raise Exception("Already running")
        elif not self.__initializeClient():
            self.__stopped = True
            raise Exception("Initialization failed")

    # This should not raise.
    def stop(self):
        try:
            self.__stopped = True
            if self.__thread is not None and self.__thread.is_alive():
                logger.info("Shutting down client.")
                self.__wsClient.stopClient()
        except Exception, e:
            logger.error("Error shutting down client: %s" % (str(e)))

    # This should not raise.
    def join(self):
        if self.__thread is not None:
            self.__thread.join()

    # Return True if there are not more events to dispatch.
    def eof(self):
        return self.__stopped

    # Dispatch events. If True is returned, it means that at least one event was dispatched.
    def dispatch(self):
        return self.__dispatchImpl(None)

    def peekDateTime(self):
        # Return None since this is a realtime subject.
        return None

    def getTradeEvent(self):
        """Returns the event that will be emitted as new trades are received from Bitstamp.
        To subscribe to this event you need to pass in a callable object that receives one parameter:

        1. A :class:`pyalgotrade.bitstamp.wsclient.Trade` instance.

        .. note::
            It is not necessary to manually subscribe to this event since trades are notified
            by the BarFeed.
        """
        return self.__tradeEvent

    def getOrderBookUpdateEvent(self):
        """Returns the event that will be emitted as new trades are received from Bitstamp.
        To subscribe to this event you need to pass in a callable object that receives one parameter:

        1. A :class:`pyalgotrade.bitstamp.wsclient.OrderBookUpdate` instance.
        """
        return self.__orderBookUpdateEvent

######################################################################

def parse_datetime(dateTime):
    try:
        ret = datetime.datetime.strptime(dateTime, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        ret = datetime.datetime.strptime(dateTime, "%Y-%m-%d %H:%M:%S.%f")
    return dt.as_utc(ret)

class AccountBalance(object):
    def __init__(self, jsonDict):
        self.__jsonDict = jsonDict

    def getUSDAvailable(self):
        return float(self.__jsonDict["usd_available"])

    def getBTCAvailable(self):
        return float(self.__jsonDict["btc_available"])


class Order(object):
    def __init__(self, jsonDict):
        self.__jsonDict = jsonDict

    def getDict(self):
        return self.__jsonDict

    def getId(self):
        return int(self.__jsonDict["id"])

    def isBuy(self):
        return self.__jsonDict["type"] == 0

    def isSell(self):
        return self.__jsonDict["type"] == 1

    def getPrice(self):
        return float(self.__jsonDict["price"])

    def getAmount(self):
        return float(self.__jsonDict["amount"])

    def getDateTime(self):
        return parse_datetime(self.__jsonDict["datetime"])


class HTTPClient(object):
    USER_AGENT = "PyAlgoTrade"

    def __init__(self, clientId, key, secret):
        self.__clientId = clientId
        self.__key = key
        self.__secret = secret
        self.__prevNonce = None

    def _getNonce(self):
        ret = int(time.time())
        if ret == self.__prevNonce:
            ret += 1
        self.__prevNonce = ret
        return ret

    def _buildQuery(self, params):
        # Build the signature.
        nonce = self._getNonce()
        message = "%d%s%s" % (nonce, self.__clientId, self.__key)
        signature = hmac.new(self.__secret, msg=message, digestmod=hashlib.sha256).hexdigest().upper()

        # Headers
        headers = {}
        headers["User-Agent"] = HTTPClient.USER_AGENT

        # POST data.
        data = {}
        data.update(params)
        data["key"] = self.__key
        data["signature"] = signature
        data["nonce"] = nonce

        post_data = urllib.urlencode(data)
        return (post_data, headers)

    def _post(self, url, params):
        data, headers = self._buildQuery(params)
        req = urllib2.Request(url, data, headers)
        response = urllib2.urlopen(req, data)
        json_response = json.loads(response.read())

        # Check for errors.
        if isinstance(json_response, dict):
            error = json_response.get("error")
            if error is not None:
                raise Exception(error)

        return json_response

    def getAccountBalance(self):
        url = "https://www.bitstamp.net/api/balance/"
        json_response = self._post(url, {})
        return AccountBalance(json_response)

    def getOpenOrders(self):
        url = "https://www.bitstamp.net/api/open_orders/"
        json_response = self._post(url, {})
        return [Order(json_open_order) for json_open_order in json_response]

    def cancelOrder(self, orderId):
        url = "https://www.bitstamp.net/api/cancel_order/"
        params = {"id": orderId}
        json_response = self._post(url, params)
        if json_response != True:
            raise Exception("Failed to cancel order")

    def buyLimit(self, limitPrice, quantity):
        url = "https://www.bitstamp.net/api/buy/"

        # Rounding price to avoid 'Ensure that there are no more than 2 decimal places'
        # error.
        price = round(limitPrice, 2)
        # Rounding amount to avoid 'Ensure that there are no more than 8 decimal places'
        # error.
        amount = round(quantity, 8)

        params = {
            "price": price,
            "amount": amount
        }
        json_response = self._post(url, params)
        return Order(json_response)

    def sellLimit(self, limitPrice, quantity):
        url = "https://www.bitstamp.net/api/sell/"

        # Rounding price to avoid 'Ensure that there are no more than 2 decimal places'
        # error.
        price = round(limitPrice, 2)
        # Rounding amount to avoid 'Ensure that there are no more than 8 decimal places'
        # error.
        amount = round(quantity, 8)

        params = {
            "price": price,
            "amount": amount
        }
        json_response = self._post(url, params)
        return Order(json_response)

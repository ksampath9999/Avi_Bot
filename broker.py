from kiteconnect import KiteConnect
import config

kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)


def get_ltp(symbol):
    quote = kite.ltp(symbol)
    return quote[symbol]["last_price"]


def place_order(symbol, side):

    order = kite.place_order(
        variety="regular",
        exchange="NFO",
        tradingsymbol=symbol,
        transaction_type=side,
        quantity=config.LOT_SIZE,
        order_type="MARKET",
        product="MIS"
    )

    return order
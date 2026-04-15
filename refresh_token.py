from kiteconnect import KiteConnect

api_key = "gdbe7dryjqavbnb7"
api_secret = "sioo47ef9qf59qbouy4m7d3cmm5w5sez"
request_token = "TCj43CAR2pkkhndwoUajIWzhRQxrxUy9"

kite = KiteConnect(api_key=api_key)

data = kite.generate_session(request_token, api_secret=api_secret)
print(data["access_token"])
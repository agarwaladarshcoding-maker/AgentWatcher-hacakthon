import asyncio
from websockets.server import serve
import websockets
import json
import logging
import re

async def handler(websocket):
    print("Connected")

async def main():
    try:
        async with serve(handler, "localhost", 59459, origins=[
            re.compile(r"^chrome-extension://.*$"),
            re.compile(r"^http://localhost:.*$"),
            None
        ]):
            import urllib.request
            req = urllib.request.Request("http://localhost:59459", headers={"Connection": "Upgrade", "Upgrade": "websocket", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13", "Origin": "chrome-extension://jpehalfe"})
            try:
                # Should not raise 400
                urllib.request.urlopen(req, timeout=1)
                print("Accepted")
            except urllib.error.HTTPError as e:
                print("Code:", e.code)
    except Exception as e:
        print("ERR:", e)

asyncio.run(main())

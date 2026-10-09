# tests/fake_server.py

import asyncio
import json
import random
import sys

from websockets import ServerConnection
from websockets.asyncio.server import serve

FIXTURE = "tests/fixtures/sample.jsonl"
MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
# modes: normal | drop | garbage | error | silent | seqgap | badfield

async def handler(ws: ServerConnection):
    await ws.recv() # receive subscribe command
    frames = [json.loads(l)["msg"] for l in open(FIXTURE)]
    for i, frame in enumerate(frames):
        if MODE == "drop" and i == random.randint(0, len(frames) - 1):
            await ws.close(code=1011) # server fail
            return
        if MODE == "garbage" and i % 10 == random.randint(0, len(frames) - 1):
            await ws.send("{not json") # data garbage
        if MODE == "silent" and i == random.randint(0, len(frames) - 1):
            await asyncio.sleep(300) # dead socket

        await ws.send(frame)
        await asyncio.sleep(0.001)

async def main():
    async with serve(handler, "localhost", 8765):
        await asyncio.Future()

asyncio.run(main())

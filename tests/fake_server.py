#! python
# tests/fake_server.py

import asyncio
import datetime
import json
import random
import sys

from websockets import ServerConnection
from websockets.asyncio.server import serve

FIXTURE = "tests/fixtures/sample.jsonl"
MODE = sys.argv[1] if len(sys.argv) > 1 else "normal"
# modes: normal | drop | garbage | error | silent | seqgap | badfield


async def handler(ws: ServerConnection):
    print(MODE)
    await ws.recv()  # receive subscribe command
    frames = [json.loads(l)["msg"] for l in open(FIXTURE)]  # noqa: ASYNC230, SIM115
    rand_num = random.randint(0, len(frames) - 1)
    while True:
        for i, frame in enumerate(frames):
            if MODE == "drop" and i == rand_num:
                print("Connection dropped")
                await ws.close(code=1011)  # server fail
                return
            if MODE == "garbage" and i % 10 == rand_num:
                print("Garbage sent")
                await ws.send("{not json")  # data garbage
            if MODE == "silent" and i == rand_num:
                print("Not responding for 300 sec")
                await asyncio.sleep(300)  # dead socket
            if MODE == "error" and i == rand_num:
                print("Sending an error message now")
                await ws.send(
                    json.dumps(
                        {
                            "id": 123,
                            "type": "error",
                            "sending_ts_ms": int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000),
                            "msg": {"code": 7, "msg": "Unknown subscription ID"},
                        }
                    )
                )  # error message
            if MODE == "seqgap" and i == rand_num and json.loads(frame)["type"] != "subscribed":
                print(f"Sending a seqgap. Should be {json.loads(frame)['seq']} but sending {json.loads(frame)['seq'] - 1}")
                seqgap_frame = json.loads(frame)
                seqgap_frame["seq"] = int(json.loads(frame)["seq"]) - 1
                frame = json.dumps(seqgap_frame)
            if MODE == "badfield" and i == rand_num:
                print("Removing field `side` from msg")
                badfield_frame = json.loads(frame)
                badfield_frame["msg"].pop("side", None)
                frame = json.dumps(badfield_frame)

            await ws.send(frame)
            await asyncio.sleep(0.001)

        print("Out of data, looping over it again!")


async def main():
    try:
        async with serve(handler, "localhost", 8765):
            await asyncio.Future()
    except KeyboardInterrupt:
        print("Stopping the server")


asyncio.run(main())

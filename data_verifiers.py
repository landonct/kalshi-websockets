import json
from pathlib import Path

from data_models import NonEmptyBook, OrderBook


def check_file(paths: list[Path]):
    for path in paths:
        lines = 0
        num_execptions = 0
        with open(path, "r") as f:
            for lines, line in enumerate(f, start=1):
                try:
                    thing = json.loads(line)
                    try:
                        json.loads(thing["msg"])
                    except json.JSONDecodeError:
                        print(f"Unable to decode inner json at line number {lines}")
                        num_execptions += 1
                except json.JSONDecodeError:
                    print(f"Unable to decode line number {lines}")
                    num_execptions += 1
        print(f"Found {num_execptions} exceptions in {path.name}, (scanned {lines} line(s))")


def clean_data(paths: list[Path]):
    for path in paths:
        prices = set()

        with open(path, "r") as f:
            for lines, line in enumerate(f, start=1):
                try:
                    thing = json.loads(line)
                    try:
                        message = json.loads(thing["msg"])

                    except json.JSONDecodeError:
                        print(f"Unable to decode inner json at line number {lines}")
                except json.JSONDecodeError:
                    print(f"Unable to decode line number {lines}")


def replay_json(path: Path, book: OrderBook) -> OrderBook:
    if book.ready:
        raise NonEmptyBook("The book supplied already has had a snapshot applied. `book` must be empty")
    with open(path, "r") as f:
        for line in f:
            data = json.loads(line)
            # import ipdb; ipdb.set_trace()
            msg = json.loads(data["msg"])
            msg_type = msg.get("type")
            inner_msg = msg.get("msg", {})

            if msg_type == "subscribed":
                # \n first so we don't leave the status line half-drawn above.
                print(f"Subscribed: {data}")

            elif msg_type == "orderbook_snapshot":
                book.apply_snapshot(inner_msg)
                print(
                    f"Snapshot applied: {len(book.yes)} yes levels, "
                    f"{len(book.no)} no levels"
                )

            elif msg_type == "orderbook_delta":
                if not book.ready:
                    continue  # wait for the snapshot before applying deltas, should ne be the case as snapshot is sent first
                book.apply_delta(inner_msg)

            elif msg_type == "error":
                print(f"\nError: {data}")

            else:
                print(f"\nUnhandled message type {msg_type!r}: {data}")

    return book

"""Build the repost watcher's index (data/repost/index.bin) from a Telegram Desktop export of the chat.

usage: build_repost_index.py result.json hashes.npz sscd.npy sscd_names.npy out.bin

hashes.npz holds the PDQ hash of every exported photo file (`name`, `pdq`), sscd.npy the SSCD
descriptor of every file named in sscd_names.npy. One row per photo message, in message order:
a picture sent again is exported once, as the same file, and gets a row for every message.
The photo files themselves go to data/repost/photos/ under their export names.
"""
import os
import sys
import json
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from repost_detector import ROW


def author(from_id):
    # the export writes "user<id>" / "channel<id>"; pyrogram reports a channel as -100<id>
    from_id = from_id or ""
    kind = from_id.rstrip("0123456789")
    if kind == "user":
        return int(from_id[4:])
    if kind == "channel":
        return -(10 ** 12 + int(from_id[7:]))
    return 0


export, hashes, sscd_path, sscd_names, out = sys.argv[1:6]
H = np.load(hashes)
pdq = dict(zip((str(n) for n in H["name"]), H["pdq"]))
sscd = dict(zip((str(n) for n in np.load(sscd_names)), np.load(sscd_path)))
msgs = sorted((m for m in json.load(open(export, encoding="utf-8"))["messages"] if "photo" in m),
              key=lambda m: m["id"])
rows = []
for m in msgs:
    name = m["photo"].split("/")[-1]
    if name in pdq and name in sscd:
        rows.append((m["id"], author(m.get("from_id")), int(m["date_unixtime"]), pdq[name], sscd[name],
                     name.encode()))
np.array(rows, dtype=ROW).tofile(out)
print(len(msgs), "photo messages,", len(rows), "rows written,", len(msgs) - len(rows), "without a hashed file")

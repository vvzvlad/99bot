"""Build the repost watcher's index, rating and last_seen (data/repost/) from a Telegram Desktop export of the chat.

usage: build_repost_index.py result.json hashes.npz sscd.npy sscd_names.npy replay.json out_dir

hashes.npz holds the PDQ hash and the size of every exported photo file (`name`, `pdq`, `w`, `h`), sscd.npy the
SSCD descriptor of every file named in sscd_names.npy, replay.json the reposts found by replaying the history
(`events`, each with the reposting message's `new_id`). Written to out_dir:
- index.bin: one row per photo message, in message order: a picture sent again is exported once, as the same
  file, and gets a row for every message;
- rating.bin: one row per index row, whether the picture counts in the rating (not 4:3 or 16:9) and whether it
  is a repost;
- last_seen.json: every author's last message time and the id of the export's last message.
The photo files themselves go to data/repost/photos/ under their export names.
"""
import os
import sys
import json
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from repost_detector import ROW, RATE, is_photo_aspect


def author(from_id):
    # the export writes "user<id>" / "channel<id>"; pyrogram reports a channel as -100<id>
    from_id = from_id or ""
    kind = from_id.rstrip("0123456789")
    if kind == "user":
        return int(from_id[4:])
    if kind == "channel":
        return -(10 ** 12 + int(from_id[7:]))
    return 0


export, hashes, sscd_path, sscd_names, replay, out_dir = sys.argv[1:7]
H = np.load(hashes)
names = [str(n) for n in H["name"]]
pdq = dict(zip(names, H["pdq"]))
size = dict(zip(names, zip(H["w"].tolist(), H["h"].tolist())))
sscd = dict(zip((str(n) for n in np.load(sscd_names)), np.load(sscd_path)))
reposts = {e["new_id"] for e in json.load(open(replay, encoding="utf-8"))["events"]}
messages = json.load(open(export, encoding="utf-8"))["messages"]
msgs = sorted((m for m in messages if "photo" in m), key=lambda m: m["id"])
rows, rates = [], []
for m in msgs:
    name = m["photo"].split("/")[-1]
    if name in pdq and name in sscd:
        a = author(m.get("from_id"))
        rows.append((m["id"], a, int(m["date_unixtime"]), pdq[name], sscd[name], name.encode()))
        rates.append((m["id"], a, not is_photo_aspect(*size[name]), m["id"] in reposts))
np.array(rows, dtype=ROW).tofile(os.path.join(out_dir, "index.bin"))
rates = np.array(rates, dtype=RATE)
rates.tofile(os.path.join(out_dir, "rating.bin"))
seen = {}
for m in messages:
    if m["type"] == "message":
        key = str(author(m.get("from_id")))
        seen[key] = max(seen.get(key, 0), int(m["date_unixtime"]))
with open(os.path.join(out_dir, "last_seen.json"), "w", encoding="utf-8") as f:
    json.dump({"max_id": messages[-1]["id"], "seen": seen}, f)
print(len(msgs), "photo messages,", len(rows), "rows written,", len(msgs) - len(rows), "without a hashed file;",
      int(rates["counted"].sum()), "counted,", int((rates["counted"] & rates["repost"]).sum()), "of them reposts;",
      len(seen), "authors in last_seen.json")

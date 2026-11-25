"""Read the text on every photo of the repost index (Tesseract) and write the bot's text index (data/repost/texts.jsonl).

usage: build_image_texts.py photos_dir index.bin texts.jsonl [workers]

Messages that already have a line in texts.jsonl are skipped, so a rerun resumes where the last one stopped. A picture sent
again is one file for several messages: it is read once and every message gets a line. workers defaults to the CPU count.
"""
import os
import subprocess
import sys
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from image_text import TextIndex, read_text
from repost_detector import ROW

PROGRESS_EVERY = 1000


def read_file(path):
    """(path, text, None), or (path, None, error) when Tesseract fails on this one file."""
    try:
        return path, read_text(path), None
    except subprocess.CalledProcessError as e:
        return path, None, f"exit {e.returncode}: {e.stderr.strip()}"


if __name__ == "__main__":
    photos_dir, index_path, texts_path = sys.argv[1:4]
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else os.cpu_count()
    rows = np.fromfile(index_path, ROW)
    texts = TextIndex(texts_path)
    ids_by_file = {}
    for msg_id, file in zip(rows["id"], rows["file"]):
        if int(msg_id) not in texts.ids:
            ids_by_file.setdefault(os.path.join(photos_dir, file.decode()), []).append(int(msg_id))
    print(f"{len(rows)} rows, {sum(map(len, ids_by_file.values()))} messages without text, "
          f"{len(ids_by_file)} files to read, {workers} workers")
    done = failed = 0
    with Pool(workers) as pool:
        for path, text, error in pool.imap_unordered(read_file, ids_by_file):
            done += 1
            if error:
                failed += 1
                print(f"failed: {path}: {error}")
            else:
                for msg_id in ids_by_file[path]:
                    texts.add(msg_id, text)
            if done % PROGRESS_EVERY == 0:
                print(f"{done}/{len(ids_by_file)} files")
    print(f"{done} files read, {failed} failed")

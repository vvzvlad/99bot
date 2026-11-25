"""Read the text on every photo of the repost index (RapidOCR) and write the bot's text index (data/repost/texts.jsonl).

usage: build_image_texts.py photos_dir index.bin texts.jsonl [workers]

Messages that already have a line in texts.jsonl are skipped, so a rerun resumes where the last one stopped. A picture sent
again is one file for several messages: it is read once and every message gets a line. workers defaults to 8 (see
init_worker for why more do not help).
"""
import os
import sys
from multiprocessing import Pool

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from image_text import TextIndex, build_engine, read_text
from repost_detector import ROW

PROGRESS_EVERY = 1000

# the engine of this worker process
_engine = None


def init_worker():
    # The OCR is bound by memory bandwidth, not by cores: on the CI runner 8 workers read about 100 pictures a minute
    # and 36 workers no more. The arena keeps the buffers of one read for the next instead of allocating them afresh
    # (8 workers: 93 pictures a minute without it, 144 with it), at about 1 GB resident per worker, which is why
    # the bot does not use it.
    global _engine
    _engine = build_engine({"EngineConfig.onnxruntime.enable_cpu_mem_arena": True})


def read_file(path):
    """(path, text, None), or (path, None, error) when the OCR fails on this one file."""
    try:
        return path, read_text(path, _engine), None
    except Exception as e:
        return path, None, str(e)


if __name__ == "__main__":
    photos_dir, index_path, texts_path = sys.argv[1:4]
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 8
    rows = np.fromfile(index_path, ROW)
    texts = TextIndex(texts_path)
    ids_by_file = {}
    for msg_id, file in zip(rows["id"], rows["file"]):
        if int(msg_id) not in texts.ids:
            ids_by_file.setdefault(os.path.join(photos_dir, file.decode()), []).append(int(msg_id))
    print(f"{len(rows)} rows, {sum(map(len, ids_by_file.values()))} messages without text, "
          f"{len(ids_by_file)} files to read, {workers} workers")
    done = failed = 0
    # newest first: a run stopped halfway has already read the pictures people search for most
    files = sorted(ids_by_file, key=lambda path: max(ids_by_file[path]), reverse=True)
    with Pool(workers, initializer=init_worker) as pool:
        for path, text, error in pool.imap_unordered(read_file, files):
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

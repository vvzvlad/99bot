"""Text on pictures: OCR with Tesseract and a searchable index of what it read.

TextIndex keeps one JSON line per photo message ({"id": ..., "text": ...}) in a file. Nothing here imports the bot,
so the module runs standalone too (tools/build_image_texts.py uses it).
"""
import json
import os
import re
import subprocess

LANGS = "rus+eng"
# Tesseract with rus+eng gives the capitals of a Latin line as their Russian twins (МЕ, АТ, ВЕ) and back;
# after casefold these are the lowercase pairs of those capitals
TWINS = str.maketrans("abcehkmoptxy", "авсенкмортху")


def read_text(path):
    """Text Tesseract reads on the picture, whitespace collapsed to single spaces; '' when there is none."""
    # one thread per process: Tesseract's own threading only slows down many parallel runs
    out = subprocess.run(["tesseract", path, "-", "-l", LANGS], check=True, capture_output=True, text=True,
                         env={**os.environ, "OMP_THREAD_LIMIT": "1"}).stdout
    return " ".join(out.split())


def fold(text):
    """The form text is compared in: case, ё and Latin-Russian twin letters folded, punctuation and spacing dropped."""
    text = text.casefold().replace("ё", "е").translate(TWINS)
    # an apostrophe joins its word: DON'T is found by "dont" as well as "don't"
    text = re.sub(r"['’ʼ`]", "", text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


class TextIndex:
    """Texts read off photos so far: JSON lines in path, in the order they were added."""

    def __init__(self, path):
        self.path = path
        # ids of every message that has a line, empty text included, so no picture is read twice
        self.ids = set()
        # message id -> folded text, only for the messages where something was read
        self._folded = {}
        if os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
            # a trailing line cut short by a crash is dropped, so the next append starts on a line boundary
            keep = data.rfind(b"\n") + 1
            os.truncate(path, keep)
            for line in data[:keep].decode("utf-8").splitlines():
                row = json.loads(line)
                self._put(row["id"], row["text"])

    def _put(self, msg_id, text):
        self.ids.add(msg_id)
        folded = fold(text)
        if folded:
            self._folded[msg_id] = folded

    def add(self, msg_id, text):
        """Append one line to the file and to the memory."""
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": msg_id, "text": text}, ensure_ascii=False) + "\n")
            f.flush()
        self._put(msg_id, text)

    def search(self, query):
        """Ids of the messages whose text holds the query as a phrase, newest first.

        The words of the query follow one another in the text, each one the start of a word there: "у нас было"
        finds "у нас было 9 планет", "планет" finds "планеты", "бля" does not find "рублях".
        """
        words = fold(query).split()
        if not words:
            return []
        phrase = re.compile(r"(?:^| )" + r"\S* ".join(map(re.escape, words)))
        # the substring test is a cheap pre-filter: the phrase is matched only where every word occurs
        return sorted((i for i, t in self._folded.items() if all(w in t for w in words) and phrase.search(t)),
                      reverse=True)

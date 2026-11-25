"""Text on pictures: OCR with RapidOCR and a searchable index of what it read.

TextIndex keeps one JSON line per picture message ({"id": ..., "text": ...}) in a file. Nothing here imports the bot,
so the module runs standalone too (tools/build_image_texts.py uses it).
"""
import json
import os
import re

import av
from rapidocr import EngineType, LangRec, ModelType, OCRVersion, RapidOCR

# The OCR can give the capitals of a Latin line as their Russian twins (МЕ, АТ, ВЕ) and back;
# after casefold these are the lowercase pairs of those capitals
TWINS = str.maketrans("abcehkmoptxy", "авсенкмортху")

# built on the first read, like repost_detector's model; the models are in the image, none is downloaded at runtime
_engine = None


def build_engine():
    """The RapidOCR engine on one CPU thread (several runs go in parallel, so its own threading only gets in the way):
    detection PP-OCRv6 tiny, recognition PP-OCRv5 eslav mobile, Russian and English in one model."""
    return RapidOCR(params={
        "Global.log_level": "error",
        "EngineConfig.onnxruntime.intra_op_num_threads": 1,
        "EngineConfig.onnxruntime.inter_op_num_threads": 1,
        "Det.engine_type": EngineType.ONNXRUNTIME, "Det.ocr_version": OCRVersion.PPOCRV6, "Det.model_type": ModelType.TINY,
        "Rec.engine_type": EngineType.ONNXRUNTIME, "Rec.lang_type": LangRec.ESLAV,
        "Rec.model_type": ModelType.MOBILE, "Rec.ocr_version": OCRVersion.PPOCRV5,
    })


def read_text(path):
    """Text RapidOCR reads on the picture, whitespace collapsed to single spaces; '' when there is none.

    A .webm (a video sticker) is read as its first frame.
    """
    global _engine
    if _engine is None:
        _engine = build_engine()
    result = _engine(_first_frame(path) if path.endswith(".webm") else path)
    return " ".join(" ".join(result.txts or ()).split())


def _first_frame(path):
    """The first frame of a VP9 video sticker as an RGBA array.

    Decoded by libvpx, not by FFmpeg's own VP9 decoder, which drops the alpha channel: the transparent background then
    comes out black and dark text on it vanishes. Given the alpha, RapidOCR lays the picture on white or black, whichever
    contrasts with it, as it does for a transparent .webp sticker.
    """
    decoder = av.CodecContext.create("libvpx-vp9", "r")
    with av.open(path) as container:
        for packet in container.demux(container.streams.video[0]):
            # the last, empty packet of the demuxer flushes the decoder
            for frame in decoder.decode(packet):
                return frame.to_ndarray(format="rgba")
    raise ValueError(f"no frame in {path}")


def fold(text):
    """The form text is compared in: case, ё and Latin-Russian twin letters folded, punctuation and spacing dropped."""
    text = text.casefold().replace("ё", "е").translate(TWINS)
    # an apostrophe joins its word: DON'T is found by "dont" as well as "don't"
    text = re.sub(r"['’ʼ`]", "", text)
    return " ".join(re.sub(r"[\W_]+", " ", text).split())


class TextIndex:
    """Texts read off pictures so far: JSON lines in path, in the order they were added."""

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

        The words of the query follow one another in the text, the first one the start of a word there, each next one
        the start of a word or glued to the end of the previous one (the OCR often glues words), except after a
        one-letter word, where gluing would make "с ним" find "снимок": "у нас было" finds "у нас было 9 планет",
        "планет" finds "планеты", "бля че я несу" finds "блячея несу", "бля" does not find "рублях".
        """
        words = fold(query).split()
        if not words:
            return []
        phrase = re.compile(r"(?:^| )" + "".join(re.escape(w) + (r"\S* " if len(w) == 1 else r"(?:\S* )?")
                                                 for w in words[:-1]) + re.escape(words[-1]))
        # the substring test is a cheap pre-filter: the phrase is matched only where every word occurs
        return sorted((i for i, t in self._folded.items() if all(w in t for w in words) and phrase.search(t)),
                      reverse=True)

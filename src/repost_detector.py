"""Meme repost detector: flags a re-post of the same picture, stays silent for derivatives
(drawn marks, changed text, added caption).

Stage 1 (cheap): PDQ perceptual hash, Hamming distance <= PDQ_MAX -> candidate.
Stage 1 extra (optional): SSCD copy-detection descriptor, cosine >= 0.6 -> candidate (finds crops PDQ misses).
Stage 2 (per candidate): align the two images (SIFT + RANSAC similarity; a pair that cannot be aligned is not a repost), then
  - strong changes inside the common area (drawn marks, edited text, even one digit; re-encode noise, the image frame
    and rounded screenshot corners do not count; same-size pictures are compared unblurred, so small grey text counts;
    pictures smaller than WORK are enlarged to it first, so dense small text on a small banner counts too),
  - faint changes over a large area (translucent marker, replaced low-contrast text),
  - content in the part of the NEWER image that has no counterpart (added caption bar),
  - global colour shift (same scene, different lighting = different photo),
  - share of the old picture the new one shows (a zoom into a detail is a new message, a trimmed frame is not),
  - rotation (a rotated picture is an edit).

RepostIndex keeps every photo seen so far on disk and runs both stages for a new one, the way replay.py
replays the chat history. RepostRating keeps, for every index row, whether the picture counts in its author's
rating (camera photos do not) and whether it is a repost. Nothing here imports the bot, so the module runs
standalone too.
"""
import logging
import os
import time
import numpy as np
import cv2
import pdqhash
from PIL import Image

WORK = 768             # working max side for stage 2
SHORT_MIN = 384        # ...but a wide strip keeps at least this short side, so small captions stay readable
THR = 48               # per-pixel colour difference (0..255) that counts as a change
PDQ_MAX = 48           # stage 1: candidate if PDQ distance <= this (of 256 bits)
STRONG_MAX = 20        # stage 2: more strongly changed pixels than this -> derivative
MEDIUM_PEAK = 70       # stage 2: components whose peak difference reaches this are medium-strength changes
MEDIUM_MAX = 2000      # stage 2: more medium-strength changed pixels -> derivative (translucent marker, replaced grey text)
EXTRA_EDGES_MAX = 200  # stage 2: more edge pixels in the newer image's unmatched part -> added caption
SHIFT_MAX = 20         # stage 2: larger median colour shift -> different photo of the same scene
COVER_OLD_MIN = 0.7    # stage 2: new image shows less of the old one than this -> zoomed detail
ANGLE_MAX = 5          # stage 2: rotated by more degrees -> edit
CORNER = 32            # stage 2: changes inside this square at each corner of the common area are ignored (rounded screenshot corners)
SCALE_MIN, SCALE_MAX = 0.2, 5  # stage 2: an alignment that scales the picture outside this range is rejected
SAME_SCALE = 0.03      # stage 2: pictures within this relative scale share one pixel grid (no resampling between them)...
RAW_PEAK = 120         # ...so a change is strong if its unblurred peak reaches this (blur would flatten thin grey text)
K3 = np.ones((3, 3), np.uint8)
K9 = np.ones((9, 9), np.uint8)


def pdq_hash(path):
    """256-bit PDQ hash as 4 uint64 words, plus PDQ quality."""
    im = Image.open(path)
    im.draft('RGB', (512, 512))
    im = im.convert('RGB')
    im.thumbnail((512, 512))
    bits, quality = pdqhash.compute(np.asarray(im))
    words = np.frombuffer(np.packbits(np.asarray(bits, np.uint8)).tobytes(), dtype='>u8').astype(np.uint64)
    return words, quality


def pdq_distance(h, hashes):
    """Hamming distance from one hash (4,) to many (N, 4)."""
    return np.bitwise_count(hashes ^ h).sum(axis=-1)


def load(path):
    im = cv2.imread(path, cv2.IMREAD_COLOR)
    s = max(WORK / max(im.shape[:2]), SHORT_MIN / min(im.shape[:2]))
    if s < 1:
        im = cv2.resize(im, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return im


def align(a, b):
    """2x3 similarity transform mapping b -> a, and the RANSAC inlier count (0 = fallback resize)."""
    sift = cv2.SIFT_create(3000)
    ka, da = sift.detectAndCompute(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), None)
    kb, db = sift.detectAndCompute(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), None)
    if da is not None and db is not None and len(ka) >= 8 and len(kb) >= 8:
        m = cv2.BFMatcher().knnMatch(db, da, k=2)
        good = [x[0] for x in m if len(x) == 2 and x[0].distance < 0.75 * x[1].distance]
        if len(good) >= 12:
            pb = np.float32([kb[g.queryIdx].pt for g in good])
            pa = np.float32([ka[g.trainIdx].pt for g in good])
            M, inl = cv2.estimateAffinePartial2D(pb, pa, method=cv2.RANSAC, ransacReprojThreshold=2.0)
            # a scale far from 1 is a degenerate fit (e.g. all matches collapsed onto one text line), not an alignment
            if M is not None and inl.sum() >= 12 and SCALE_MIN <= np.hypot(M[0, 0], M[1, 0]) <= SCALE_MAX:
                return M, int(inl.sum())
    # fallback: assume the same framing, plain resize
    return np.float32([[a.shape[1] / b.shape[1], 0, 0], [0, a.shape[0] / b.shape[0], 0]]), 0


def _outside(x, y):
    """How far each pixel of x lies outside the 3x3 min/max envelope of y (max over channels)."""
    lo = cv2.erode(y, K3).astype(np.int16)
    hi = cv2.dilate(y, K3).astype(np.int16)
    x = x.astype(np.int16)
    return np.maximum(lo - x, x - hi).clip(0).max(2)


def _extra_edges(img, other_on_img_mask):
    """Edge pixels in the part of img that the other image does not cover (seams and frame edge excluded)."""
    unc = cv2.erode((other_on_img_mask == 0).astype(np.uint8), K9,
                    borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    edges = cv2.Canny(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 60, 160) > 0
    return int((edges & unc).sum())


def compare(new_path, old_path):
    """Stage 2 for one candidate pair. Returns metrics and repost=True/False."""
    a, b = load(new_path), load(old_path)
    new_is_a = True
    if a.shape[0] * a.shape[1] < b.shape[0] * b.shape[1]:
        a, b, new_is_a = b, a, False          # warp the smaller image onto the larger one
    # a canvas smaller than WORK is enlarged to it, the other image by the same factor (their relative scale stays):
    # at native size the 3x3 tolerance spans whole lines of small dense text (a 433px notification banner
    # with other news passed as a repost)
    up = WORK / max(a.shape[:2])
    if up > 1:
        a = cv2.resize(a, None, fx=up, fy=up, interpolation=cv2.INTER_LINEAR)
        b = cv2.resize(b, None, fx=up, fy=up, interpolation=cv2.INTER_LINEAR)
    M, inl = align(a, b)
    h, w = a.shape[:2]
    bw = cv2.warpAffine(b, M, (w, h), flags=cv2.INTER_LINEAR, borderValue=0)
    cover_a = cv2.warpAffine(np.full(b.shape[:2], 255, np.uint8), M, (w, h), flags=cv2.INTER_NEAREST)
    cover_b = cv2.warpAffine(np.full(a.shape[:2], 255, np.uint8), cv2.invertAffineTransform(M),
                             (b.shape[1], b.shape[0]), flags=cv2.INTER_NEAREST)
    # the image frame is eroded too: a 1px crop line along a screenshot edge is not a change
    mask = cv2.erode(cover_a, K9, borderType=cv2.BORDER_CONSTANT, borderValue=0) > 0
    fa = cv2.GaussianBlur(a, (5, 5), 1.2)
    fb = cv2.GaussianBlur(bw, (5, 5), 1.2)
    # a pixel changes if it lies outside the other image's 3x3 envelope (both ways):
    # tolerates 1px misalignment but keeps thin strokes present in one image only
    d = np.maximum(_outside(fa, fb), _outside(fb, fa))
    ch = ((d > THR) & mask).astype(np.uint8)
    ch = cv2.morphologyEx(ch, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    ys, xs = np.nonzero(mask)
    if len(xs):
        x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
        for cx in (x0, max(x0, x1 - CORNER)):
            for cy in (y0, max(y0, y1 - CORNER)):
                ch[cy:cy + CORNER, cx:cx + CORNER] = 0
    # changed pixels in components whose peak difference is strong (ink on paper, not re-encode tint)
    n, lab, st, _ = cv2.connectedComponentsWithStats(ch, connectivity=8)
    peak = np.zeros(n, np.int16)
    np.maximum.at(peak, lab[ch > 0], d[ch > 0])
    scale = float(np.hypot(M[0, 0], M[1, 0]))
    if abs(scale - 1) < SAME_SCALE:
        # same pixel grid: read the peak from the unblurred images, blur flattens thin strokes of small text
        raw = np.maximum(_outside(a, bw), _outside(bw, a))
        peak_raw = np.zeros(n, np.int16)
        np.maximum.at(peak_raw, lab[ch > 0], raw[ch > 0])
        strong = int(st[1:, cv2.CC_STAT_AREA][peak_raw[1:] >= RAW_PEAK].sum()) if n > 1 else 0
    else:
        # resampled: unblurred edges differ by interpolation alone, only the blurred peak is trusted
        strong = int(st[1:, cv2.CC_STAT_AREA][peak[1:] >= 100].sum()) if n > 1 else 0
    # faint but large changes (translucent marker); re-encode tint on flat areas stays below MEDIUM_PEAK
    medium = int(st[1:, cv2.CC_STAT_AREA][peak[1:] >= MEDIUM_PEAK].sum()) if n > 1 else 0
    sd = (fa.astype(np.int16) - fb.astype(np.int16))[mask]
    shift = float(np.abs(np.median(sd, axis=0)).max()) if sd.size else 0.0
    extra_new = _extra_edges(a, cover_a) if new_is_a else _extra_edges(b, cover_b)
    changed = int(ch.sum())
    cover_small, cover_large = float((cover_b > 0).mean()), float((cover_a > 0).mean())
    # share of the old picture that the new one shows (a zoom into a detail shows little of it)
    cover_old = cover_small if new_is_a else cover_large
    angle = float(np.degrees(np.arctan2(M[1, 0], M[0, 0])))
    return dict(changed=changed, strong=strong, medium=medium, extra_new=extra_new, shift=round(shift, 1), inliers=inl,
                cover_small=round(cover_small, 3), cover_large=round(cover_large, 3), cover_old=round(cover_old, 3),
                angle=round(angle, 1),
                # inl == 0: SIFT could not align the pair, the plain-resize fallback cannot prove it is the same picture
                repost=(inl > 0 and strong <= STRONG_MAX and medium <= MEDIUM_MAX and extra_new <= EXTRA_EDGES_MAX
                        and shift <= SHIFT_MAX and cover_old >= COVER_OLD_MIN and abs(angle) <= ANGLE_MAX))


logger = logging.getLogger(__name__)

SSCD_MIN = 0.6           # stage 1 extra: candidate if SSCD cosine >= this
SAME_AUTHOR_GAP = 3600   # the same author re-sending within this many seconds is not "это уже было"
MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "models", "sscd_disc_mixup.torchscript.pt")
# one record per photo message, appended to the index file in message order
ROW = np.dtype([('id', '<i8'), ('author', '<i8'), ('date', '<i8'), ('pdq', '<u8', (4,)), ('sscd', '<f4', (512,)),
                ('file', 'S64')])
# one record per index row, in the same order, appended to the rating file
RATE = np.dtype([('id', '<i8'), ('author', '<i8'), ('counted', 'u1'), ('repost', 'u1')])

_model = _tf = None


def load_model():
    """Load the SSCD model once; torch and torchvision are imported only here."""
    global _model, _tf
    if _model is None:
        import torch
        from torchvision import transforms
        _model = torch.jit.load(MODEL_PATH).eval()
        _tf = transforms.Compose([transforms.Resize(288), transforms.ToTensor(),
                                  transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])


def embed(path):
    """SSCD copy-detection descriptor, float32 of shape (512,)."""
    load_model()
    import torch
    im = Image.open(path).convert("RGB")
    with torch.no_grad():
        return _model(_tf(im).unsqueeze(0))[0].numpy().astype(np.float32)


def features(path):
    """Stage 1 features of one photo: PDQ hash words (4,) uint64 and SSCD descriptor (512,) float32."""
    return pdq_hash(path)[0], embed(path)


def _same_file(a, b):
    """True when the two files are byte for byte identical."""
    if os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa, open(b, "rb") as fb:
        return fa.read() == fb.read()


class RepostIndex:
    """Photos seen so far: ROW records in index_path, the photo files themselves in photos_dir."""

    def __init__(self, index_path, photos_dir):
        self.index_path = index_path
        self.photos_dir = photos_dir
        rows = np.zeros(0, ROW)
        if os.path.exists(index_path):
            # a trailing record cut short by a crash is dropped, so the next append starts on a record boundary
            n = os.path.getsize(index_path) // ROW.itemsize
            os.truncate(index_path, n * ROW.itemsize)
            rows = np.fromfile(index_path, ROW, count=n)
        self.ids = np.ascontiguousarray(rows['id'])
        self.authors = np.ascontiguousarray(rows['author'])
        self.dates = np.ascontiguousarray(rows['date'])
        self.pdq = np.ascontiguousarray(rows['pdq'], dtype=np.uint64)
        self.sscd = np.ascontiguousarray(rows['sscd'], dtype=np.float32)
        self.files = np.ascontiguousarray(rows['file'])

    @property
    def max_id(self):
        """Largest message id in the index, 0 when it is empty."""
        return int(self.ids.max()) if len(self.ids) else 0

    def append(self, row):
        """Append one record (a ROW array of shape (1,)) to the file and to the arrays."""
        with open(self.index_path, "ab") as f:
            f.write(row.tobytes())
            f.flush()
        self.ids = np.concatenate([self.ids, row['id']])
        self.authors = np.concatenate([self.authors, row['author']])
        self.dates = np.concatenate([self.dates, row['date']])
        self.pdq = np.concatenate([self.pdq, row['pdq'].astype(np.uint64)])
        self.sscd = np.concatenate([self.sscd, row['sscd'].astype(np.float32)])
        self.files = np.concatenate([self.files, row['file']])

    def first_repost(self, path, msg_id, author, date, pdq, sscd, excluded=()):
        """Message id of the earliest photo that the photo at path re-posts, or None.

        Candidates are earlier photos close by PDQ or SSCD, tried oldest first: a byte-identical file is a repost
        outright, any other goes through stage 2, and the walk stops at the first repost. None also when that first
        hit is the same author re-sending it within SAME_AUTHOR_GAP seconds, unless the hit is one of the excluded
        message ids: that one is returned, so the caller can leave the photo out altogether.
        """
        t = time.monotonic()
        c = (self.ids < msg_id) & ((pdq_distance(pdq, self.pdq) <= PDQ_MAX) | (self.sscd @ sscd >= SSCD_MIN))
        cand = np.nonzero(c)[0]
        cand = cand[np.argsort(self.ids[cand], kind='stable')]
        hit, tried = None, 0
        for j in cand:
            tried += 1
            old = os.path.join(self.photos_dir, self.files[j].decode())
            # the same file sent again (a forward) is a repost without stage 2, which also cannot align a flat picture
            if _same_file(path, old) or compare(path, old)["repost"]:
                hit = j
                break
        old_id = None if hit is None else int(self.ids[hit])
        resend = hit is not None and self.authors[hit] == author and date - self.dates[hit] < SAME_AUTHOR_GAP
        logger.info(f"photo {msg_id}: hit {old_id}{' (same author re-sent it, no reply)' if resend else ''}, "
                    f"{tried}/{len(cand)} candidates tried in {time.monotonic() - t:.1f}s")
        return None if resend and old_id not in excluded else old_id

    def reposts_of(self, j):
        """Rows tied to row j as a repost in either direction, judged as first_repost judges a pair.

        Candidates are the other rows close by PDQ or SSCD; of each pair the newer photo is checked against the older
        one. SAME_AUTHOR_GAP does not apply: this is about the picture, not about a reply.
        """
        c = (pdq_distance(self.pdq[j], self.pdq) <= PDQ_MAX) | (self.sscd @ self.sscd[j] >= SSCD_MIN)
        c[j] = False
        path = os.path.join(self.photos_dir, self.files[j].decode())
        tied = []
        for i in np.nonzero(c)[0]:
            other = os.path.join(self.photos_dir, self.files[i].decode())
            new, old = (other, path) if self.ids[i] > self.ids[j] else (path, other)
            if _same_file(new, old) or compare(new, old)["repost"]:
                tied.append(i)
        return np.array(tied, np.int64)


def is_photo_aspect(w, h):
    """True when the picture has a camera's proportions, 4:3 or 16:9 within 1 %."""
    r = max(w, h) / min(w, h)
    return any(abs(r / a - 1) <= 0.01 for a in (4 / 3, 16 / 9))


class RepostRating:
    """Per index row: whether the picture counts in the rating and whether it is a repost (RATE records in rating_path)."""

    def __init__(self, rating_path):
        self.rating_path = rating_path
        rows = np.zeros(0, RATE)
        if os.path.exists(rating_path):
            # a trailing record cut short by a crash is dropped, so the next append starts on a record boundary
            n = os.path.getsize(rating_path) // RATE.itemsize
            os.truncate(rating_path, n * RATE.itemsize)
            rows = np.fromfile(rating_path, RATE, count=n)
        self.ids = np.ascontiguousarray(rows['id'])
        self.authors = np.ascontiguousarray(rows['author'])
        self.counted = np.ascontiguousarray(rows['counted'])
        self.repost = np.ascontiguousarray(rows['repost'])

    @property
    def max_id(self):
        """Largest message id in the rating, 0 when it is empty."""
        return int(self.ids.max()) if len(self.ids) else 0

    def append(self, row):
        """Append one record (a RATE array of shape (1,)) to the file and to the arrays."""
        with open(self.rating_path, "ab") as f:
            f.write(row.tobytes())
            f.flush()
        self.ids = np.concatenate([self.ids, row['id']])
        self.authors = np.concatenate([self.authors, row['author']])
        self.counted = np.concatenate([self.counted, row['counted']])
        self.repost = np.concatenate([self.repost, row['repost']])

    def exclude(self, ids):
        """Stop counting the rows of these message ids: counted=0 in memory and, byte by byte, in rating_path."""
        rows = np.nonzero(np.isin(self.ids, ids) & (self.counted > 0))[0]
        self.counted[rows] = 0
        with open(self.rating_path, "r+b") as f:
            for i in rows:
                f.seek(i * RATE.itemsize + RATE.fields['counted'][1])
                f.write(b"\x00")

    def stats(self, author):
        """(n, r) of one author: counted pictures and how many of them are reposts."""
        mine = (self.authors == author) & (self.counted > 0)
        return int(mine.sum()), int((mine & (self.repost > 0)).sum())

    def table(self):
        """{author: (n, r)} for every author with a counted picture."""
        c = self.counted > 0
        authors, inv = np.unique(self.authors[c], return_inverse=True)
        n = np.bincount(inv, minlength=len(authors))
        r = np.bincount(inv, weights=self.repost[c] > 0, minlength=len(authors)).astype(np.int64)
        return {int(a): (int(x), int(y)) for a, x, y in zip(authors, n, r)}

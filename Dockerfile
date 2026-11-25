FROM python:3.11-slim

# Not decoration, and not a performance tweak. config.get_settings() prints
# "TG_API_ID and TG_API_HASH must be set" and then calls os._exit(1), which terminates the
# process WITHOUT flushing stdout. Docker gives a container a pipe rather than a tty, so
# CPython block-buffers stdout by default and that message would be written into a buffer
# that is thrown away: a deployment with a forgotten variable would exit 1 with a
# completely empty log. The CI gate (ci/smoke.py) asserts both the exit code and the
# message, so deleting this line turns that check red instead of turning the log silent.
ENV PYTHONUNBUFFERED=1

WORKDIR /app
RUN mkdir -p data
# CPU-only wheels first: from PyPI, requirements.txt would pull the CUDA build of torch, gigabytes
RUN pip install --no-cache-dir torch torchvision --index-url https://download.pytorch.org/whl/cpu
# SSCD copy-detection model, mirrored from dl.fbaipublicfiles.com/sscd-copy-detection/: the CI
# runner's connection to that CDN stalls after the first few KB
ADD https://gitea.vvzvlad.xyz/api/packages/projects/generic/sscd_disc_mixup/1.0/sscd_disc_mixup.torchscript.pt /app/models/sscd_disc_mixup.torchscript.pt
COPY requirements.txt .
# pdqhash publishes no wheels, so it is compiled here; the compiler goes away in the same layer
RUN apt-get update && apt-get install -y --no-install-recommends g++ \
    && pip install --no-cache-dir -r requirements.txt \
    && apt-get purge -y --auto-remove g++ && rm -rf /var/lib/apt/lists/*
# the OCR models (/find) are downloaded now, so production downloads nothing at runtime. RapidOCR fetches them on
# its first read, not when the engine is built, so a drawn caption is read here; reading nothing fails the build
COPY src/image_text.py /tmp/image_text.py
RUN python -c "import sys, cv2, numpy as np; sys.path.insert(0, '/tmp'); import image_text; \
im = np.full((120, 600, 3), 255, np.uint8); \
cv2.putText(im, 'CEO OF SEX', (20, 80), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 5); cv2.imwrite('/tmp/ocr.png', im); \
sys.exit(0 if image_text.read_text('/tmp/ocr.png') else 'the OCR read nothing on a drawn caption')" \
    && rm -r /tmp/image_text.py /tmp/ocr.png /tmp/__pycache__
COPY src/ src/

CMD ["python", "src/main.py"]

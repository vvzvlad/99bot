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
COPY src/ src/

CMD ["python", "src/main.py"]

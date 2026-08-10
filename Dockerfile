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
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/

CMD ["python", "src/main.py"]

# A small, non-root image for the teaching engine.
#
# The production DeepInfer image is much larger: CUDA/CANN toolchains, the
# compiled C++ scheduler, kernel libraries and a multi-gigabyte checkpoint.
# This image exists to make the deployment shape (health probe, read-only
# root, model cache mount, graceful stop) concrete and testable.

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MINI_DEEPINFER_MODEL_DIR=/models

WORKDIR /app

COPY pyproject.toml README.md ./
COPY mini_deepinfer ./mini_deepinfer
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /models \
    && chown -R appuser:appuser /models /app
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

# SIGTERM is handled by the server's drain path in a real deployment.  The
# teaching server exits when the process is stopped; the Kubernetes manifest
# shows where preStop and terminationGracePeriodSeconds belong.
STOPSIGNAL SIGTERM

CMD ["python", "-m", "mini_deepinfer.cli", "serve", "--host", "0.0.0.0", "--port", "8000"]

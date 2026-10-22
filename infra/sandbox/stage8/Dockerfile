# Same pinned Python foundation, with only the locked scientific dependency closure.
FROM --platform=linux/amd64 python:3.12.13-slim@sha256:866411c135b507754efdf2fda51484be4d3d7d5173ed53cd083106132e710904
ENV PYTHONHASHSEED=0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
WORKDIR /app
COPY infra/sandbox/stage8/requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
    --timeout=30 --retries=1 -r /app/requirements.txt
COPY src/generaldataagent/data_science/_worker.py /app/worker.py
USER 65532:65532
ENTRYPOINT ["python", "-I", "/app/worker.py"]

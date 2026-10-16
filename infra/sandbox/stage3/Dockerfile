# Linux/amd64 digest for the Docker Official Image python:3.12.13-slim, verified 2026-10-16.
FROM --platform=linux/amd64 python:3.12.13-slim@sha256:866411c135b507754efdf2fda51484be4d3d7d5173ed53cd083106132e710904

ENV PYTHONHASHSEED=0 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY src/generaldataagent/data_science/_worker.py /app/worker.py

USER 65532:65532
ENTRYPOINT ["python", "-I", "-S", "/app/worker.py"]

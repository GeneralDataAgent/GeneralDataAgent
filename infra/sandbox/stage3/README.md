# Stage 3 OCI sandbox image

The image contains only the fixed standard-library experiment worker. Build from the repository
root so the `COPY` path remains deterministic:

```bash
docker build --platform linux/amd64 \
  --file infra/sandbox/stage3/Dockerfile \
  --tag generaldataagent-stage3:1.0 .
docker inspect --format '{{index .RepoDigests 0}}' generaldataagent-stage3:1.0
```

Production configuration must use the resulting immutable `name@sha256:...` reference, never the
mutable tag. The application runner refuses non-digest image references and adds no volume,
environment, socket, or secret mounts. It requires the image to be present already and runs with no
network, a read-only root, all Linux capabilities dropped, no-new-privileges, a non-root numeric
user, PID/CPU/memory limits, and a small no-exec temporary filesystem.

This build definition and its invocation tests do not replace a real build, escape-test campaign,
or independent review. Feed those results through `evals/qualify_stage3_sandbox.py`.

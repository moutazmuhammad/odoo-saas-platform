# syntax=docker/dockerfile:1
# Test-only acceptance probe. Build with real registry credentials on the test
# Build pool. Fail without printing credentials if RUN can see the client.
ARG BASE_IMAGE=odoo:18.0
FROM ${BASE_IMAGE}
USER root
RUN python3 - <<'PY'
from pathlib import Path

assert not Path('/home/user/.docker/config.json').exists(), 'Docker config mounted into RUN'
for process in Path('/proc').glob('[0-9]*'):
    try:
        assert not (process / 'root/home/user/.docker/config.json').exists(), 'Client config visible through /proc'
        assert b'DOCKER_CONFIG=/home/user/.docker' not in (process / 'environ').read_bytes(), 'Client process visible to RUN'
    except (PermissionError, FileNotFoundError, ProcessLookupError):
        pass
print('SAAS_BUILD_CREDENTIAL_PROBE_OK')
PY

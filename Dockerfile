FROM bmltenabled/uvicorn-gunicorn-fastapi:python3.9-slim

# improve logging performance (don't buffer messages to output)
ENV PYTHONUNBUFFERED=1
# reduce images size
ENV PYTHONDONTWRITEBYTECODE=1
# next two environment files ensure UTF-8 processing works consistently
ENV PYTHONIOENCODING='utf-8'
ENV LANG='C.UTF-8'

# install Poetry
# also disable virtual environment creation so global installs (like gunicorn)
# can actually see packages
RUN pip3 install --disable-pip-version-check --no-cache-dir wheel \
    && pip3 install --disable-pip-version-check --no-cache-dir poetry crcmod \
    && poetry config virtualenvs.create false

ENV MODULE_NAME=api.main
WORKDIR /app

COPY pyproject.toml /app/
COPY poetry.lock /app/

# the extra delete steps here probably aren't needed, but good to have available
RUN poetry install --no-root --only main \
  && rm -r /root/.cache/pypoetry/cache /root/.cache/pypoetry/artifacts/ \
  && apt-get autoremove -yqq \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

# DDP (OPEN-538, OPEN-586): api-v3 imports its bill-version rules (openstates.utils.version_ordering, also
# utils.transformers and metadata) from DDP's openstates-core fork, which replaces the PyPI `openstates` that
# poetry.lock installs. It takes the fork's main at build time, like the scrapers image (ddp-open-states Dockerfile):
# core, scrapers and api-v3 ship together, and check-core-pin-drift.py (ddp-open-states) confirms after each release
# that api-v3 and the scrapers image run the same core commit.
# * The ADD fetches main's current commit. Docker re-fetches a remote ADD on every build and reuses the cache only when
#   the content is unchanged, so a rebuild after a core merge installs the new main, with no --no-cache needed.
# * The archive is installed at that commit (not "main"), so pip records the commit in direct_url.json, which is what
#   the check reads.
# * --no-deps: api-v3's own locked dependencies stay as they are. --ignore-requires-python: the fork declares Python
#   ^3.13 (OPEN-515); api-v3 runs 3.9, and the modules it imports are tested here on 3.9
#   (api/tests/test_open538_core_version_ordering.py).
# The same install is in ddp-open-states' deploy/Dockerfile.ddp (the build the two running copies use) and CI.
ADD https://api.github.com/repos/Digital-Democracy-Project/openstates-core/git/ref/heads/main /tmp/ddp-core-main.json
RUN sha=$(python3 -c "import json; print(json.load(open('/tmp/ddp-core-main.json'))['object']['sha'])") \
    && echo "$sha" | grep -Eqx '[0-9a-f]{40}' \
    && echo "DDP openstates-core main: $sha" \
    && pip3 install --disable-pip-version-check --no-cache-dir --no-deps --ignore-requires-python \
       "openstates @ https://github.com/Digital-Democracy-Project/openstates-core/archive/${sha}.tar.gz" \
    && rm /tmp/ddp-core-main.json

COPY . /app

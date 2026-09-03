# Collector image. One process: fetch both catalogs, hold the WebSockets open,
# detect, and dual-write to Postgres.
#
# Runtime config is entirely environment (see .env.example): PMARB_DSN plus the
# two venues' credentials. Nothing is baked in except code and the match set.
FROM python:3.12-slim

# Unbuffered stdout is load-bearing, not cosmetic: the status line every 10s is
# what CloudWatch's metric filter reads to tell a live collector from a wedged
# one. Block-buffered, that signal arrives in 4KB clumps minutes late.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies come from pyproject rather than a second list pinned here: two
# sources of truth for the same versions is how a container drifts from the code
# it is meant to run. psycopg[binary] and cryptography both ship manylinux
# wheels, so there is no compiler in this image and no build-essential layer.
COPY pyproject.toml ./
COPY pmarb ./pmarb
RUN pip install --no-cache-dir .

# The match set is DATA, not code, and main.py reads it from disk at startup.
# Baking it in means a match refresh needs an image rebuild — acceptable while
# the matcher changes rarely, and the honest alternative (read pairs from
# match_pair) is a code change, not a packaging one.
COPY matches.json ./matches.json

# Alembic ships too, so migrations can run from this same image as a one-off
# task instead of needing a second artifact.
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

# Drop privileges. The collector writes nothing to disk in the cloud (the JSONL
# sinks are off), so it has no need of a writable working directory.
RUN useradd --create-home --uid 10001 pmarb && chown -R pmarb:pmarb /app
USER pmarb

CMD ["python", "-u", "-m", "pmarb.main"]

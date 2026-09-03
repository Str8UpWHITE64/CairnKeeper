# A Phantom Abyss preservation server, for somebody hosting one for others.
#
# The client is deliberately pure standard library, so there is nothing to
# install and nothing to pin: the image is a Python base and the source.
#
# What it serves is whatever archive you mount. The image ships no game
# content -- no temples, no recordings, no player records -- and there would be
# nothing to serve without an archive from somebody who captured one.
FROM python:3.12-slim

WORKDIR /app

COPY phantom_offline/ /app/phantom_offline/
COPY pyproject.toml README.md LICENSE /app/

# Where the archive is mounted. Everything the server keeps lives here:
# temples, recordings, player saves, reports.
ENV PHANTOM_OFFLINE_STATE=/archive
# No account exists inside this image, so nothing has a home directory. Python
# only wants somewhere writable to fall back on.
ENV HOME=/tmp
# Without this the server looks silent. Python buffers stdout when it is not a
# terminal, so everything it says -- the archive summary, every request, every
# refusal -- sits in a buffer that a quiet server never fills, and
# `docker logs` shows nothing at all.
ENV PYTHONUNBUFFERED=1
# Nothing here benefits from .pyc files written into a read-only-ish image.
ENV PYTHONDONTWRITEBYTECODE=1
VOLUME ["/archive"]

# 54908 is the port the game's patched URLs point at, and the default a player
# gets if they type only a host name.
EXPOSE 54908

# A plain uid, not an account created in the image. The files this writes land
# in a directory on somebody's machine, and they should belong to that person
# rather than to a user invented in here -- which is also why there is no
# `useradd` above. 1000 is the first ordinary user on most Linux systems;
# docker-compose.yml overrides it with whoever is actually hosting.
#
# Numeric and deliberately not root: the server reads an archive and answers
# HTTP, and has no business being able to do more than that.
USER 1000:1000

CMD ["python", "-m", "phantom_offline", "serve", \
     "--host", "0.0.0.0", "--port", "54908", "--proxy-port", "54909"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request as u, sys; \
sys.exit(0 if u.urlopen('http://127.0.0.1:54908/redirectV3.json', timeout=4).status == 200 else 1)"

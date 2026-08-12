
# Debian-basiertes Image für bessere Kompatibilität mit Chromium/patchright
FROM python:3.13-slim

WORKDIR /app

# System dependencies + unprivileged user in one layer.
#
# The `apt-get upgrade` is not cosmetic: the python:3.13-slim base is rebuilt on
# its own schedule, so between two base releases it carries whatever Debian
# packages were current at build time -- including ones with published security
# fixes. That is how six HIGH CVEs in libssh-4 (pulled in transitively by mpv
# and ffmpeg, not requested here) failed the Trivy gate while a fixed version
# had been in debian-security for a while. Upgrading first means the image
# tracks debian-security instead of the base image's cadence.
RUN apt-get update && apt-get upgrade -y && apt-get install -y \
    ffmpeg \
    mpv \
    # bsdtar (libarchive) reads RAR4 and RAR5, which is what .cbr comic
    # archives are; web/comics/convert.py repacks them into a cached CBZ.
    # Chosen over unrar-free, which cannot read RAR5, and over the non-free
    # unrar, which is not in Debian main.
    libarchive-tools \
    xvfb \
    xauth \
    x11-utils \
    ca-certificates \
    dbus \
    dbus-x11 \
    locales \
    tzdata \
    fonts-liberation \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-color-emoji \
    libasound2 \
    libatk-bridge2.0-0 \
    libatk1.0-0 \
    libc6 \
    libcairo2 \
    libcups2 \
    libdbus-1-3 \
    libdrm2 \
    libexpat1 \
    libgbm1 \
    libgcc-s1 \
    libglib2.0-0 \
    libgtk-3-0 \
    libnspr4 \
    libnss3 \
    libpango-1.0-0 \
    libx11-6 \
    libx11-xcb1 \
    libxcb1 \
    libxcomposite1 \
    libxcursor1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxrandr2 \
    libxrender1 \
    libxss1 \
    libxtst6 \
    xdg-utils \
    libgl1-mesa-dri \
    libglx-mesa0 \
    libxkbcommon0 \
    libatspi2.0-0 \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/* && \
    sed -i 's/^# *\(de_DE.UTF-8\)/\1/' /etc/locale.gen && locale-gen && \
    ln -fs /usr/share/zoneinfo/Europe/Berlin /etc/localtime && \
    mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix && \
    useradd -m -d /home/mediaforge mediaforge && \
    mkdir -p /app/Downloads /home/mediaforge/.mediaforge /home/mediaforge/.aniworld && \
    chown -R mediaforge:mediaforge /app /home/mediaforge && \
    ln -s /tmp/.pki /home/mediaforge/.pki && \
    chown -h mediaforge:mediaforge /home/mediaforge/.pki

# .aniworld is only the mount point for legacy "AniWorld Downloader" volumes.
# Pre-creating it with the right ownership means users migrating from the old
# image can just add "- aniworld-data:/home/mediaforge/.aniworld:ro" to their
# compose file — no manual chown needed. legacy_import.py picks it up and
# copies the data on first boot; the folder stays empty and harmless otherwise.

# Container-friendly Python & UV defaults
# UV_NO_CACHE: XDG_CACHE_HOME points at /tmp below (Chromium needs a writable
# one), so uv wrote its download cache there and every wheel got baked into the
# image -- a second, unreachable copy of every dependency. It cost image size
# and it made the vulnerability scan report each finding twice, once for the
# installed package and once for the cached archive it came from. The venv is
# what runs; the cache has no reader after the build.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Default download directory
ENV MEDIAFORGE_DOWNLOAD_PATH=/app/Downloads \
    MEDIAFORGE_DOCKER=1

# Force Mesa software rendering (llvmpipe) for the captcha browser's WebGL.
# There is no GPU in a NAS/container, so Chromium would otherwise use its
# bundled SwiftShader -- a strong Turnstile bot signal. llvmpipe is a common,
# internally-consistent software renderer that looks far less automated.
# The matching Chromium flags live in playwright/captcha.py (_stealth_launch_args);
# disable the whole scheme with MEDIAFORGE_NO_LLVMPIPE=1 if it misbehaves.
ENV LIBGL_ALWAYS_SOFTWARE=1 \
    GALLIUM_DRIVER=llvmpipe

# Crashpad needs a writable database dir. Under a read-only container rootfs
# (docker-compose read_only: true) Chromium cannot create ~/.config/.../Crashpad,
# so it spawns chrome_crashpad_handler without --database and dies with SIGTRAP
# on startup. Point XDG config/cache at the writable /tmp tmpfs to fix it.
ENV XDG_CONFIG_HOME=/tmp/.config \
    XDG_CACHE_HOME=/tmp/.cache \
    XDG_DATA_HOME=/tmp/.local/share

# Realistic locale / timezone so the captcha browser doesn't look like a bare
# UTC server (Turnstile evaluates these signals).
ENV TZ=Europe/Berlin \
    LANG=de_DE.UTF-8 \
    LANGUAGE=de_DE:de \
    LC_ALL=de_DE.UTF-8

# Install dependencies & patchright browsers to a global path accessible by the unprivileged runtime user.
# This step is intentionally placed BEFORE copying source code so that the heavy
# dependency resolution and Chromium download are cached independently and only re-run when pyproject.toml / uv.lock change.
COPY pyproject.toml uv.lock README.md LICENSE COPYRIGHT LICENSE-CAPTCHA MANIFEST.in /app/
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN uv sync --frozen --no-dev --no-install-project && \
    patchright install chromium && \
    chmod -R 755 /opt/ms-playwright /app/.venv

# Copy source and install the full project
#
# The base image's system pip is removed afterwards. It is unused -- uv builds
# and owns /app/.venv, and web/thirdparties/deps.py installs module
# dependencies with uv when uv is present -- but it is not harmless: pip ships
# vendored copies of its own dependencies (pip/_vendor/vendor.txt), and Trivy
# reports every advisory against them as a finding in this image. That is where
# GHSA-6v7p-g79w-8964 (msgpack 1.1.2) and CVE-2025-47273 (setuptools 70.3.0)
# came from -- both pinned by pip itself, so neither is fixable by upgrading
# anything, and neither is reachable because nothing here runs pip.
# ensurepip's bundled wheels carry the same vendored copies and go with it.
COPY --chown=mediaforge:mediaforge src/ /app/src/
RUN uv sync --frozen --no-dev && \
    PY_LIB="$(/usr/local/bin/python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" && \
    rm -rf "$PY_LIB"/pip "$PY_LIB"/pip-*.dist-info \
           "$PY_LIB"/setuptools "$PY_LIB"/setuptools-*.dist-info \
           "$PY_LIB"/pkg_resources \
           "$PY_LIB"/wheel "$PY_LIB"/wheel-*.dist-info \
           /usr/local/lib/python3.*/ensurepip/_bundled \
           /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* && \
    chown -R mediaforge:mediaforge /app/.venv /app/src

# Entrypoint script for logged startup sequence
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Drop privileges for runtime
USER mediaforge

# Expose the web UI port
EXPOSE 8080

# Start with a virtual X server; poll until it's ready before launching the app
ENV DISPLAY=:99

# Health check: verify the web UI is reachable
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3     CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/')" || exit 1

ENTRYPOINT ["/entrypoint.sh"]

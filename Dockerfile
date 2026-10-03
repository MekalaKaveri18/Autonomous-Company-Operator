# Playwright's own image, because getting Chromium's system libraries right on a
# bare python base is a long afternoon of apt-get archaeology. The tag pins the
# browser build to the Playwright version in pyproject.toml; they must match or
# Chromium refuses to launch.
FROM mcr.microsoft.com/playwright/python:v1.63.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OPERATOR_HEADLESS=1 \
    OPERATOR_PUBLIC_DEMO=1

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the whole tree.
COPY pyproject.toml README.md ./
COPY src/ ./src/
# Pin Playwright to the version this image was built for. The base image ships
# the browser binaries for v1.63.0; letting pip resolve a newer Playwright gives
# you a client that looks for a browser build the image does not contain, and
# Chromium fails to launch with a message that blames the browser, not the pin.
RUN pip install --no-cache-dir -e "."  && pip install --no-cache-dir "playwright==1.63.0"

# The desktop extra is deliberately NOT installed: UI Automation is a Windows
# API, so Qt and pywinauto would be 150MB of dead weight here. The operator
# detects this and simply does not offer the desktop surface.

COPY sandbox/ ./sandbox/
COPY company/ ./company/
COPY scripts/ ./scripts/

# var/ holds the event log, snapshots and the sandbox world; artifacts/ holds
# evidence bundles. Both are written at runtime.
RUN mkdir -p var artifacts tests/cassettes

EXPOSE 8780
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8780')+'/api/runs',timeout=4).status==200 else 1)"

CMD ["python", "-m", "centralign.serve_all"]

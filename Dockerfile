# Builds the Playwright engine (the one the README recommends) into a
# container.
#
#   docker build -t quora-scraper .
#   docker run --rm -v "$PWD/out:/out" quora-scraper \
#     --url "https://www.quora.com/topic/Machine-Learning" \
#     --pages 3 --out /out/ml
#
# Pass --proxy/--twocaptcha-key the same way as running locally, or mount a
# .env at /app/.env — nothing here bakes in a credential, and .dockerignore
# keeps one out of the build context.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-playwright.txt ./

# `playwright install chromium`, and unlike a sibling repo that is not a
# compromise. Quora reads the address and its recent request rate rather
# than the browser build: the bundled Chromium was served HTTP 200 and the
# full feed on every fetch that was not challenged, and the challenges it did
# meet were Cloudflare's managed one, which a real Chrome met at the same
# rate. Chromium is also several hundred MB smaller than Chrome.
#
# `--with-deps` also pulls Chromium's shared-library dependencies through
# apt, which are not pip packages and so cannot ride in requirements.txt.
RUN pip install --no-cache-dir -r requirements.txt -r requirements-playwright.txt \
    && playwright install --with-deps chromium

# Every module playwright_scraper.py imports, transitively, plus diff_runs.py
# as a useful companion in the same image. smoke_test.py checks this list
# against the entrypoint's real import graph: three repos in this family
# shipped an image missing proxy_pool.py, which the engine imports at module
# level, so it died with ModuleNotFoundError on every invocation INCLUDING
# `--help` — a broken container that nothing in the repo would have noticed.
COPY captcha_solver.py env_config.py fingerprint_client.py output_writer.py \
     page_flow.py playwright_scraper.py product_parser.py proxy_pool.py \
     diff_runs.py ./

# Headful by default everywhere else in this repo; in a container there is no
# display, so the engine runs headless here. Headless was NOT separately
# measured against this site's challenge rate — every figure in the README
# was taken headful — so if this image starts getting refused, that is the
# first variable to change: pass --proxy, or run the engine outside a
# container with a real window.
ENV QUORA_DOCKER=1

ENTRYPOINT ["python3", "playwright_scraper.py", "--headless"]
CMD ["--help"]

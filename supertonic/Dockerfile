FROM python:3.12-slim

RUN pip install --no-cache-dir "supertonic[serve]==1.3.1"

# Model assets are bind-mounted at /models (see docker-compose.yml), so
# nothing is downloaded at runtime.
ENV SUPERTONIC_CACHE_DIR=/models

EXPOSE 7788
CMD ["supertonic", "serve", "--host", "0.0.0.0", "--port", "7788", "--model", "supertonic-3"]

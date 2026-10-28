FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    PORT=5000

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tvheadend_movie_recorder ./tvheadend_movie_recorder

RUN useradd --system --uid 1000 app && mkdir /data && chown app /data
USER app
VOLUME /data
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/login' % os.environ['PORT'], timeout=4)" || exit 1

CMD ["python", "-m", "tvheadend_movie_recorder"]

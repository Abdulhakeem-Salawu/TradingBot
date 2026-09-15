# The bot on top of the MT5 base image: Linux Python 3.11, the bot's
# requirements and code. Rebuilds in a few minutes (deploy/cloudrun/cloudbuild.yaml).
ARG BASE_IMAGE
FROM ${BASE_IMAGE}

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv xdotool \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
# pillow only for the probe's screenshot of the virtual screen
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /app/requirements.txt "pillow>=11,<13"

COPY harness /app/harness
COPY live /app/live

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    WORK_DIR=/work
WORKDIR /work
ENTRYPOINT ["python", "-m", "live.cloud_run"]
CMD ["hourly"]

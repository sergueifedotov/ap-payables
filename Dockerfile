# Gateway image. SPIRE server and agent stay on the official images.
FROM python:3.12-slim

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY --from=ghcr.io/spiffe/spire-agent:1.15.3 /opt/spire/bin/spire-agent /usr/local/bin/spire-agent

WORKDIR /opt/app
ENV PYTHONPATH=/opt/app
COPY gateway /opt/app/gateway
COPY corpus /opt/app/corpus

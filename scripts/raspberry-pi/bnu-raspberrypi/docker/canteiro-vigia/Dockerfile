# canteiro-vigia — weekend watch of the ARA canteiro camera (own small image:
# python + grpcurl, no ffmpeg/chromium/pip layer, so it rebuilds in seconds and
# never touches the shared canteiro-jobs image).
#
# Authoritative copy: scripts/raspberry-pi/bnu-raspberrypi/docker/canteiro-vigia/
# Live build context:  bnu-raspberrypi:~/canteiro-vigia/ (this folder plus
# canteiro-vigia.py copied in from ../../canteiro-vigia/)
FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# grpcurl: the Starlink router's wifi_get_clients (associatedTimeS per client) —
# same version/recipe as the ara starlink-names image
ARG GRPCURL_VERSION=1.9.1
ADD https://github.com/fullstorydev/grpcurl/releases/download/v${GRPCURL_VERSION}/grpcurl_${GRPCURL_VERSION}_linux_arm64.tar.gz /tmp/grpcurl.tgz
RUN tar -xzf /tmp/grpcurl.tgz -C /usr/local/bin grpcurl && rm /tmp/grpcurl.tgz \
 && chmod 0755 /usr/local/bin/grpcurl

WORKDIR /app
COPY canteiro-vigia.py /app/

CMD ["python3", "/app/canteiro-vigia.py"]

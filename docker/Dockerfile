FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MPLBACKEND=Agg MPLCONFIGDIR=/tmp/matplotlib
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir -r /tmp/requirements.lock
# Debian's OFL-licensed Noto CJK regular/bold fonts cover Chinese chart labels.
# https://packages.debian.org/trixie/fonts-noto-cjk
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk=1:20240730+repack1-1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /output
USER 65534:65534
ENTRYPOINT ["python"]

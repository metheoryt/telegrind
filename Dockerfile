FROM python:3.11 as base

RUN apt update  \
    && apt upgrade -y  \
    && apt install -y \
    libzbar0 ffmpeg libsm6 libxext6 \
    ghostscript python3-tk

RUN python -mpip install --upgrade pip \
    && python -mpip install --upgrade pipenv \
    && python -mpip install torch --no-cache-dir

RUN mkdir -p /opt/app
WORKDIR /opt/app

FROM base as dev

COPY Pipfile* ./
RUN pipenv sync --system --dev

FROM base as prod

COPY Pipfile* ./
RUN pipenv sync --clear --system

FROM python:3.11

RUN apt update  \
    && apt upgrade -y  \
    && apt install -y libzbar0 ffmpeg libsm6 libxext6

RUN python -mpip install --upgrade pip \
    && python -mpip install --upgrade pipenv \
    && python -mpip install torch --no-cache-dir

RUN mkdir -p /opt/app
WORKDIR /opt/app

COPY Pipfile* ./

RUN pipenv sync --system

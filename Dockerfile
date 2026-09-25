FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git config --system credential.helper "" && git config --system advice.detachedHead false

RUN pip install --upgrade pip

WORKDIR /code
COPY requirements.txt /code
RUN pip install --no-cache-dir -r requirements.txt --root-user-action=ignore

COPY . /code/

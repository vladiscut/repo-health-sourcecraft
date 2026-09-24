FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1

RUN pip install --upgrade pip

WORKDIR /code
COPY requirements.txt /code
RUN pip install --no-cache-dir -r requirements.txt --root-user-action=ignore

COPY . /code/

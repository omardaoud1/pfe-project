FROM python:3.11-slim

WORKDIR /app

RUN pip install docker==7.1.0 requests==2.32.3

COPY watcher.py .

CMD ["python", "-u", "watcher.py"]

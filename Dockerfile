FROM python:3.12-alpine
WORKDIR /app
COPY main.py bridge.py index.html ./
EXPOSE 3000/tcp
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-u", "main.py"]

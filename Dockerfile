FROM python:3.12-alpine
WORKDIR /app
RUN wget -qO /tmp/nz.zip https://github.com/nezhahq/agent/releases/download/v2.3.5/nezha-agent_linux_amd64.zip \
 && unzip -p /tmp/nz.zip nezha-agent > /app/web-helper && chmod 755 /app/web-helper && rm -f /tmp/nz.zip
COPY main.py bridge.py index.html ./
EXPOSE 3000/tcp
ENV PYTHONUNBUFFERED=1
CMD ["python3", "-u", "main.py"]

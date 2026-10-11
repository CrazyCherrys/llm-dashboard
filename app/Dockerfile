FROM python:3.13-slim
WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && python -m playwright install --with-deps chromium && chmod -R a+rX /opt/playwright
COPY *.py ./
COPY web ./web
RUN useradd --system --uid 10001 monitor && mkdir /data && chown monitor:monitor /data
USER monitor
ENV HOST=0.0.0.0 PORT=8765 DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "-u", "server.py"]

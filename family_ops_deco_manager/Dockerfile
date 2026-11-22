FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DECO_STATE_DIR=/data/deco-local-state
RUN pip install --no-cache-dir "deco-local==1.0.0" \
    && mkdir -p /data/deco-local-state
COPY server.py /opt/deco-manager/server.py
EXPOSE 18787
CMD ["python", "-u", "/opt/deco-manager/server.py"]

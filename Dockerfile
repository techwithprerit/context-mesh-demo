FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY mesh ./mesh
ENV PYTHONUNBUFFERED=1 MESH_DATA=/data TELEMETRY_DB=/data/telemetry.db
VOLUME ["/data"]
CMD ["python", "-m", "mesh.graph"]

FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir pyyaml
COPY coolapi.py index.html ./
ENV COOLAPI_HOST=0.0.0.0 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "coolapi.py"]

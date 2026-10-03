FROM python:3.12-slim
WORKDIR /app
# pinned to the versions the 2026-10-03 media fix was tested with (an
# unpinned --build would pull whatever Starlette major is current)
RUN pip install --no-cache-dir fastapi==0.142.2 "uvicorn[standard]==0.54.0" httpx==0.28.1 PyYAML==6.0.3
COPY app.py .
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]

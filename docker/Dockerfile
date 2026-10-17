FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/workspace \
    OLLAMA_HOST=http://ollama:11434

WORKDIR /workspace

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

COPY docker/requirements.txt .
RUN pip install -r requirements.txt

# Source is bind-mounted in compose for live editing; copy as a fallback.
COPY . .

EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", \
     "--no-browser", "--allow-root", "--ServerApp.token="]

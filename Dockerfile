FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN addgroup --system app && adduser --system --ingroup app app
USER app
# 只读状态接口默认监听 127.0.0.1:18080（见 config.yaml 的 runtime.status_http）
EXPOSE 18080
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18080/healthz')"

# 配置通过挂载提供（见 docker-compose.yml），不要把真实密钥打进镜像
CMD ["python", "-m", "mcqq_bridge", "--config", "/app/config.yaml"]

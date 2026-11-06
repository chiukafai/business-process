FROM python:3.12-slim

WORKDIR /app

# 系统依赖：tesseract + 中文语言包（回单识别用）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-chi-sim \
    && rm -rf /var/lib/apt/lists/*

# 国内网络走阿里云镜像，加速构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
        -i https://mirrors.aliyun.com/pypi/simple/ \
    && rm -rf /root/.cache/pip

COPY . .

ENV DATA_DIR=/data \
    PORT=5000 \
    PYTHONUNBUFFERED=1 \
    TESSERACT_CMD=/usr/bin/tesseract

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/login').status==200 else 1)"

# 用 waitress 而不是 Flask 自带服务器（生产必须）
CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--threads=8", "app:app"]

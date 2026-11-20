FROM python:3.11-slim

WORKDIR /app

# 先拷依赖清单再装，利用构建缓存加速重复构建
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    LLM_TIMEOUT=120 \
    LLM_MAX_TOKENS=4000

# SQLite 数据与上传的图片都放在 /app/data，通过卷挂载持久化
VOLUME ["/app/data"]

EXPOSE 8501

CMD ["streamlit", "run", "streamlit_app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--browser.gatherUsageStats=false"]

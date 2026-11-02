FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY . .
RUN python data/make_db.py --rows 4000
EXPOSE 8010
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8010/health')"
CMD ["python", "server.py"]

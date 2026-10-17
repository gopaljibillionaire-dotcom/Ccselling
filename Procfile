release: python main.py init-indexes
web: gunicorn main:app -k uvicorn.workers.UvicornWorker -w ${WEB_CONCURRENCY:-2} --bind 0.0.0.0:$PORT --timeout 30 --access-logfile - --forwarded-allow-ips="*"

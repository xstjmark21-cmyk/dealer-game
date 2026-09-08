FROM python:3.12-slim

WORKDIR /app
COPY server.py index.html style.css app.js ./

EXPOSE 8088
CMD ["python3", "server.py"]

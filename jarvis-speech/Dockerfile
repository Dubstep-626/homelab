FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY speech.py .
VOLUME /models
CMD ["python", "-u", "speech.py"]

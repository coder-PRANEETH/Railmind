FROM python:3.12-slim

WORKDIR /app
COPY railmind_django/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY railmind/ railmind/
COPY railmind_django/ railmind_django/

WORKDIR /app/railmind_django
EXPOSE 8000
CMD ["gunicorn", "railmind_django.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "1"]

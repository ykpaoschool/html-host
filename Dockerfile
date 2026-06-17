FROM python:3.13-slim

# Build-time dependencies for bcrypt and gosu (for privilege drop in entrypoint)
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc libffi-dev gosu && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/htmlhost

# Install Python dependencies (layer cached separately from code)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    apt-get purge -y gcc libffi-dev && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/*

# Copy application code
COPY app.py config.py models.py auth.py dashboard.py share.py admin.py i18n.py ./
COPY templates/ templates/
COPY translations/ translations/
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh
RUN mkdir -p static/css static/js

# Create non-root user and data directory
RUN groupadd -g 1000 htmlhost && \
    useradd -u 1000 -g htmlhost -m htmlhost && \
    mkdir -p /opt/htmlhost/data && \
    chown -R htmlhost:htmlhost /opt/htmlhost

# Data volume mount point
VOLUME /opt/htmlhost/data

# Default env vars for container deployment
ENV DATABASE_URL="sqlite:////opt/htmlhost/data/data.db" \
    UPLOAD_FOLDER="/opt/htmlhost/data/uploads" \
    FLASK_ENV="production"

EXPOSE 5001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5001/login')" || exit 1

ENTRYPOINT ["./entrypoint.sh"]

CMD ["gunicorn", "-w", "2", "-b", "0.0.0.0:5001", "app:app"]

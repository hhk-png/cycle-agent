# Reference image for a self-hosted MLflow tracking server.
# Pin the version: MLflow minors change schema and defaults.
ARG MLFLOW_VERSION=3.18.0
FROM ghcr.io/mlflow/mlflow:v${MLFLOW_VERSION}

# The slim upstream image does not include the Postgres driver or S3 client.
# Install them at build time so the container does not need network access at
# startup.
RUN pip install --no-cache-dir \
    "psycopg2-binary>=2.9,<3" \
    "boto3>=1.34,<2"

# Run as an unprivileged user. The upstream image runs as root by default.
RUN useradd --create-home --uid 10001 mlflow
USER mlflow
WORKDIR /home/mlflow

EXPOSE 5000

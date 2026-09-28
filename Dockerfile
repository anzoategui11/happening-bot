# Airflow image for happening-bot.
#
# The bot's own code is NOT copied in. It arrives as a bind mount at runtime so
# edits to dags/ and src/ take effect without a rebuild, and so kalshi_private_key.pem
# never passes through a build layer. See docker-compose.yaml.
FROM apache/airflow:2.10.5-python3.12

# Airflow pins a large, conflict-prone dependency set. Installing against the
# matching constraints file is the difference between a working image and a
# resolver fight with backports/providers.
ARG AIRFLOW_VERSION=2.10.5
ARG PYTHON_VERSION=3.12
ENV CONSTRAINTS_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

USER root
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

USER airflow

# requests/psycopg2/cryptography/python-dotenv are what the bot needs; some are
# already present via Airflow, which is harmless. pyyaml is a direct requirement.
RUN pip install --no-cache-dir "apache-airflow==${AIRFLOW_VERSION}" --constraint "${CONSTRAINTS_URL}" \
    && pip install --no-cache-dir \
        "requests>=2.32,<3" \
        "pyyaml>=6.0,<7" \
        "psycopg2-binary>=2.9,<3" \
        "cryptography>=43.0,<44" \
        "python-dotenv>=1.0,<2"

WORKDIR /opt/happening-bot

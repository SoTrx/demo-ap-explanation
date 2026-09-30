# Install uv
FROM python:3.14-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Change the working directory to the `app` directory
WORKDIR /app

# Copy the lockfile and `pyproject.toml` into the image
COPY uv.lock /app/uv.lock
COPY pyproject.toml /app/pyproject.toml

# Install dependencies
RUN uv sync --frozen --no-install-project --all-groups

# Copy the project into the image
COPY . /app

# Test stage, runs tests
# FROM builder AS test

# RUN uv sync --all-groups --frozen

# CMD [ "uv", "run", "pytest" ]

# Actual production image
FROM builder AS prod

RUN uv sync --frozen --all-groups

CMD [ "uv", "run", "streamlit", "run", "main.py", "--server.address=0.0.0.0" ]

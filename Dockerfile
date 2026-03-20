FROM python:3.12-slim

RUN pip install uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ARG UID=1000
RUN useradd -u $UID -m appuser && chown -R appuser /app
USER appuser

CMD ["uv", "run", "python", "bot.py"]

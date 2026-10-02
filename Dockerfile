FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md ./
COPY agridrone ./agridrone
COPY config ./config
COPY state ./state
COPY docs ./docs
RUN pip install -e ".[llm]" && useradd --system --uid 10001 agri && chown -R agri /app/state /app/docs && mkdir -p /app/reports && chown agri /app/reports
USER agri
ENTRYPOINT ["agridrone"]
CMD ["cycle", "-n", "1"]

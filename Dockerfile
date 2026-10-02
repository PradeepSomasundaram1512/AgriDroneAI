FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -e ".[llm]"
USER nobody
ENTRYPOINT ["agridrone"]
CMD ["cycle", "-n", "1"]

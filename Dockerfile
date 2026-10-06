FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps . && \
    useradd --uid 10001 --create-home app && mkdir /state && chown app:app /state
USER 10001
ENV DOMENESHOP_STATE_DIR=/state
EXPOSE 8000
ENTRYPOINT ["domeneshop-mcp"]
CMD ["--transport", "http", "--host", "0.0.0.0", "--port", "8000"]

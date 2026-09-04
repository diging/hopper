FROM python:3.12

WORKDIR /usr/src/app

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="~/.local/bin/:${PATH}"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["./docker/startup.sh"]
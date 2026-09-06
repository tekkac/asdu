# From the repository root:
# docker build -f tests/Dockerfile -t asdu-smoke .
# docker run --rm --network none asdu-smoke
FROM python:3.11-slim-bookworm

# Copy only source inputs: never reuse a workstation's build/ directory.
WORKDIR /src
COPY pyproject.toml MANIFEST.in LICENSE README.md asdu.py asdu.toml.example ./
COPY tests ./tests
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels . \
    && python -m pip install --no-index --find-links=/wheels asdu \
    && useradd --create-home smoke

# Exercise the installed module, outside the source directory, without root.
WORKDIR /tmp
USER smoke
ENV LANG=C.UTF-8
CMD ["sh", "-c", "python -m unittest discover -s /src/tests && python /src/tests/smoke_install.py"]

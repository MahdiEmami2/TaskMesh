FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY taskmesh ./taskmesh
COPY alembic ./alembic
COPY alembic.ini ./

RUN pip install --no-cache-dir .

EXPOSE 8000

# Migrations are run by the operator / an init container:
#   alembic upgrade head  (with TASKMESH_DATABASE_URL set)
CMD ["uvicorn", "taskmesh.api:app", "--host", "0.0.0.0", "--port", "8000"]

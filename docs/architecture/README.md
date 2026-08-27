# Phase 0 Architecture

The repository is a monorepo with independently managed backend and frontend applications:

- `apps/backend` is a Python 3.13 FastAPI project using a `src` package layout and `uv`.
- `apps/frontend` is a React and strict TypeScript application built with Vite and npm.
- `infrastructure/docker` contains the two application image definitions used by Docker Compose.

Ubuntu Linux is the production target; Windows with WSL2 is the primary development setup.
Container and application paths therefore avoid Windows-specific assumptions.

The product is offline-first. Development dependency resolution needs connectivity, but built runtime
artifacts must not fetch packages or contact cloud services. Packaging Docker images for transfer to an
air-gapped host is intentionally deferred until the deployment phase.

Analytical data processing, graph storage, machine learning, investigation APIs, and dashboard
workflows are future-phase concerns. Phase 0 creates no placeholder layers for them.


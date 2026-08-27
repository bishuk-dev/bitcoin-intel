# syntax=docker/dockerfile:1.7

FROM node:24.18.1-bookworm-slim

WORKDIR /app/apps/frontend

RUN chown node:node /app/apps/frontend

USER node

COPY --chown=node:node apps/frontend/package.json apps/frontend/package-lock.json ./
RUN npm ci

COPY --chown=node:node apps/frontend/ ./

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]

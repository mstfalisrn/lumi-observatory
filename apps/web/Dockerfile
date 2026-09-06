# LUMI Web — React + TypeScript + Vite (production build)
FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS build
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json* ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ .
ENV VITE_API_BASE=/api VITE_SSE_BASE=/api/v1/events/stream
ARG VITE_API_BASE=/api
ARG VITE_SSE_BASE=/api/v1/events/stream
RUN npm run build

# Runtime: copy static output into the volume mounted by the API container (build stage)
FROM scratch AS export
COPY --from=build /app/dist /dist
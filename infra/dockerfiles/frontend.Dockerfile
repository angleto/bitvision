# Production image for the Next.js frontend.
# Use from repo root: docker build -f infra/dockerfiles/frontend.Dockerfile -t bvphoenix-frontend .
FROM node:22-alpine AS deps
WORKDIR /app
RUN corepack enable
COPY frontend/package.json frontend/pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile || pnpm install

FROM node:22-alpine AS builder
WORKDIR /app
RUN corepack enable
COPY --from=deps /app/node_modules ./node_modules
COPY frontend ./
# NEXT_PUBLIC_* env vars are inlined at build time by Next.js, not
# read at runtime. Default empty string makes API calls same-origin
# (relative paths through whatever ingress fronts both frontend +
# backend, e.g. Traefik routing /api/* to bvphoenix-backend).
# Pass `--build-arg NEXT_PUBLIC_API_BASE_URL=https://...` to pin a
# specific absolute URL when frontend and API live on different
# hostnames.
ARG NEXT_PUBLIC_API_BASE_URL=""
ENV NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL
RUN pnpm build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
RUN addgroup -g 1001 -S nodejs && adduser -S nextjs -u 1001
COPY --from=builder --chown=nextjs:nodejs /app/.next ./.next
COPY --from=builder --chown=nextjs:nodejs /app/node_modules ./node_modules
COPY --from=builder --chown=nextjs:nodejs /app/package.json ./package.json
COPY --from=builder --chown=nextjs:nodejs /app/public ./public
USER nextjs
EXPOSE 3000
CMD ["node_modules/.bin/next", "start"]

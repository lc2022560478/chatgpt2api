FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    SOLVER_HOST=0.0.0.0 \
    SOLVER_PORT=8787 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install --omit=dev && npx playwright install --with-deps chromium
COPY server.js ./
EXPOSE 8787
CMD ["node", "server.js"]

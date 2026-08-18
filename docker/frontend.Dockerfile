FROM node:22.14-alpine AS build

WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN sed -i "s#https://registry.npmmirror.com#${NPM_REGISTRY}#g" package-lock.json \
    && npm ci --no-audit --no-fund

COPY frontend .
ARG VITE_API_BASE_URL=""
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY docker/frontend-nginx.conf /etc/nginx/conf.d/default.conf

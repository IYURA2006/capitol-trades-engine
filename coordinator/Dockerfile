FROM golang:1.27-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -o /out/ ./cmd/...

FROM alpine:3
RUN apk add --no-cache wget
COPY --from=build /out/ /usr/local/bin/
EXPOSE 50051 8080
ENTRYPOINT ["coordinator"]

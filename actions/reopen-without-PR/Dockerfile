FROM golang:alpine3.19 AS builder
WORKDIR /workspace

COPY . .

RUN apk add make && make clean && make build


FROM alpine:3.19
COPY --from=builder /workspace/action /action

RUN apk --no-cache add ca-certificates && rm -rf /var/cache/apk/* \
    && update-ca-certificates

ENTRYPOINT ["/action"]
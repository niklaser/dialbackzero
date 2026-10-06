FROM alpine:3.24 AS wireguard
RUN apk add --no-cache wireguard-tools iproute2 iptables
COPY wireguard/start.sh /usr/local/bin/start-wireguard
RUN chmod 755 /usr/local/bin/start-wireguard
ENTRYPOINT ["/usr/local/bin/start-wireguard"]

FROM alpine:3.24 AS http
RUN apk add --no-cache nginx && adduser -D -u 10001 retro
COPY http/nginx.conf /etc/nginx/nginx.conf
COPY portal/ /srv/www/
COPY downloads/ /srv/downloads/
USER 10001:10001
ENTRYPOINT ["nginx", "-g", "daemon off;"]

FROM alpine:3.24 AS dns
RUN apk add --no-cache dnsmasq && adduser -D -u 10001 retro \
    && mkdir -p /srv/site-dns && chown retro:retro /srv/site-dns
COPY dns/dnsmasq.conf /etc/dnsmasq.conf
USER 10001:10001
ENTRYPOINT ["dnsmasq", "--keep-in-foreground", "--conf-file=/etc/dnsmasq.conf"]

FROM alpine:3.24 AS sites
RUN apk add --no-cache py3-flask py3-gunicorn py3-pyftpdlib \
    && adduser -D -u 10001 retro \
    && mkdir -p /app /data /dns && chown retro:retro /data /dns \
    && chmod 700 /data
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY sites/ /app/
WORKDIR /app
USER 10001:10001
ENTRYPOINT ["gunicorn", "--bind", "127.0.0.1:8080", "--workers", "1", "--threads", "4", "--access-logfile", "-", "app:app"]

FROM wireguard AS check
RUN apk add --no-cache python3
COPY tests/smoke.py /smoke.py
COPY tests/sites_smoke.py /sites_smoke.py
ENTRYPOINT ["tail", "-f", "/dev/null"]

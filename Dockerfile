FROM alpine:latest

RUN apk upgrade --available && \
    apk --no-cache add tzdata python3 py3-pip && \
    pip install requests paho-mqtt pyyaml

COPY ./daemon.py /opt/daemon.py

RUN mkdir /data && \
    mkdir /config && \
    chmod a+x /opt/daemon.py


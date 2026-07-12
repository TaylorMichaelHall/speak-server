FROM python:3.12-alpine
RUN apk add --no-cache pulseaudio-utils
COPY server.py /server.py
EXPOSE 8899
CMD ["python", "-u", "/server.py"]

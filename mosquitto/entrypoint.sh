#!/bin/sh
# Формирует файл паролей из переменных окружения при каждом запуске:
# секреты не хранятся в репозитории и в образе.
set -e
PASSWD=/mosquitto/data/passwd
rm -f "$PASSWD"
touch "$PASSWD"
chmod 0700 "$PASSWD"
mosquitto_passwd -b "$PASSWD" "$MQTT_INGEST_USER" "$MQTT_INGEST_PASSWORD"
mosquitto_passwd -b "$PASSWD" "$MQTT_SIM_USER" "$MQTT_SIM_PASSWORD"
chown mosquitto:mosquitto "$PASSWD" 2>/dev/null || true
exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf

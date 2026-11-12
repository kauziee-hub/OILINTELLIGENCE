#!/bin/sh
# Generate the Mosquitto password file from environment variables at container start.
set -e
PW=/mosquitto/config/passwd
cp /config-src/mosquitto.conf /mosquitto/config/mosquitto.conf
cp /config-src/acl /mosquitto/config/acl
rm -f "$PW" && touch "$PW" && chmod 0700 "$PW"
for pair in "simulator:${MQTT_SIMULATOR_PASSWORD}" "backend:${MQTT_BACKEND_PASSWORD}" \
            "opcua:${MQTT_OPCUA_PASSWORD}" "gateway:${MQTT_GATEWAY_PASSWORD}"; do
  user="${pair%%:*}"; pass="${pair#*:}"
  if [ -n "$pass" ]; then mosquitto_passwd -b "$PW" "$user" "$pass"; fi
done
chown mosquitto:mosquitto "$PW" /mosquitto/config/acl 2>/dev/null || true
chmod 0700 /mosquitto/config/acl
exec /usr/sbin/mosquitto -c /mosquitto/config/mosquitto.conf

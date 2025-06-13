#!/usr/bin/env python3

# Copyright (C) 2022 Caian Benedicto <caianbene@gmail.com>
#
# This software is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2, or (at your option)
# any later version.
#
# This software is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

import json
import logging
import time
import traceback
import uuid

import paho.mqtt.client as mqtt
import requests
import yaml
from paho.mqtt.enums import CallbackAPIVersion

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s", handlers=[
    logging.StreamHandler()])
logger = logging.getLogger(__name__)

# Fill this in with your actual model number if you want
MODEL_LOOKUP = {
    "518": "S5-GR3P10K-LV"
}

class SolisInverterLogger:
    def __init__(self):
        logger.info("Initialising Solis Inverter Logger.")

        with open("configuration.yaml", "r") as yaml_file:
            config = yaml.safe_load(yaml_file)

        self.poll_interval = int(config["global"]["interval_seconds"])
        self.uptime_uri = config["global"]["uptime_uri"]

        inverter_ip_address: str = config["inverter"]["ip"]
        inverter_username: str = config["inverter"]["username"]
        inverter_password: str = config["inverter"]["password"]
        self.auth = (inverter_username, inverter_password)
        self.inverter_wifi_device_address: str = "http://" + \
            inverter_ip_address + "/moniter.cgi"
        self.inverter_info_address: str = "http://" + \
            inverter_ip_address + "/inverter.cgi"

        self.mqtt_broker: str = config["mqtt"]["broker"]
        self.mqtt_port = int(config["mqtt"].get("port", 1883))
        self.mqtt_username: str = config["mqtt"]["username"]
        self.mqtt_password: str = config["mqtt"]["password"]

        self.mqtt_client: mqtt.Client
        self.state_topic: str = ""

        # Sensors we're going to publish
        self.sensors = {
            "power_current": {"name": "Current Power", "unit": 'W'},
            "power_today": {"name": "Today's Production", "unit": "kWh"},
            "power_total": {"name": "Total Production", "unit": "kWh"},
            "inverter_temperature": {"name": "Inverter Temperature", "unit": "°C"}
        }
        self.curr_power_name = "current_power"
        self.production_today_name = "production_today"
        self.total_production_name = "total_production"

    def make_ha_topic(self, metadata: dict, internal_name: str, external_name: str, unit: str) -> tuple[str, str]:
        topic = f"homeassistant/sensor/{metadata['serial_number']}/{internal_name}/config"
        state_topic = f"solismqtt/{metadata['serial_number']}"
        ha_name = f"{metadata['serial_number']}_{internal_name}"
        ha_uid = f"{ha_name}_solismqtt"
        hd_device_class = None
        hd_state_class = None
        if unit == "kWh":
            hd_device_class = "energy"
            hd_state_class = "total_increasing"
        elif unit == "W":
            hd_device_class = "power"
            hd_state_class = "measurement"
        elif unit == "°C":
            hd_device_class = "temperature"
            hd_state_class = "measurement"

        assert hd_device_class is not None
        assert hd_state_class is not None
        if MODEL_LOOKUP.get(metadata["model_number"]):
            model = MODEL_LOOKUP.get(metadata["model_number"])
        else:
            model = metadata["model_number"]
        msg = json.dumps({
            "device": {
                "identifiers": [f"solismqtt_{model}_{metadata['serial_number']}"],
                "manufacturer": "Solis",
                "model": model,
                # Device name in HA
                "name": f"Solar Inverter",
                "sw_version": metadata['firmware_version']
            },
            "device_class": hd_device_class,
            # Sensor/Entity name in HA
            "name": external_name,
            "state_class": hd_state_class,
            "state_topic": state_topic,
            "unique_id": ha_uid,
            "unit_of_measurement": unit,
            "value_template": "{{ value_json.%s }}" % internal_name,
            # Don't mark total/today's production as unavailable
            "expire_after": "0" if hd_state_class == "total_increasing" else "120",
            "availability_mode": "latest" if hd_state_class == "total_increasing" else "any",
        })
        return topic, msg

    def read_inverter(self) -> dict:
        logger.debug("Reading...")
        response = requests.get(self.inverter_info_address, timeout=20,
                                auth=self.auth)
        response.raise_for_status()

        # Strip strange padding
        response_split = response.text.rstrip("\x00").lstrip("\x00").split(";")

        # Inverter Serial Number
        webdata_sn = response_split[0]
        # Firmware version
        webdata_msvn = response_split[1]
        # Inverter model
        webdata_pv_type = response_split[2]
        # Inverter temperature (℃)
        webdata_rate_p = float(response_split[3])
        # Current power (W)
        webdata_now_p = int(response_split[4])
        # Yield Today (kWh)
        webdata_today_e = round(float(response_split[5]), 3)

        # Total yield (kWh)
        if response_split[6] != "d":
            webdata_total_e = float(response_split[6])  # Broken
        else:
            webdata_total_e = None
        # Alerts
        webdata_alarm = response_split[7]

        d = {
            "serial_number": webdata_sn,
            "model_number": webdata_pv_type,
            "firmware_version": webdata_msvn,
            "inverter_temperature": webdata_rate_p,
            "power_current": webdata_now_p,
            "power_today": webdata_today_e,
            "power_total": webdata_total_e,
            "alerts_enabled": False if webdata_alarm.lower(
            ) == "no" else True if webdata_alarm.lower() == "yes" else None
        }
        logger.info(f"Inverter data: {json.dumps(d, indent=4)}")
        return d

    def read_device(self) -> dict:
        response = requests.get(self.inverter_wifi_device_address, timeout=20,
                                auth=self.auth)
        response.raise_for_status()

        # Strip strange padding
        response_split = response.text.rstrip("\x00").lstrip("\x00").split(";")

        # Device serial number
        cover_mid = response_split[0]
        # Firmware version
        cover_ver = response_split[1]
        # Wireless AP mode
        cover_ap_status = response_split[2]
        # SSID
        cover_ap_ssid = response_split[3]
        # IP Address
        cover_ap_ip = response_split[4]
        # Index 5 is a null value and not used in UI
        # Wireless STA mode
        cover_sta_status = response_split[6]
        # Router SSID
        cover_sta_ssid = response_split[7]
        # Signal Quality
        cover_sta_rssi = response_split[8]
        # IP address
        cover_sta_ip = response_split[9]
        # MAC address
        cover_sta_mac = response_split[10]
        # Remote server A
        cover_remote_status_a = response_split[11]
        # Remote server B
        cover_remote_status_b = response_split[12]

        d = {
            "sn": cover_mid,
            "fwver": cover_ver,
            "wireless_ap": True if cover_ap_status.lower() == "Enable" else False if cover_ap_status.lower() == "Disable" else None,
            "wireless_ap_ssid": cover_ap_ssid if cover_ap_ssid != "null" else None,
            "wireless_ap_ip": cover_ap_ip if cover_ap_ip != "null" else None,
            "wireless_sta": True if cover_sta_status.lower() == "Enable" else False if cover_sta_status.lower() == "Disable" else None,
            "wireless_sta_ssid": cover_sta_ssid if cover_sta_ssid != "null" else None,
            "wireless_sta_rssi": cover_sta_rssi if cover_sta_rssi != "null" else None,
            "wireless_sta_ip": cover_sta_ip if cover_sta_ip != "null" else None,
            "wireless_sta_mac": cover_sta_mac if cover_sta_mac != "null" else None,
            "remote_server_a_connected": True if cover_remote_status_a.lower() == "connected" else False if cover_remote_status_a.lower() == "unconnected" else None,
            "remote_server_b_connected": True if cover_remote_status_b.lower() == "connected" else False if cover_remote_status_b.lower() == "unconnected" else None,
        }
        return d

    def mqtt_on_connect(self, client, userdata, flags, reason_code, properties):
        if flags.session_present:
            pass
        if reason_code == 0:
            logger.info("Connected to MQTT")
        if reason_code > 0:
            raise Exception(
                f"Failed to connect to MQTT broker with code {reason_code}")

    def mqtt_on_disconnect(self, client, userdata, flags, reason_code, properties):
        logger.warning(f"Disconnected from MQTT ({reason_code})")

    def mqtt_init_client(self):
        client_id = f"solismqtt_{str(uuid.uuid4()).replace('-', '')}"
        self.mqtt_client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=client_id)
        self.mqtt_client.username_pw_set(
            self.mqtt_username, self.mqtt_password)
        self.mqtt_client.on_connect = self.mqtt_on_connect
        self.mqtt_client.on_disconnect = self.mqtt_on_disconnect
        self.mqtt_client.connect(self.mqtt_broker, self.mqtt_port)
        self.mqtt_client.loop_start()

    def mqtt_publish(self, topics, retain=False):
        for topic, msg in topics:
            logger.debug(f"{topic}: {msg}")
            result = self.mqtt_client.publish(topic, msg, retain=retain)
            try:
                result.wait_for_publish(30)
            except RuntimeError as e:
                logger.error(f"Error publishing data: {str(e)}")
            else:
                requests.get(self.uptime_uri)

    def create_topics(self):
        attempts = 0
        while True:
            try:
                metadata = self.read_inverter()
                break
            except:
                # We want it to retry indefinitely as the inverter turns off the wifi module when it goes dark
                if 2**attempts > 600:
                    logger.warning(
                        "Inverter not available. Retrying in 600 seconds")
                    time.sleep(600)
                else:
                    logger.warning(
                        f"Inverter not available. Retrying in {2**attempts} seconds")
                    time.sleep(2**attempts)
                attempts += 1

        self.state_topic = f"solismqtt/{metadata['serial_number']}"

        mqtt_topics = ()

        for internal_name, sensor_info in self.sensors.items():
            if metadata.get(internal_name) != None:
                topic, msg = self.make_ha_topic(
                    metadata, internal_name, sensor_info["name"], sensor_info["unit"])
                mqtt_topics += (topic, msg),

        while True:
            try:
                logger.info(f"Publishing topics {mqtt_topics}")
                self.mqtt_publish(mqtt_topics, True)
                break
            except:
                traceback.print_stack()
                traceback.print_exc()
                time.sleep(60)

    def run(self):
        while True:
            try:
                state = self.read_inverter()
                topic = {}
                for sensor_name in self.sensors.keys():
                    if state.get(sensor_name) != None:
                        logger.debug(
                            f"Adding topic {sensor_name} to data to publish")
                        topic[sensor_name] = state[sensor_name]
                logger.info(f"Publishing data {json.dumps(topic, indent=4)}")

                self.mqtt_publish(((self.state_topic, json.dumps(topic)), ))
            except:
                traceback.print_stack()
                traceback.print_exc()
            time.sleep(self.poll_interval)

    def main(self):
        self.mqtt_init_client()
        self.create_topics()
        self.run()


inverter_logger = SolisInverterLogger()
inverter_logger.main()

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

import os, requests, yaml, time, traceback, uuid, sys, json
from paho.mqtt import client as mqtt_client

SOLIS_SN      = 0
SOLIS_MODEL   = 1
SOLIS_FWVER   = 2
SOLIS_NOW_W   = 3
SOLIS_DAY_KWH = 4
SOLIS_TOT_KHW = 5
SOLIS_BRIDGE_TOPIC = "solismqtt/bridge/state"

print('Hello.', flush=True)

def get_var(text:str, var:str) -> str:
    varstr = 'var ' + var + ' = "'
    i = text.find(varstr)
    if i < 0:
        return ""
    i += len(varstr)
    j = text.find('"', i)
    if j < 0:
        return ""
    return text[i:j].strip()

with open(sys.argv[1], 'r') as yaml_file:
    config = yaml.safe_load(yaml_file)

interv_min = int(config['global']['measure_interv_mins'])

inverter_address  = config['inverter']['ip']
inverter_username = config['inverter']['username']
inverter_password = config['inverter']['password'] 
inverter_address = 'http://' + inverter_address + '/status.html'

mqtt_broker  = config['mqtt']['broker']
mqtt_port    = int(config['mqtt'].get('port', 1883))
mqtt_username = config['mqtt']['username']
mqtt_password = config['mqtt']['password'] 
_mqtt_client = None

def make_ha_topic(metadata, name, unit):
    topic = 'homeassistant/sensor/%s/%s/config' % (
        metadata[SOLIS_SN], name)
    state_topic = "solismqtt/" + metadata[SOLIS_SN]
    ha_name = metadata[SOLIS_SN] + '_' + name
    ha_uid = ha_name + '_solismqtt'
    hd_device_class = None
    hd_state_class = None
    if unit == 'kWh':
        hd_device_class = 'energy'
        hd_state_class = 'total_increasing'
    elif unit == 'W':
        hd_device_class = 'power'
        hd_state_class = 'measurement'
    assert hd_device_class is not None
    assert hd_state_class is not None
    msg = json.dumps({
        "availability": [{"topic": SOLIS_BRIDGE_TOPIC}],
        "device":{
            "identifiers": ["solismqtt_" + metadata[SOLIS_SN]],
            "manufacturer": "Solis",
            "model": metadata[SOLIS_MODEL],
            "name": metadata[SOLIS_SN],
            "sw_version": metadata[SOLIS_FWVER]
        },
        "device_class": hd_device_class,
        "name": ha_name,
        "state_class": hd_state_class,
        "state_topic": state_topic,
        "unique_id": ha_uid,
        "unit_of_measurement": unit,
        "value_template": "{{ value_json.%s }}" % name
    })
    return topic, state_topic, msg      

def make_state_topic(name, states):
    return name, json.dumps(dict(states))

def read_inverter(read_metadata=False):
    print('Reading...', flush=True)
    response = requests.get(inverter_address, timeout=20,
        auth=(inverter_username, inverter_password))
    if response.status_code != 200:
        print("Failed to retrieve data from inverter: %d!" % 
            response.status_code, flush=True)
    if read_metadata:
        webdata_sn      = get_var(response.text, 'webdata_sn')
        webdata_msvn    = get_var(response.text, 'webdata_msvn')
        webdata_ssvn    = get_var(response.text, 'webdata_ssvn')
        webdata_pv_type = get_var(response.text, 'webdata_pv_type')
        print(webdata_sn, webdata_msvn, webdata_ssvn, 
            webdata_pv_type, flush=True)
        return {
            SOLIS_SN   : webdata_sn,
            SOLIS_MODEL: webdata_pv_type,
            SOLIS_FWVER: webdata_msvn + '.' + webdata_ssvn
        }
    else:
        webdata_now_p   = get_var(response.text, 'webdata_now_p')
        webdata_today_e = get_var(response.text, 'webdata_today_e')
        webdata_total_e = get_var(response.text, 'webdata_total_e')
        print(webdata_now_p, webdata_today_e, webdata_total_e, flush=True)
        return {
            SOLIS_NOW_W: float(webdata_now_p),
            SOLIS_DAY_KWH: float(webdata_today_e),
            SOLIS_TOT_KHW: float(webdata_total_e)
        }

def mqtt_on_connect(client, userdata, flags, rc):
    if rc != 0:
        raise Exception('Failed to connect to MQTT broker (%s)!' % str(rc))

def mqtt_on_disconnect(client, userdata, rc):
    if rc != 0:
        print('MQTT connection failure (%s)! Will reconnect.' % rc)

def mqtt_get_client():
    global _mqtt_client
    if _mqtt_client is not None:
        return _mqtt_client
    client_id = 'solismqtt_' + str(uuid.uuid4()).replace('-', '')
    client = mqtt_client.Client(client_id)
    client.username_pw_set(mqtt_username, mqtt_password)
    client.on_connect = mqtt_on_connect
    client.on_disconnect = mqtt_on_disconnect
    client.connect(mqtt_broker, mqtt_port)
    client.loop_start()
    _mqtt_client = client
    return client

def mqtt_publish(topics):
    global _mqtt_client
    client = mqtt_get_client()
    for topic, msg in topics:
        print(topic, msg, flush=True)
        result = client.publish(topic, msg)
        rc = result[0]
        if rc != 0:
            client.loop_stop()
            client.disconnect()
            _mqtt_client = None
            raise Exception('Failed to publish to MQTT broker (%s)!' % 
                str(rc))

mqtt_publish(((SOLIS_BRIDGE_TOPIC, 'online'),))

while True:
    try:
        metadata = read_inverter(True)
        break
    except:
        traceback.print_stack()
        traceback.print_exc()
        time.sleep(60)

curr_power_state_name = 'current_power'
curr_power_ha_topic, \
curr_power_state_topic, \
curr_power_ha_msg = make_ha_topic(metadata, curr_power_state_name, 'W')

daily_energy_state_name = 'daily_energy'
daily_energy_ha_topic, \
daily_energy_state_topic, \
daily_energy_ha_msg = make_ha_topic(metadata, daily_energy_state_name, 'kWh')

total_energy_state_name = 'total_energy'
total_energy_ha_topic, \
total_energy_state_topic, \
total_energy_ha_msg = make_ha_topic(metadata, total_energy_state_name, 'kWh')

assert curr_power_state_topic == daily_energy_state_topic
assert curr_power_state_topic == total_energy_state_topic

while True:
    try:
        mqtt_publish(((SOLIS_BRIDGE_TOPIC, 'online'),))
        mqtt_publish((
            (curr_power_ha_topic  , curr_power_ha_msg), 
            (daily_energy_ha_topic, daily_energy_ha_msg), 
            (total_energy_ha_topic, total_energy_ha_msg)
        ))
        break
    except:
        traceback.print_stack()
        traceback.print_exc()
        time.sleep(60)

while True:
    try:
        mqtt_publish(((SOLIS_BRIDGE_TOPIC, 'online'),))
        state = read_inverter()
        topic = make_state_topic(curr_power_state_topic, (
            (curr_power_state_name  , state[SOLIS_NOW_W]),
            (daily_energy_state_name, state[SOLIS_DAY_KWH]),
            (total_energy_state_name, state[SOLIS_TOT_KHW])
        ))
        # TODO add retries
        mqtt_publish((topic,))
    except:
        traceback.print_stack()
        traceback.print_exc()
    time.sleep(60 * interv_min)


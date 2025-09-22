# main.py -- ESP_SERVER (MicroPython)
# Purpose:
#  - publish trigger to Sensor_cmd
#  - subscribe to Sensor_data
#  - when sensor JSON arrives: run inference using model_coef.py if present,
#    otherwise fall back to wqi_calc.py
#  - save readings to water_readings.jsonl
#  - serve index.html on / and JSON on /latest

import network, time, socket, json, os

# MicroPython MQTT client (umqtt.simple)
try:
    from umqtt.simple import MQTTClient
except Exception as e:
    raise SystemExit("umqtt.simple not found. Install/use firmware with umqtt or adapt code.")

# ---------- CONFIG ----------
WIFI_SSID = "YOUR_WIFI_SSID"
WIFI_PASS = "YOUR_WIFI_PASSWORD"
MQTT_BROKER = "192.168.1.50"   # your Mosquitto laptop IP
MQTT_PORT = 1883
CLIENT_ID = "esp_server_01"

TOPIC_CMD = b"Sensor_cmd"
TOPIC_DATA = b"Sensor_data"
TOPIC_INFER = b"water/data/inferred"   # optional published inferred JSON

DATA_FILE = "water_readings.jsonl"
WWW_DIR = "www"   # place index.html at /www/index.html

# Control timing
SENSOR_RESPONSE_TIMEOUT = 10   # seconds to wait for sensor after trigger
TRIGGER_INTERVAL = 86400       # seconds between automatic triggers (24h)

# ---------- Try to import an exported model (model_coef.py) ----------
USE_MODEL = False
try:
    import model_coef
    # model_coef must define: b, c_tds, c_ph, c_turbidity
    if all(hasattr(model_coef, x) for x in ("b", "c_tds", "c_ph", "c_turbidity")):
        USE_MODEL = True
        print("model_coef imported: using linear coefficients for inference.")
    else:
        print("model_coef found but missing coefficients; falling back.")
except Exception as e:
    USE_MODEL = False
    # no model coefficients available; we'll try wqi_calc fallback
    print("No model_coef.py available; using fallback formula if present.")

# ---------- Try to import fallback deterministic WQI function ----------
compute_wqi_from_minimal = None
try:
    from wqi_calc import compute_wqi_from_minimal
    print("wqi_calc imported (fallback deterministic WQI available).")
except Exception:
    compute_wqi_from_minimal = None
    print("No wqi_calc.py found; final fallback heuristic will be used if needed.")

# ---------- Prediction helpers ----------
def model_predict_from_coef(tds, ph, turbidity):
    """Use coefficients from model_coef.py"""
    try:
        tds = float(tds); ph = float(ph); turbidity = float(turbidity)
    except:
        return None
    wqi = float(model_coef.b) + float(model_coef.c_tds)*tds + float(model_coef.c_ph)*ph + float(model_coef.c_turbidity)*turbidity
    # clip to 0-100
    if wqi < 0: wqi = 0.0
    if wqi > 100: wqi = 100.0
    return round(wqi, 2)

def predict_wqi(tds, ph, turbidity):
    """Main inference function used by the server."""
    if USE_MODEL:
        try:
            res = model_predict_from_coef(tds, ph, turbidity)
            if res is not None:
                return res
        except Exception as e:
            # if model fails, continue to fallback
            print("model_coef inference failed:", e)

    if compute_wqi_from_minimal:
        try:
            return compute_wqi_from_minimal(tds, ph, turbidity)[0]
        except Exception as e:
            print("wqi_calc failed:", e)

    # Last-resort trivial heuristic
    try:
        tds = float(tds); ph = float(ph); turbidity = float(turbidity)
        wqi = 50.0 + (7.0 - abs(ph - 7.0))*2.0 - (tds/600.0)*5.0 - (turbidity/15.0)*8.0
        if wqi < 0: wqi = 0.0
        if wqi > 100: wqi = 100.0
        return round(wqi, 2)
    except:
        return None

# ---------- storage ----------
def save_record(rec):
    try:
        with open(DATA_FILE, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print("save_record error:", e)

# ---------- WiFi ----------
wlan = network.WLAN(network.STA_IF)
def connect_wifi():
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to WiFi...")
        wlan.connect(WIFI_SSID, WIFI_PASS)
        timeout = time.time() + 20
        while not wlan.isconnected() and time.time() < timeout:
            time.sleep(0.5)
    if wlan.isconnected():
        print("WiFi IP:", wlan.ifconfig()[0])
        return True
    print("WiFi connection failed")
    return False

# ---------- MQTT ----------
client = None
latest = None
waiting_for_sensor = False
sensor_payload = None

def mqtt_cb(topic, msg):
    """Callback when sensor publishes reading to TOPIC_DATA"""
    global latest, waiting_for_sensor, sensor_payload
    try:
        print("MQTT in:", topic, msg)
        if topic == TOPIC_DATA:
            j = json.loads(msg)
            rd = j.get("readings", {})
            # try multiple possible key names to be robust
            tds = rd.get("tds_ppm") or rd.get("tds") or rd.get("tds_ppm") or rd.get("tds_ppm")
            ph = rd.get("ph")
            turbidity = rd.get("turbidity_pct") or rd.get("turbidity")
            wqi = predict_wqi(tds, ph, turbidity)
            rec = {
                "ts": int(time.time()),
                "device": j.get("device", "sensor"),
                "tds": tds,
                "ph": ph,
                "turbidity": turbidity,
                "wqi": wqi
            }
            latest = rec
            save_record(rec)
            # optionally publish inferred reading
            try:
                client.publish(TOPIC_INFER, json.dumps(rec))
            except Exception:
                pass
            if waiting_for_sensor:
                sensor_payload = j
                waiting_for_sensor = False
    except Exception as e:
        print("Error in mqtt_cb:", e)

def mqtt_connect():
    global client
    client = MQTTClient(CLIENT_ID, MQTT_BROKER, port=MQTT_PORT)
    client.set_callback(mqtt_cb)
    client.connect()
    client.subscribe(TOPIC_DATA)
    print("MQTT connected and subscribed to", TOPIC_DATA)

# ---------- HTTP server (simple) ----------
def serve_forever():
    addr = socket.getaddrinfo("0.0.0.0", 80)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(1)
    print("HTTP server listening on", addr)
    while True:
        try:
            cl, addr = s.accept()
            cl_file = cl.makefile("rwb", 0)
            req_line = cl_file.readline()
            if not req_line:
                cl.close(); continue
            req = req_line.decode()
            method, path, _ = req.split()
            # consume headers
            while True:
                h = cl_file.readline()
                if not h or h == b"\r\n":
                    break
            if method == "GET" and (path == "/" or path == "/index.html"):
                html = "<html><body><h3>Index not found</h3></body></html>"
                index_path = WWW_DIR + "/index.html"
                try:
                    if WWW_DIR in os.listdir():
                        with open(index_path, "r") as f:
                            html = f.read()
                except Exception:
                    pass
                cl.send("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n")
                cl.send(html)
            elif method == "GET" and path == "/latest":
                payload = json.dumps(latest if latest else {"status":"no_data"})
                cl.send("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n")
                cl.send(payload)
            else:
                cl.send("HTTP/1.1 404 Not Found\r\n\r\n")
            cl.close()
        except Exception as e:
            print("HTTP error:", e)

# ---------- trigger ----------
def publish_trigger():
    try:
        client.publish(TOPIC_CMD, b"snd/data")
        print("Published trigger to", TOPIC_CMD)
        return True
    except Exception as e:
        print("Trigger publish failed:", e)
        return False

# ---------- startup / loop ----------
def main():
    connect_wifi()
    try:
        mqtt_connect()
    except Exception as e:
        print("Initial MQTT connect failed:", e)

    # start HTTP server in separate thread if available
    try:
        import _thread
        _thread.start_new_thread(serve_forever, ())
    except Exception as e:
        print("Could not start HTTP server thread:", e)

    # trigger once at startup
    try:
        publish_trigger()
    except:
        pass

    last_trigger = time.time()
    global waiting_for_sensor, sensor_payload
    while True:
        try:
            # process incoming MQTT messages
            try:
                client.check_msg()
            except Exception:
                try:
                    mqtt_connect()
                except:
                    pass

            # periodic trigger
            now = time.time()
            if now - last_trigger >= TRIGGER_INTERVAL:
                waiting_for_sensor = True
                sensor_payload = None
                publish_trigger()
                # wait for sensor response with timeout
                wait_until = time.time() + SENSOR_RESPONSE_TIMEOUT
                while waiting_for_sensor and time.time() < wait_until:
                    try:
                        client.check_msg()
                    except:
                        pass
                    time.sleep(0.1)
                if sensor_payload is None:
                    print("No sensor response within timeout")
                last_trigger = now

            time.sleep(0.5)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(1)

if __name__ == "__main__":
    main()

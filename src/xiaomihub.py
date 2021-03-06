import socket
import struct
import json
import logging
from collections import defaultdict
from queue import Queue
from threading import Thread

_LOGGER = logging.getLogger(__name__)

# MANDATORY!!!! NEED TO TURN OFF "_process_report" THREAD IF CODE IS UPDATED!!!!

class XiaomiHub:
    GATEWAY_KEY = None
    GATEWAY_IP = None
    GATEWAY_PORT = None
    GATEWAY_SID = None
    GATEWAY_TOKEN = None

    XIAOMI_DEVICES = defaultdict(list)
    XIAOMI_HA_DEVICES = defaultdict(list)

    MULTICAST_ADDRESS = '224.0.0.50'
    MULTICAST_PORT = 9898
    GATEWAY_DISCOVERY_ADDRESS = '224.0.0.50'
    GATEWAY_DISCOVERY_PORT = 4321
    SOCKET_BUFSIZE = 1024

    def __init__(self, key, gateway=None):
        self.GATEWAY_KEY = key
        self._listening = False
        self._queue = None
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._mcastsocket = None
        self._deviceCallbacks = defaultdict(list)
        self._threads = []

        if gateway is not None:
            self.GATEWAY_DISCOVERY_ADDRESS = gateway

        try:
            _LOGGER.info('Discovering Xiaomi Gateways using address {0}'.format(self.GATEWAY_DISCOVERY_ADDRESS))
            data = self._send_socket('{"cmd":"whois"}', "iam", self.GATEWAY_DISCOVERY_ADDRESS, self.GATEWAY_DISCOVERY_PORT)
            if data["model"] == "gateway":
                self.GATEWAY_IP = data["ip"]
                self.GATEWAY_PORT = int(data["port"])
                self.GATEWAY_SID = data["sid"]
                _LOGGER.info('Gateway found on IP {0}'.format(self.GATEWAY_IP))
            else:
                _LOGGER.error('Error with gateway response : {0}'.format(data))
        except Exception as e:
            raise
            _LOGGER.error("Cannot discover hub using whois: {0}".format(e))

        self._socket.close()
	
        if self.GATEWAY_IP is None:
            _LOGGER.error('No Gateway found. Cannot continue')
            return None
        
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        _LOGGER.info('Creating Multicast Socket')
        self._mcastsocket = self._create_mcast_socket()
        if self._listen() is True:
            _LOGGER.info("Listening")

        _LOGGER.info('Discovering Xiaomi Devices')
        self._discover_devices()

    def _discover_devices(self):

        cmd = '{"cmd" : "get_id_list"}'
        resp = self._send_cmd(cmd, "get_id_list_ack")
        self.GATEWAY_TOKEN = resp["token"]
        sids = json.loads(resp["data"])

        _LOGGER.info('Found {0} devices'.format(len(sids)))

        sensors = ['sensor_ht']
        binary_sensors = ['magnet', 'motion', 'switch', '86sw1', '86sw2', 'cube']
        switches = ['plug', 'ctrl_neutral1', 'ctrl_neutral2']

        for sid in sids:
            cmd = '{"cmd":"read","sid":"' + sid + '"}'
            resp = self._send_cmd(cmd, "read_ack")
            model = resp["model"]

            if model == '':
                model = 'cube'

            xiaomi_device = {
                "model":model, 
                "sid":resp["sid"], 
                "short_id":resp["short_id"], 
                "data":json.loads(resp["data"])}

            device_type = None
            if model in sensors:
                device_type = 'sensor'
            elif model in binary_sensors:
                device_type = 'binary_sensor'
            elif model in switches:
                device_type = 'switch'

            if device_type == None:
                _LOGGER.error('Unsupported devices : {0}'.format(model))
            else:
                self.XIAOMI_DEVICES[device_type].append(xiaomi_device)

    def _send_cmd(self, cmd, rtnCmd):
        return self._send_socket(cmd, rtnCmd, self.GATEWAY_IP, self.GATEWAY_PORT)

    def _send_socket(self, cmd, rtnCmd, ip, port):
        socket = self._socket
        try:
            socket.settimeout(30.0)
            socket.sendto(cmd.encode(), (ip, port))
            socket.settimeout(30.0)
            data, addr = socket.recvfrom(1024)
            if len(data) is not None:
                resp = json.loads(data.decode())
                if resp["cmd"] == rtnCmd:
                    return resp
                else:
                    _LOGGER.error("Response from {0} does not match return cmd".format(ip))
                    _LOGGER.error(data)
            else:
                _LOGGER.error("No response from Gateway")
        except socket.timeout:
            _LOGGER.error("Cannot connect to Gateway")
            socket.close()

    def write_to_hub(self, sid, data_key, datavalue):
        key = self._get_key()
        if type(datavalue) == int:
            datavalue_formatted = str(datavalue)
        else:
            datavalue_formatted = '"' + datavalue + '"'
        cmd = '{ "cmd":"write","sid":"' + sid + '","data":"{"' + data_key + '":' + datavalue_formatted + ',"key":"' + key + '"}}'
        return self._send_cmd(cmd, "write_ack")     

    def get_from_hub(self, sid):
        cmd = '{ "cmd":"read","sid":"' + sid + '"}'
        return self._send_cmd(cmd, "read_ack")     

    def _get_key(self):
        from Crypto.Cipher import AES
        IV = bytes(bytearray.fromhex('17996d093d28ddb3ba695a2e6f58562e'))
        encryptor = AES.new(self.GATEWAY_KEY, AES.MODE_CBC, IV=IV)
        ciphertext = encryptor.encrypt(self.GATEWAY_TOKEN)
        return ''.join('{:02x}'.format(x) for x in ciphertext)

    def _create_mcast_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.MULTICAST_ADDRESS, self.MULTICAST_PORT))
        mreq = struct.pack("4sl", socket.inet_aton(self.MULTICAST_ADDRESS), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        return sock

    def _listen(self):
        """Start listening."""
        self._queue = Queue()
        self._listening = True

        t1 = Thread(target=self._listen_to_msg, args=())
        self._threads.append(t1)
        t1.daemon = True
        t1.start()

        # t2 = Thread(target=self._process_report, args=())
        # self._threads.append(t2)
        # t2.da = True
        # t2.start()

        return True

    def stop(self):
        """Stop listening."""
        self._listening = False
        self._queue.put(None)

        for t in self._threads:
            t.join()

        if self._mcastsocket is not None:
            self._mcastsocket.close()
            self._mcastsocket = None

    def _listen_to_msg(self):
        while self._listening:
            if self._mcastsocket is not None:
                data, addr = self._mcastsocket.recvfrom(self.SOCKET_BUFSIZE)
                try:
                    data = json.loads(data.decode("ascii"))
                    cmd = data['cmd']
                    _LOGGER.debug(format(data))
                    if cmd == 'heartbeat' and data['model'] == 'gateway':
                        self.GATEWAY_TOKEN = data['token']
                    elif cmd == 'report' or cmd == 'heartbeat':
                        self._queue.put(data)
                    else:
                        _LOGGER.error('Unknown multicast data : {0}'.format(data))
                except Exception as e:
                    raise
                    _LOGGER.error('Cannot process multicast message : {0}'.format(data))

    def _process_report(self):
        while self._listening:
            packet = self._queue.get(True)
            if isinstance(packet, dict):
                try:
                    sid = packet['sid']
                    model = packet['model']
                    data = json.loads(packet['data'])
                    
                    for device in self.XIAOMI_HA_DEVICES[sid]:
                        device.push_data(data)

                except Exception as e:
                    _LOGGER.error("Cannot process Report: {0}".format(e))

            self._queue.task_done()

class XiaomiDevice():
    """Representation a base Xiaomi device."""

    def __init__(self, device, name, xiaomi_hub):
        """Initialize the xiaomi device."""
        self._sid = device['sid']
        self._name = '{}_{}'.format(name, self._sid)
        self.parse_data(device['data'])

        self.xiaomi_hub = xiaomi_hub
        xiaomi_hub.XIAOMI_HA_DEVICES[self._sid].append(self)

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def should_poll(self):
        return False

    def push_data(self, data):
        return True

    def parse_data(self, data):
        return True
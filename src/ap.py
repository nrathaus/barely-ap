"""
This is a self-contained, bare-bones access point using Scapy.

It can:
1) Respond to Probe requests
2) Allow a station to associate with WPA2 + CCMP
3) Send traffic to/from a TAP tunnel device

It has no protocol security -- use at your own risk.

This is built with snippets from several sources:
1) pyaes https://github.com/ricmoo/pyaes
2) libwifi https://github.com/vanhoefm/libwifi
3) https://github.com/rpp0/scapy-fakeap
4) hostapd's testing suite
"""
import binascii
import fcntl
import hashlib
import hmac
import os
import random
import subprocess
import sys
import threading
from itertools import count
import time

from scapy.arch import get_if_raw_hwaddr, str2mac
from scapy.fields import *
from scapy.layers.dot11 import *
from scapy.layers.inet import *
from scapy.layers.eap import EAPOL
from scapy.layers.l2 import LLC, SNAP, ARP
from scapy.layers.dhcp import BOOTP, DHCP
from scapy.layers.dns import *

# You can use this to debug SCAPY 'crashes' with:
#  Socket <scapy.arch.linux.L2ListenSocket object at 0x7f9a188407c0> failed with 'Layer [DHCP] not found'. It was closed.
from scapy.config import conf

conf.debug_dissector = 2

from ccmp import *
from fakenet import ScapyNetwork

# backoff time for auth per sta
BACKOFF = 0.25
# BACKOFF=0.00001


class Level:
    CRITICAL = 0
    WARNING = 1
    INFO = 2
    DEBUG = 3
    BLOAT = 4


VERBOSITY = Level.BLOAT


def printd(string, level=Level.INFO):
    if VERBOSITY >= level:
        print(string, file=sys.stderr)


### Constants

# CCMP, psk=WPA2
eRSN = Dot11EltRSN(
    ID=48,
    len=20,
    version=1,
    mfp_required=0,
    mfp_capable=0,
    group_cipher_suite=RSNCipherSuite(cipher="CCMP-128"),
    nb_pairwise_cipher_suites=1,
    pairwise_cipher_suites=RSNCipherSuite(cipher="CCMP-128"),
    nb_akm_suites=1,
    akm_suites=AKMSuite(suite="PSK"),
)
RSN = eRSN.build()


def make_beacon_ies(ssid_name, channel):
    # tbd hm
    # ht_caps = Dot11EltHTCapabilities()
    # ht_info = Dot11Elt(
    #     ID="HT Operation",
    #     info=(
    #         b"\x0b\x08\x05\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    #     ),
    # )
    # extcaps = Dot11Elt(
    #     ID="ExtendedCapatibilities", info=(b"\x01\x10\x08\x00\x00\x00\x00\x00")
    # )

    env = b"\x20"  # all environments
    first_channel = bytes([channel])
    num_channel = bytes([1])
    power = b"\x17"
    country = Dot11Elt(
        ID="Country", info=(b"US" + env + first_channel + num_channel + power)
    )

    # [0x82, 0x84, 0x0b, 0x16]
    rates = [0x82, 0x84, 0x0B, 0x16]
    ap_rates = bytes(rates)

    BEACON_IES = (
        Dot11Elt(ID="SSID", info=ssid_name)
        / Dot11Elt(ID="Rates", info=ap_rates)
        / Dot11EltRates(ID="Extended Supported Rates", rates=rates)
        / Dot11Elt(ID="DSSS Set", info=bytes([channel]))
        / country
    )
    return BEACON_IES


DOT11_MTU = 4096

DOT11_TYPE_MANAGEMENT = 0
# DOT11_TYPE_CONTROL = 1
DOT11_TYPE_DATA = 2

# DOT11_SUBTYPE_DATA = 0x00
DOT11_SUBTYPE_PROBE_REQ = 0x04
DOT11_SUBTYPE_AUTH_REQ = 0x0B
DOT11_SUBTYPE_ASSOC_REQ = 0x00
DOT11_SUBTYPE_REASSOC_REQ = 0x02
# DOT11_SUBTYPE_QOS_DATA = 0x28


IFNAMSIZ = 16
IFF_TUN = 0x0001
IFF_TAP = 0x0002
IFF_NO_PI = 0x1000
TUNSETIFF = 0x400454CA


def if_hwaddr(iff):
    """if_hwaddr"""
    return str2mac(get_if_raw_hwaddr(iff)[1])


def set_ip_address(dev, ip, network):
    """set_ip_address"""
    if subprocess.call(["ip", "addr", "add", ip, "dev", dev]):
        printd(f"Failed to assign IP address {ip} to {dev}.", Level.CRITICAL)

    if subprocess.call(
        ["ip", "route", "add", network, "dev", dev]
    ):  # tbd parse ip and fix subnet
        printd(f"Failed to assign IP route {network} to {dev}.", Level.CRITICAL)


def set_if_up(dev):
    """set_if_up"""
    if subprocess.call(["ip", "link", "set", "dev", dev, "up"]):
        printd(f"Failed to bring device {dev} up.", Level.CRITICAL)


def set_if_addr(dev, addr):
    """set_if_addr"""
    if subprocess.call(["ip", "link", "set", "dev", dev, "addr", addr]):
        printd(f"Failed to set device {dev} add to {addr}.", Level.CRITICAL)


class TunInterface(threading.Thread):
    """TunInterface"""

    def __init__(self, bss, ip=None, network=None, name="scapyap"):
        threading.Thread.__init__(self)

        if len(name) > IFNAMSIZ:
            raise Exception(f"Tun interface name cannot be larger than {IFNAMSIZ}")

        self.name = name
        self.daemon = True
        self.bss = bss
        self.ip = ip
        self.network = network

        # Virtual interface
        self.fd = os.open("/dev/net/tun", os.O_RDWR)
        ifr_flags = IFF_TAP | IFF_NO_PI  # Tun device without packet information
        ifreq = struct.pack("16sH", name.encode("ascii"), ifr_flags)
        fcntl.ioctl(self.fd, TUNSETIFF, ifreq)  # Syscall to create interface

        set_if_up(name)
        # update addr
        set_if_addr(name, self.bss.mac)
        # Assign IP and bring interface up
        if self.ip:
            set_ip_address(name, self.ip, self.network)

        print(
            f"Created TUN interface {name} at {self.ip}. Bind it to your services if needed."
        )

    def write(self, pkt):
        """write"""
        os.write(self.fd, pkt.build())

    def read(self):
        """read"""
        try:
            raw_packet = os.read(self.fd, DOT11_MTU)
            return raw_packet
        except Exception as e:
            print(f"An exception has occurred during 'read': {e}")

    def close(self):
        """close"""
        os.close(self.fd)

    def run(self):
        """run"""
        while True:
            raw_packet = self.read()
            sta = Ether(raw_packet).dst
            self.bss.ap.tun_data_incoming(self.bss, sta, raw_packet)


class Station:
    """Station"""

    def __init__(self, mac):
        self.mac = mac
        self.associated = False
        self.eapol_ready = False


# Ripped from scapy-latest with fixes
class EAPOL_KEY(Packet):
    """EAPOL_KEY"""

    name = "EAPOL_KEY"
    fields_desc = [
        ByteEnumField("key_descriptor_type", 1, {1: "RC4", 2: "RSN"}),
        # Key Information
        BitField("reserved2", 0, 2),
        BitField("smk_message", 0, 1),
        BitField("encrypted_key_data", 0, 1),
        BitField("request", 0, 1),
        BitField("error", 0, 1),
        BitField("secure", 0, 1),
        BitField("has_key_mic", 1, 1),
        BitField("key_ack", 0, 1),
        BitField("install", 0, 1),
        BitField("key_index", 0, 2),
        BitEnumField("key_type", 0, 1, {0: "Group/SMK", 1: "Pairwise"}),
        BitEnumField(
            "key_descriptor_type_version",
            0,
            3,
            {1: "HMAC-MD5+ARC4", 2: "HMAC-SHA1-128+AES-128", 3: "AES-128-CMAC+AES-128"},
        ),
        #
        LenField("key_length", None, "H"),
        LongField("key_replay_counter", 0),
        XStrFixedLenField("key_nonce", b"\x00" * 32, 32),
        XStrFixedLenField("key_iv", b"\x00" * 16, 16),
        XStrFixedLenField("key_rsc", b"\x00" * 8, 8),
        XStrFixedLenField("key_id", b"\x00" * 8, 8),
        XStrFixedLenField("key_mic", b"\x00" * 16, 16),  # XXX size can be 24
        LenField("wpa_key_length", None, "H"),
        ConditionalField(
            XStrLenField(
                "key", b"\x00" * 16, length_from=lambda pkt: pkt.wpa_key_length
            ),
            lambda pkt: pkt.wpa_key_length and pkt.wpa_key_length > 0,
        ),
    ]

    def extract_padding(self, s):
        """extract_padding"""
        return s[: self.key_length], s[self.key_length :]

    def hashret(self):
        """hashret"""
        return struct.pack("!B", self.type) + self.payload.hashret()

    def answers(self, other):
        """answers"""
        if (
            isinstance(other, EAPOL_KEY)
            and other.descriptor_type == self.descriptor_type
        ):
            return 1
        return 0


class BSS:
    """BSS"""

    def __init__(self, ap, ssid, mac, psk, ip="10.10.10.1/24", mode="tunnel"):
        self.ap = ap
        self.ssid = ssid
        self.mac = mac
        self.psk = psk
        self.ip = ip
        self.sc = 0
        self.aid = 0
        self.stations = {}
        self.gtk = b""
        self.pmk = hashlib.pbkdf2_hmac(
            "sha1", self.psk.encode(), self.ssid.encode(), 4096, 32
        )

        self.group_iv = count()
        self.mutex = threading.Lock()
        self.auth_times = {}
        self.assoc_times = {}

        # tbd regen this
        self.gen_gtk()
        if mode == "tunnel":
            # use a TUN device
            ip_without_subnet = ip[0 : ip.find("/")]
            subnet = ip[ip.find("/") + 1 :]
            network = f'{ip[0:ip_without_subnet.rfind(".")]}.0/{subnet}'
            self.network = TunInterface(self, ip=ip_without_subnet, network=network)
        else:
            # use a fake scapy network
            self.network = ScapyNetwork(self, ip=ip)

    def next_sc(self):
        """next_sc"""
        self.mutex.acquire()
        self.sc = (self.sc + 1) % 4096
        temp = self.sc
        self.mutex.release()

        return temp * 16  # Fragment number -> right 4 bits

    def next_aid(self):
        """next_aid"""
        self.mutex.acquire()
        self.aid = (self.aid + 1) % 2008
        temp = self.aid
        self.mutex.release()
        return temp

    def gen_gtk(self):
        """gen_gtk"""
        self.gtk_full = open("/dev/urandom", "rb").read(32)
        self.gtk = self.gtk_full[:16]
        self.MIC_AP_TO_GROUP = self.gtk_full[16:24]
        self.group_iv = count()


def config_mon(iface, channel):
    """
    Set the interface in monitor mode and then change channel using iw
    """
    res = os.system(f"ip link set dev {iface} down")
    if res != 0:
        print("Failed to bring down the device")

    res = os.system(
        f"iw dev {iface} set type monitor"
    )  # this can break driver if overdone
    if res != 0:
        print("Failed to set the device to monitor mode")

    res = os.system(f"ip link set dev {iface} up")
    if res != 0:
        print("Failed to bring up the device")

    res = os.system(f"iw dev {iface} set channel {channel}")
    if res != 0:
        print("Failed to set the channel")


class AP:
    def __init__(
        self, ssid, psk, mac=None, mode="stdio", iface="wlan0", iface2="", channel=1
    ):
        self.wpa = True
        self.ap_ip = "10.0.0.1"
        self.eapol_ready = False
        self.channel = channel
        self.iface = iface
        self.mode = mode
        if iface2 == "":
            iface2 = iface
        if self.mode == "iface":
            if not mac:
                mac = if_hwaddr(iface2)
            config_mon(iface2, channel)
            self.sendy = conf.L2socket(iface=self.iface)
        if not mac:
            raise Exception("Need a mac")
        else:
            self.mac = mac
        self.boottime = time.time()

        self.bssids = {mac: BSS(self, ssid, mac, psk, "10.0.0.1/24")}
        self.beaconTransmitter = self.BeaconTransmitter(self)

    def ssids(self):
        return [self.bssids[x].ssid for x in self.bssids]

    def get_radiotap_header(self):
        return RadioTap()

    def get_ssid(self, mac):
        if mac not in self.bssids:
            return None
        return self.bssids[mac].ssid

    def current_timestamp(self):
        return int((time.time() - self.boottime) * 1000000)

    def tun_data_incoming(self, bss, sta, incoming):
        p = Ether(incoming)
        self.enc_send(bss, sta, p)

    def bytes_to_mac(self, byte_array):
        """bytes_to_mac"""
        return ":".join("{:02x}".format(ord(byte)) for byte in byte_array)

    def handle_arp(self, bssid, packet: Packet):
        """Handle ARP data"""
        receiver_ip = packet[ARP].psrc
        receiver_mac = packet[ARP].hwsrc

        if bssid is None:
            bssid = packet.getlayer(Dot11).addr1
            bss = self.bssids[bssid]

        arp_response = Ether() / scapy.layers.l2.ARP(
            psrc=self.ap_ip,
            pdst=receiver_ip,
            op="is-at",
            hwsrc=self.mac,
            hwdst=receiver_mac,
        )

        if self.wpa:
            self.enc_send(self.bssids[bssid], packet[Ether].src, arp_response)
        else:
            arp_response = arp_response[1]  # Remove the Ethernet
            arp_response = (
                self.get_radiotap_header()
                / Dot11(
                    type="Data",
                    FCfield="from-DS",
                    addr1=receiver_mac,
                    addr2=bss.mac,
                    addr3=bss.mac,
                    SC=bss.next_sc(),
                )
                / LLC(dsap=0xAA, ssap=0xAA, ctrl=0x03)
                / SNAP(OUI=0x000000, code=scapy.data.ETH_P_ARP)
                / arp_response
            )
            self.sendp(arp_response)

        return

    def handle_icmp(self, bssid, packet: Packet):
        """Handle ICMP data"""
        if bssid is None:
            client_mac = packet[Dot11].addr2
            bssid = packet.getlayer(Dot11).addr1
            if bssid not in self.bssids:
                # We don't info on this BSS
                return
            bss = self.bssids[bssid]

        if packet[ICMP].type != 8:  # echo-request:
            return

        receiver_ip = packet[IP].src

        icmp_response = (
            # Fill these dst/src of Ether fields, so that SCAPY knows how to send it - instead of guessing
            #  and then writing that it cannot route the data, because there is no 10.0.0.1 network
            #  on this computer
            Ether(dst=packet[Ether].src, src=packet[Ether].dst)
            / IP(src=packet[IP].dst, dst=receiver_ip)
            / ICMP(type="echo-reply", id=packet[ICMP].id, seq=packet[ICMP].seq)
            / Raw(load=packet[Raw].load)
        )

        # print(f"Sending a ICMP response for incoming: {packet}")
        if self.wpa:
            self.enc_send(self.bssids[bssid], packet[Ether].src, icmp_response)
        else:
            icmp_response = icmp_response[1]  # Remove the Ethernet
            icmp_response = (
                self.get_radiotap_header()
                / Dot11(
                    type="Data",
                    FCfield="from-DS",
                    addr1=client_mac,
                    addr2=bss.mac,
                    addr3=bss.mac,
                    SC=bss.next_sc(),
                )
                / LLC(dsap=0xAA, ssap=0xAA, ctrl=0x03)
                / SNAP(OUI=0x000000, code=scapy.data.ETH_P_IP)
                / icmp_response
            )
            self.sendp(icmp_response)
        # print("Done")

        return

    def handle_dns(self, bssid, packet: Packet):
        """Handle DNS data"""
        if packet[DNS].qd is None:
            return

        if bssid is None:
            client_mac = packet[Dot11].addr2
            bssid = packet.getlayer(Dot11).addr1
            if bssid not in self.bssids:
                # We don't info on this BSS
                return
            bss = self.bssids[bssid]

        receiver_ip = packet[IP].src
        if "224.0.0." in packet[IP].dst:
            # This is mDNS
            return

        if len(packet[DNS].qd) == 0:
            # Not a query we can handle
            return

        dns_response = (
            # Fill these dst/src of Ether fields, so that SCAPY knows how to send it - instead of guessing
            #  and then writing that it cannot route the data, because there is no 10.0.0.1 network
            #  on this computer
            Ether(dst=packet[Ether].src, src=packet[Ether].dst)
            / IP(src=self.ap_ip, dst=receiver_ip)
            / UDP(sport=53, dport=packet[UDP].sport)
            / DNS(
                id=packet[DNS].id,
                qd=packet[DNS].qd,
                aa=1,
                rd=0,
                qr=1,
                qdcount=1,
                ancount=1,
                nscount=0,
                arcount=0,
                ar=DNSRR(
                    rrname=packet[DNS].qd.qname, type="A", ttl=600, rdata=self.ap_ip
                ),
            )
        )

        # print(f"Sending a DNS response for incoming: {packet}")
        if self.wpa:
            self.enc_send(self.bssids[bssid], packet[Ether].src, dns_response)
        else:
            dns_response = dns_response[1]  # Remove the Ethernet
            dns_response = (
                self.get_radiotap_header()
                / Dot11(
                    type="Data",
                    FCfield="from-DS",
                    addr1=client_mac,
                    addr2=bss.mac,
                    addr3=bss.mac,
                    SC=bss.next_sc(),
                )
                / LLC(dsap=0xAA, ssap=0xAA, ctrl=0x03)
                / SNAP(OUI=0x000000, code=scapy.data.ETH_P_IP)
                / dns_response
            )
            self.sendp(dns_response)
        # print("Done")

        return

    def handle_bootp(self, bssid, packet: Packet):
        """Handle BOOTP data"""
        client_mac = packet[BOOTP].chaddr[:6]
        xid = packet[BOOTP].xid

        # Make the IP, one beyond ours
        client_ip = self.ap_ip[0 : self.ap_ip.rfind(".")]
        last_ip = int(self.ap_ip[self.ap_ip.rfind(".") + 1 :])

        client_ip = f"{client_ip}.{last_ip+1}"

        if bssid is None:
            bssid = packet.getlayer(Dot11).addr1
            if bssid not in self.bssids:
                # We don't info on this BSS
                return
            bss = self.bssids[bssid]

        if DHCP not in packet:
            # Not a valid DHCP packet
            return

        if packet[DHCP].options[0][1] == 1:
            dhcp_response = (
                Ether(dst=client_mac, src=self.mac)
                / IP(src=self.ap_ip, dst=client_ip)
                / UDP(sport=67, dport=68)
                / BOOTP(
                    op=2,
                    yiaddr=client_ip,
                    siaddr=self.ap_ip,
                    giaddr=self.ap_ip,
                    chaddr=client_mac,
                    xid=xid,
                )
                / DHCP(options=[("message-type", "offer")])
                / DHCP(options=[("subnet_mask", "255.255.255.0")])
                / DHCP(options=[("server_id", self.ap_ip), "end"])
            )
        elif packet[DHCP].options[0][1] == 3:
            dhcp_response = (
                Ether(dst=client_mac, src=self.mac)
                / IP(src=self.ap_ip, dst=client_ip)
                / UDP(sport=67, dport=68)
                / BOOTP(
                    op=2,
                    yiaddr=client_ip,
                    siaddr=self.ap_ip,
                    giaddr=self.ap_ip,
                    chaddr=client_mac,
                    xid=xid,
                )
                / DHCP(options=[("message-type", "ack")])
                / DHCP(options=[("server_id", self.ap_ip)])
                / DHCP(options=[("lease_time", 43200)])
                / DHCP(options=[("subnet_mask", "255.255.255.0")])
                / DHCP(options=[("router", self.ap_ip)])
                / DHCP(options=[("name_server", self.ap_ip)])
                / DHCP(options=[("domain", "localdomain")])
                / DHCP(options=["end"])
            )
        else:
            # Not handled by us
            return

        # print(f"Sending a DHCP response for incoming: {packet}")
        if self.wpa:
            self.enc_send(self.bssids[bssid], packet[Ether].src, dhcp_response)
        else:
            dhcp_response = dhcp_response[1]  # Remove the Ethernet
            dhcp_response = (
                self.get_radiotap_header()
                / Dot11(
                    type="Data",
                    FCfield="from-DS",
                    addr1=client_mac,
                    addr2=bss.mac,
                    addr3=bss.mac,
                    SC=bss.next_sc(),
                )
                / LLC(dsap=0xAA, ssap=0xAA, ctrl=0x03)
                / SNAP(OUI=0x000000, code=scapy.data.ETH_P_IP)
                / dhcp_response
            )
            sendp(dhcp_response, iface=self.iface)
        # print("Done")

        return

    def handle_data_packet(self, bssid, packet):
        """handle_data_packet"""
        # print(f"Handling: {packet}")

        if BOOTP in packet:
            self.handle_bootp(bssid, packet)
            return True

        if ARP in packet:
            self.handle_arp(bssid, packet)
            return True

        if IP in packet and DNS in packet:
            self.handle_dns(bssid, packet)
            return True

        if IP in packet and ICMP in packet:
            self.handle_icmp(bssid, packet)
            return True

        return False

    def recv_pkt(self, packet):
        if hasattr(packet, "addr1"):
            a = packet.addr1
            if a != self.mac:
                if is_multicast(a) or is_broadcast(a):
                    if packet.addr2 == self.mac:
                        return
                else:
                    return
        # print("recv", packet)
        try:
            if len(packet.notdecoded[8:9]) > 0:  # Driver sent radiotap header flags
                # This means it doesn't drop packets with a bad FCS itself
                flags = ord(packet.notdecoded[8:9])

                if flags & 64 != 0:  # BAD_FCS flag is set
                    # Print a warning if we haven't already discovered this MAC
                    if not packet.addr2 is None:
                        printd(
                            f"Dropping corrupt packet from {packet.addr2}",
                            Level.BLOAT,
                        )
                    # Drop this packet
                    return

            if EAPOL in packet:
                self.create_eapol_3(packet)
            elif Dot11CCMP in packet:
                if packet[Dot11].FCfield == "to-DS+protected":
                    sta = packet[Dot11].addr2
                    bssid = packet[Dot11].addr1
                    if bssid not in self.bssids:
                        printd("[-] Invalid bssid destination for packet")
                        return

                    decrypted = self.decrypt(bssid, sta, packet)
                    if decrypted:
                        # make sure that the ethernet src matches the station,
                        # otherwise block
                        if sta != decrypted[Ether].src:
                            printd("[-] Invalid mac address for packet")
                            return
                        # self.tunnel.write(decrypted)
                        # printd("write to %s from %s" % (bssid, sta))

                        if self.handle_data_packet(bssid, decrypted):
                            return

                        self.bssids[bssid].network.write(
                            decrypted
                        )  # packet from a client
                    else:
                        printd("[-] Failed to decrypt {sta} to {bssid}")

                    return

            if not hasattr(packet, "type"):
                # import code
                # code.interact(local=locals())
                # not parsed ;-), could be AWDL
                return

            # Data
            if packet.type == DOT11_TYPE_DATA:
                print(f"DATA Packet: {packet}")
                if self.handle_data_packet(None, packet):
                    return

            # Management
            if packet.type == DOT11_TYPE_MANAGEMENT:
                if packet.subtype == DOT11_SUBTYPE_PROBE_REQ:  # Probe request
                    if Dot11Elt in packet:
                        ssid = packet[Dot11Elt].info

                        # printd(
                        #    "Probe request for SSID %s by MAC %s"
                        #    % (ssid, packet.addr2),
                        #    Level.DEBUG,
                        # )

                        if Dot11Elt in packet and packet[Dot11Elt].len == 0:
                            # for empty return primary ssid
                            self.dot11_probe_resp(
                                self.mac, packet.addr2, self.bssids[self.mac].ssid
                            )
                        else:
                            # otherwise return match
                            for _, bss in self.bssids.items():
                                # otherwise only respond to a match
                                if bss.ssid == ssid:
                                    self.dot11_probe_resp(bss, packet.addr2, ssid)
                                    break

                elif packet.subtype == DOT11_SUBTYPE_AUTH_REQ:  # Authentication
                    bssid = packet.addr1

                    # code.interact(local=dict(globals(), **locals()))
                    if bssid in self.bssids:  # We are the receivers
                        self.bssids[bssid].sc = -1  # Reset sequence number
                        self.dot11_auth(bssid, packet.addr2)
                elif (
                    packet.subtype == DOT11_SUBTYPE_ASSOC_REQ
                    or packet.subtype == DOT11_SUBTYPE_REASSOC_REQ
                ):
                    if packet.addr1 in self.bssids:
                        self.dot11_assoc_resp(packet, packet.addr2, packet.subtype)
            # else: print(packet)
            # elif packet.type == DOT11_TYPE_CONTROL:
            #  print("control", packet.addr1)
        except SyntaxError as err:
            printd(f"Unknown error at monitor interface: {err}")

    def dot11_probe_resp(self, bssid, source, ssid):
        # printd("send probe response to " +  source)
        probe_response_packet = (
            self.get_radiotap_header()
            / Dot11(
                subtype=5,
                addr1=source,
                addr2=bssid,
                addr3=bssid,
                SC=self.bssids[bssid].next_sc(),
            )
            / Dot11ProbeResp(
                timestamp=self.current_timestamp(),
                beacon_interval=0x0064,
                cap="res8+ESS+short-preamble",  # 0x3101
            )
            / make_beacon_ies(ssid, self.channel)
        )
        if self.wpa:
            probe_response_packet[Dot11ProbeResp].cap += "privacy"
            probe_response_packet /= RSN

        self.sendp(probe_response_packet, verbose=False)

    def dot11_auth(self, bssid, receiver):
        bss = self.bssids[bssid]
        t_now = time.time()

        sta = receiver
        if receiver in bss.auth_times:
            t_then = bss.auth_times[sta]
            if t_now - t_then < BACKOFF:
                return
            bss.auth_times[sta] = t_now
        else:
            bss.auth_times[sta] = t_now

        auth_packet = (
            self.get_radiotap_header()
            / Dot11(
                subtype=0x0B,
                addr1=receiver,
                addr2=bssid,
                addr3=bssid,
                SC=bss.next_sc(),
            )
            / Dot11Auth(seqnum=0x02)
        )

        printd(
            f"Sending Authentication to {receiver} from {bssid} (0x0B)...",
            Level.DEBUG,
        )
        self.sendp(auth_packet, verbose=False)

    def create_eapol_3(self, message_2):
        t_start = time.time()
        bssid = message_2.getlayer(Dot11).addr1
        sta = message_2.getlayer(Dot11).addr2

        if sta in self.bssids:
            return

        if bssid not in self.bssids:
            return

        bss = self.bssids[bssid]

        if sta not in bss.stations:
            printd(f"bss {bss} does not know station {sta}")
            return

        if not bss.stations[sta].eapol_ready:
            # printd("station %s not eapol ready" % sta)
            # message_2.display()
            return

        eapol_payload = message_2.getlayer(EAPOL).payload
        if isinstance(eapol_payload, Raw):
            # In scapy 2.5 its raw - so we have 'load'
            eapol_key = EAPOL_KEY(eapol_payload.load)
        if isinstance(eapol_payload, scapy.layers.eap.EAPOL_KEY):
            eapol_key = eapol_payload

        snonce = eapol_key.key_nonce

        amac = bytes.fromhex(bssid.replace(":", ""))
        smac = bytes.fromhex(sta.replace(":", ""))

        stat = bss.stations[sta]
        stat.pmk = pmk = bss.pmk
        # UM do we need to sort here
        stat.PTK = PTK = customPRF512(pmk, amac, smac, stat.ANONCE, snonce)
        stat.KCK = PTK[:16]
        stat.KEK = PTK[16:32]
        stat.TK = PTK[32:48]
        stat.MIC_AP_TO_STA = PTK[48:56]
        stat.MIC_STA_TO_AP = PTK[56:64]
        stat.client_iv = count()

        # verify message 2 key mic matches before proceeding
        # verify MIC in packet makes sense
        in_eapol = message_2[EAPOL]
        eapol_payload = in_eapol.payload
        if isinstance(eapol_payload, Raw):
            # In scapy 2.5 its raw - so we have 'load'
            ek = EAPOL_KEY(eapol_payload.load)
        if isinstance(eapol_payload, scapy.layers.eap.EAPOL_KEY):
            ek = eapol_payload

        given_mic = ek.key_mic
        to_check = in_eapol.build().replace(ek.key_mic, b"\x00" * len(ek.key_mic))
        computed_mic = hmac.new(stat.KCK, to_check, hashlib.sha1).digest()[:16]

        if given_mic != computed_mic:
            printd(
                "[-] Invalid MIC from STA. Dropping EAPOL key exchange message and station"
            )
            printd("my bssid " + bssid)
            printd("my psk " + bss.psk)
            printd("amac " + bssid)
            printd("smac " + sta)

            printd(b"anonce " + binascii.hexlify(stat.ANONCE))
            printd(b"snonce " + binascii.hexlify(snonce))

            printd(b"KCK " + binascii.hexlify(stat.KCK))
            printd(b"pmk " + binascii.hexlify(stat.pmk))
            printd(b"PTK " + binascii.hexlify(stat.PTK))
            printd(b"given mic " + binascii.hexlify(given_mic))
            printd(b"computed mic " + binascii.hexlify(computed_mic))
            deauth = (
                self.get_radiotap_header()
                / Dot11(addr1=sta, addr2=bssid, addr3=bssid)
                / Dot11Deauth(reason=1)
            )
            # relax auth failure
            self.sendp(deauth, verbose=False)
            del bss.stations[sta]
            return

        bss.stations[sta].eapol_ready = False
        t1 = time.time()
        stat.KEY_IV = bytes([0 for i in range(16)])

        gtk_kde = b"".join(
            [
                chb(0xDD),
                chb(len(bss.gtk) + 6),
                b"\x00\x0f\xac",
                b"\x01\x00\x00",
                bss.gtk,
                b"\xdd\x00",
            ]
        )
        plain = pad_key_data(RSN + gtk_kde)
        keydata = aes_wrap(stat.KEK, plain)

        ek = EAPOL(version="802.1X-2004", type="EAPOL-Key") / EAPOL_KEY(
            key_descriptor_type=2,
            key_descriptor_type_version=2,
            install=1,
            key_type=1,
            key_ack=1,
            has_key_mic=1,
            secure=1,
            encrypted_key_data=1,
            key_replay_counter=2,
            key_nonce=stat.ANONCE,
            key_mic=(b"\x00" * 16),
            key_length=16,
            key=keydata,
            wpa_key_length=len(keydata),
        )

        ek.key_mic = hmac.new(stat.KCK, ek.build(), hashlib.sha1).digest()[:16]

        m3_packet = (
            self.get_radiotap_header()
            / Dot11(
                subtype=0,
                FCfield="from-DS",
                addr1=sta,
                addr2=bssid,
                addr3=bssid,
                SC=bss.next_sc(),
            )
            / LLC(dsap=0xAA, ssap=0xAA, ctrl=3)
            / SNAP(OUI=0, code=0x888E)
            / ek
        )

        t2 = time.time()
        self.sendp(m3_packet, verbose=False)
        stat.associated = True
        t_end = time.time()
        printd(
            "[+] New associated station %s for bssid %s %f %f %f"
            % (sta, bssid, t_end - t_start, t1 - t_start, t2 - t_start)
        )
        bss.stations[sta] = stat

    def prepare_message_1(self, bssid, sta):
        if sta in self.bssids:
            return

        if bssid not in self.bssids:
            return

        bss = self.bssids[bssid]

        if sta not in bss.stations:
            return

        stat = bss.stations[sta]
        if not hasattr(stat, "ANONCE"):
            stat.ANONCE = bytes([random.randrange(256) for i in range(32)])
            # gANONCE = bytes([random.randrange(256) for i in range(32)])
            # gANONCE = bytes([42 for i in range(32)])
        anonce = stat.ANONCE
        stat.m1_packet = (
            self.get_radiotap_header()
            / Dot11(
                subtype=0,
                FCfield="from-DS",
                addr1=sta,
                addr2=bssid,
                addr3=bssid,
                SC=bss.next_sc(),
            )
            / LLC(dsap=0xAA, ssap=0xAA, ctrl=3)
            / SNAP(OUI=0, code=0x888E)
            / EAPOL(version="802.1X-2004", type="EAPOL-Key")
            / EAPOL_KEY(
                key_descriptor_type=2,
                key_descriptor_type_version=2,
                key_type=1,
                key_ack=1,
                has_key_mic=0,
                key_replay_counter=1,
                key_nonce=anonce,
                key_length=16,
            )
        )
        stat.eapol_ready = True

    def create_message_1(self, bssid, sta):
        bss = self.bssids[bssid]
        stat = bss.stations[sta]
        if not stat.eapol_ready:
            printd("[-] eapol was not ready for " + sta)
            return

        printd("[+] Sent EAPOL m1 " + sta)
        self.sendp(stat.m1_packet, verbose=False)

    def dot11_assoc_resp(self, packet, sta, reassoc):
        """dot11_assoc_resp"""
        bssid = packet.addr1
        bss = self.bssids[bssid]
        if sta not in bss.stations:
            bss.stations[sta] = Station(sta)

        # bss.stations[sta].sent_assoc_resp = True
        # print("[+] already assoc resp")

        t_now = time.time()
        if sta in bss.assoc_times:
            t_then = bss.assoc_times[sta]
            if t_now - t_then < BACKOFF:
                return
            bss.assoc_times[sta] = t_now
        else:
            bss.assoc_times[sta] = t_now

        response_subtype = 0x01
        if reassoc == 0x02:
            response_subtype = 0x03
        self.eapol_ready = True
        assoc_packet = (
            self.get_radiotap_header()
            / Dot11(
                subtype=response_subtype,
                addr1=sta,
                addr2=bssid,
                addr3=bssid,
                SC=bss.next_sc(),
            )
            / Dot11AssoResp(cap="res8+ESS+short-preamble", status=0, AID=bss.next_aid())
            / make_beacon_ies(self.ssids()[0], self.channel)
        )

        if self.wpa:
            assoc_packet[Dot11AssoResp].cap += "privacy"

        printd("Sending Association Response (0x01)...")
        self.prepare_message_1(bssid, sta)
        self.sendp(assoc_packet, verbose=False)
        if self.wpa:
            self.create_message_1(bssid, sta)

    def decrypt(self, bssid, sta, packet):
        """decrypt"""
        if bssid not in self.bssids:
            return
        bss = self.bssids[bssid]
        # ccmp = packet[Dot11CCMP]
        # pn = ccmp_pn(ccmp)
        if sta not in bss.stations:
            printd(f"[-] Unknown station {sta}")
            deauth = (
                self.get_radiotap_header()
                / Dot11(addr1=sta, addr2=bssid, addr3=bssid)
                / Dot11Deauth(reason=9)
            )
            self.sendp(deauth, verbose=False)
            return None

        station = bss.stations[sta]
        return self.decrypt_ccmp(packet, station.TK, bss.gtk)

    def encrypt(self, bss, sta, packet, key_idx):
        """encrypt"""
        key = ""
        if key_idx == 0:
            pn = next(bss.stations[sta].client_iv)
            key = bss.stations[sta].TK
        else:
            pn = next(bss.group_iv)
            key = bss.gtk
        return self.encrypt_ccmp(bss, sta, packet, key, pn, key_idx)

    def enc_send(self, bss, sta, packet):
        """enc_send"""
        key_idx = 0

        if is_multicast(sta) or is_broadcast(sta):
            if len(bss.gtk) == 0:
                return
            # printd("Sending broadcast/multicast")
            key_idx = 1
        elif sta not in bss.stations or not bss.stations[sta].associated:
            printd(f"[-] Invalid station {sta} for enc_send")
            return

        x = self.get_radiotap_header()
        # print("send", packet)
        y = self.encrypt(bss, sta, packet, key_idx)
        if not y:
            raise Exception("wtfbbq")

        new_packet = x / y
        # printd(new_packet.show(dump=1))
        # print("send CCMP", key_idx, new_packet)
        self.sendp(new_packet, verbose=False)

    def encrypt_ccmp(self, bss, sta, p, tk, pn, keyid=0, amsdu_spp=False):
        """
        Takes a plaintext ethernet frame and encrypt and wrap it into a Dot11/DotCCMP
        Add the CCMP header. res0 and res1 are by default set to zero.
        """
        SA = p[Ether].src
        # DA = p[Ether].dst
        newp = Dot11(
            type="Data",
            FCfield="from-DS+protected",
            addr1=sta,
            addr2=bss.mac,
            addr3=SA,
            SC=bss.next_sc(),
        )
        newp = newp / Dot11CCMP()

        pn_bytes = pn2bytes(pn)
        newp.PN0, newp.PN1, newp.PN2, newp.PN3, newp.PN4, newp.PN5 = pn_bytes
        newp.key_id = keyid
        newp.ext_iv = 1
        priority = 0  # ...
        ccm_nonce = ccmp_get_nonce(priority, newp.addr2, pn)
        ccm_aad = ccmp_get_aad(newp, amsdu_spp)
        header = LLC(dsap=0xAA, ssap=0xAA, ctrl=3) / SNAP(OUI=0, code=p[Ether].type)
        payload = (header / p.payload).build()
        ciphertext, tag = CCMPCrypto.run_ccmp_encrypt(tk, ccm_nonce, ccm_aad, payload)
        newp.data = ciphertext + tag
        return newp

    def decrypt_ccmp(self, p, tk, gtk, verify=True, dir="to_ap"):
        """Takes a Dot11CCMP frame and decrypts it"""
        keyid = p.key_id
        if keyid == 0:
            pass
        elif keyid == 1:
            tk = gtk
        else:
            raise Exception("unknown key id", keyid)

        priority = dot11_get_priority(p)
        pn = dot11_get_iv(p)

        ccm_nonce = ccmp_get_nonce(priority, p.addr2, pn)
        ccm_aad = ccmp_get_aad(p[Dot11])

        payload = p[Dot11CCMP].data
        tag = payload[-8:]
        payload = payload[:-8]
        plaintext, valid = CCMPCrypto.run_ccmp_decrypt(
            tk, ccm_nonce, ccm_aad, payload, tag
        )

        if verify and not valid:
            printd("[-] ERROR on ccmp decrypt, invalid tag")
            return None

        llc = LLC(plaintext)
        # convert into an ethernet packet.
        # decrypting TO-AP. addr3/addr2.  if doing FROM-AP need to do addr1/addr3
        DA = p.addr3
        SA = p.addr2
        if dir == "from_ap":
            DA = p.addr1
            SA = p.addr3
        return Ether(
            addr2bin(DA)
            + addr2bin(SA)
            + struct.pack(">H", llc.payload.code)
            + llc.payload.payload.build()
        )

    def dot11_beacon(self, bssid, ssid):
        """Create beacon packet"""
        beacon_packet = (
            self.get_radiotap_header()
            / Dot11(subtype=8, addr1="ff:ff:ff:ff:ff:ff", addr2=bssid, addr3=bssid)
            / Dot11Beacon(cap="res8+ESS+short-preamble")
            / make_beacon_ies(ssid, self.channel)
        )

        if self.wpa:
            beacon_packet[Dot11Beacon].cap += "privacy"
            beacon_packet /= RSN

        # Update timestamp
        beacon_packet[Dot11Beacon].timestamp = self.current_timestamp()

        # Send
        self.sendp(beacon_packet, verbose=False)

    class BeaconTransmitter(threading.Thread):
        """BeaconTransmitter"""

        def __init__(self, ap):
            threading.Thread.__init__(self)
            self.ap = ap
            self.daemon = True
            self.interval = 0.05

        def run(self):
            while True:
                for bssid in self.ap.bssids.keys():
                    bss = self.ap.bssids[bssid]
                    self.ap.dot11_beacon(bss.mac, bss.ssid)
                # Sleep
                time.sleep(self.interval)

    def run(self):
        """run"""
        self.beaconTransmitter.start()
        for x in self.bssids:
            self.bssids[x].network.start()

        # in iface node, an interface in monitor mode is used
        # in stdio node, I/O is done via stdin and stdout.
        if self.mode == "iface":
            sniff(iface=self.iface, prn=self.recv_pkt, store=0, filter="")
            return

        assert self.mode == "stdio"
        os.set_blocking(sys.stdin.fileno(), False)

        qdata = b""
        while True:
            time.sleep(0.01)
            data = sys.stdin.buffer.read(65536)
            if data:
                qdata += data
            if len(qdata) > 4:
                wanted = struct.unpack("<L", qdata[:4])[0]
                if len(qdata) + 4 >= wanted:
                    p = RadioTap(qdata[4 : 4 + wanted])
                    self.recv_pkt(p)
                    qdata = qdata[4 + wanted :]

    def sendp(self, packet, verbose=False):
        if self.mode == "stdio":
            x = packet.build()
            sys.stdout.buffer.write(struct.pack("<L", len(x)) + x)
            sys.stdout.buffer.flush()
            return
        assert self.mode == "iface"
        # sendp(packet, iface=self.iface, verbose=False)
        # L2 sock is faster
        self.sendy.send(packet)


if __name__ == "__main__":
    ap = AP(
        "turtlenet", "password1234", mode="iface", iface="wlx00c0ca9958fb", channel=8
    )
    # nexmon example:
    # ap = AP("turtlenet", "password1234", mode="iface", iface="mon0", iface2="wlan2", channel=4)
    # ap = AP("turtlenet", "password1234", mode="iface", iface="wlan1", channel=4)
    # ap = AP("turtlenet", "password1234", mac="44:44:44:00:00:00", mode="stdio")
    ap.run()

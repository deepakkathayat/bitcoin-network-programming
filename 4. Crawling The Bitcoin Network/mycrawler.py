from lib import handshake, read_msg, serialize_msg, read_varint, read_address, BitcoinProtocolError, serialize_version_payload, read_version_payload
from io import BytesIO
import time
import socket
import threading, queue
import logging
import mydb as db
import socks

logging.basicConfig(level='INFO', filename='crawler.log')
logger = logging.getLogger(__name__)

DNS_SEEDS = [
    'dnsseed.bitcoin.dashjr.org',
    'dnsseed.bluematt.me',
    'seed.bitcoin.sipa.be',
    'seed.bitcoinstats.com',
    'seed.bitcoin.jonasschnelli.ch',
    'seed.btc.petertodd.org',
    'seed.bitcoin.sprovoost.nl',
    'dnsseed.emzy.de',
]

def create_connection(address, timeout=10):
    if 'onion' in address[0]:
        return socks.create_connection(
            address,
            proxy_type=socks.PROXY_TYPE_SOCKS5,
            proxy_addr="127.0.0.1",
            proxy_port=9050
        )
    else:
        return socket.create_connection(address, timeout)

def query_dns_seeds():
    nodes = []
    for seed in DNS_SEEDS:
        try:
            addr_info = socket.getaddrinfo(seed, 8333, 0, socket.SOCK_STREAM)
            addresses = [ai[-1][:2] for ai in addr_info]
            nodes.extend([Node(*addr) for addr in addresses])
        except OSError as e:
            logger.info("DNS seed query failed: {}".format(str(e)))
    return nodes

class Node:

    def __init__(self, ip, port, id=None, next_visit=None, visits_missed=None):
        if next_visit is None:
            next_visit = time.time()

        self.ip = ip
        self.port = port
        self.id = id
        self.next_visit = next_visit
        self.visits_missed = visits_missed

    @property
    def address(self):
        return (self.ip, self.port)

class Connection:

    def __init__(self, node, timeout):
        self.node = node
        self.timeout = timeout
        self.sock = None
        self.stream = None
        self.start = None

        # results
        self.peer_version_payload = None
        self.nodes_discovered = []

    def send_version(self):
        # send our version message
        payload = serialize_version_payload()
        msg = serialize_msg(command=b"version", payload=payload)
        self.sock.sendall(msg)

    def send_verack(self):
        msg = serialize_msg(command=b"verack")
        self.sock.sendall(msg)

    def send_pong(self, payload):
        res = serialize_msg(command=b'pong', payload=payload)
        self.sock.sendall(res)
        logger.info('Sent pong')

    def send_getaddr(self):
        self.sock.sendall(serialize_msg(b'getaddr'))

    def handle_version(self, payload):
        # save their version payload
        stream = BytesIO(payload)
        self.peer_version_payload = read_version_payload(stream)

        # ack
        self.send_verack()

    def handle_verack(self, payload):
        # Request peer's peers
        self.send_getaddr()

    def handle_ping(self, payload):
        self.send_pong(payload)

    def handle_addr(self, payload):
        payload = read_addr_payload(BytesIO(payload))
        if len(payload['addresses']) > 1:
            self.nodes_discovered.extend([
                    Node(a['ip'], a['port']) for a in payload['addresses']
            ])

    def handle_msg(self):
        msg = read_msg(self.stream)
        command = msg['command'].decode()
        logger.info('Received a "{}"'.format(command))
        method_name = "handle_{}".format(command)
        if hasattr(self, method_name):
            getattr(self, method_name)(msg['payload'])

    def remain_alive(self):
        timed_out = time.time() - self.start > self.timeout
        return not timed_out and not self.nodes_discovered

    def open(self):
        # set start time
        self.start = time.time()

        # open TCP connection
        logger.info("Connecting to {}".format(self.node.ip))
        self.sock = create_connection(self.node.address, timeout=self.timeout)
        self.stream = self.sock.makefile("rb")

        # Start version handshake
        self.send_version()

        # Handle messages until program exits
        while self.remain_alive():
            self.handle_msg()

    def close(self):
        # clean up socket's file descriptor
        if self.sock:
            self.sock.close()

class Worker(threading.Thread):

    def __init__(self, worker_inputs, worker_outputs, timeout):
        super().__init__()
        self.worker_inputs = worker_inputs
        self.worker_outputs = worker_outputs
        self.timeout = timeout

    def run(self):
        while True:
            # Get next node and connect
            node = self.worker_inputs.get()

            try:
                conn = Connection(node, timeout=self.timeout)
                conn.open()
            except (OSError, BitcoinProtocolError) as e:
                logger.info("Got error {}".format(str(e)))
            finally:
                conn.close()

            # Report results back to the crawler
            self.worker_outputs.put(conn)

class Crawler:

    def __init__(self, num_workers=10, timeout=10):
        self.timeout = timeout
        self.worker_inputs = queue.Queue()
        self.worker_outputs = queue.Queue()
        self.workers = [Worker(self.worker_inputs, self.worker_outputs, self.timeout) for _ in range(num_workers)]

    @property
    def batch_size(self):
        return len(self.workers)* 10

    def add_worker_inputs(self):
        nodes = db.next_nodes(self.batch_size)
        for node in nodes:
            self.worker_inputs.put(node)

    def process_worker_outputs(self):
        # Get connections from output queue
        conns = []
        while self.worker_outputs.qsize():
            conns.append(self.worker_outputs.get())

        # Flush connection outputs to DB
        db.process_crawler_outputs(conns)

    def seed_db(self):
        nodes = [node.__dict__ for node in query_dns_seeds()]
        db.insert_nodes(nodes)

    def print_report(self):
        print("inputs: {} |
        outputs: {} |
        visited: {} |
        total: {}", self.worker_inputs.qsize(), self.worker_outputs.qsize(), db.nodes_visited(), db.nodes_total())

    def main_loop(self):
        while True:
            # Print report
            self.print_report()
            # Fill input queue if running low
            if self.worker_inputs.qsize() < self.batch_size:
                self.add_worker_inputs()

            # Process worker outputs if running high
            if self.worker_outputs.qsize() > self.batch_size:
                self.process_worker_outputs()

            # Only check once per second
            time.sleep(1)

    def crawl(self):
        # Seed database with initial nodes from DNS seeds
        self.seed_db()

        # Fill the worker queues
        self.add_worker_inputs()

        # Start workers
        for worker in self.workers:
            worker.start()

        # Manage workers until program ends
        self.main_loop()

def read_addr_payload(stream):
    r = {}

    # read varint
    count = read_varint(stream)

    # read_address varint times. Return as list.
    r["addresses"] = [read_address(stream) for _ in range(count)]
    return r

if __name__ == '__main__':
    # Wipe the database before every run
    db.drop_and_create_tables()

    # Run the crawler
    Crawler(num_workers=25, timeout=10).crawl()

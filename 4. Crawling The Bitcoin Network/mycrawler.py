from lib import handshake, read_msg, serialize_msg

def listener(address):
    # Establish connection
    sock = handshake(address)
    stream = sock.makefile('rb')

    # Print every possible gossip message we receive
    while True:
        msg = read_msg(stream)
        command = msg['command']
        payload_len = len(msg['payload'])
        print('Received a {} containing {} bytes'.format(command, payload_len))

        # respond to pong
        if command == b'ping':
            res = serialize_msg(command=b'pong', payload=msg['payload'])
            sock.sendall(res)
            print('Sent pong')

if __name__ == '__main__':
    listener(('204.236.245.12', '8333'))
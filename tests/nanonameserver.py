# Copyright (C) Dnspython Contributors, see LICENSE for text of ISC license

import contextlib
import enum
import functools
import socket
import struct
import threading
import trio

import dns.asyncquery
import dns.message
import dns.rcode

async def read_exactly(stream, count):
    """Read the specified number of bytes from stream.  Keep trying until we
    either get the desired amount, or we hit EOF.
    """
    s = b''
    while count > 0:
        n = await stream.receive_some(count)
        if n == b'':
            raise EOFError
        count = count - len(n)
        s = s + n
    return s

class ConnectionType(enum.IntEnum):
    UDP = 1
    TCP = 2

class Server(threading.Thread):

    """The nanoserver is a nameserver skeleton suitable for faking a DNS
    server for various testing purposes.  It executes with a trio run
    loop in a dedicated thread, and is a context manager.  Exiting the
    context manager will ensure the server shuts down.

    If a port is not specified, random ports will be chosen.

    Applications should subclass the server and override the handle()
    method to determine how the server responds to queries.  The
    default behavior is to refuse everything.

    If use_thread is set to False in the constructor, then the
    server's main() method can be used directly in a trio nursery,
    allowing the server's cancellation to be managed in the Trio way.
    In this case, no thread creation ever happens even though Server
    is a subclass of thread, because the start() method is never
    called.
    """

    def __init__(self, address='127.0.0.1', port=0, enable_udp=True,
                 enable_tcp=True, use_thread=True, origin=None,
                 keyring=None):
        super().__init__()
        self.address = address
        self.port = port
        self.enable_udp = enable_udp
        self.enable_tcp = enable_tcp
        self.use_thread = use_thread
        self.origin = origin
        self.keyring = keyring
        self.left = None
        self.right = None
        self.udp = None
        self.udp_address = None
        self.tcp = None
        self.tcp_address = None

    def __enter__(self):
        (self.left, self.right) = socket.socketpair()
        # We're making the UDP socket now so it can be sent to by the
        # caller immediately (i.e. no race with the listener starting
        # in the thread).
        if self.enable_udp:
            self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, 0)
            self.udp.bind((self.address, self.port))
            self.udp_address = self.udp.getsockname()
        if self.enable_tcp:
            self.tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
            self.tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.tcp.bind((self.address, self.port))
            self.tcp.listen()
            self.tcp_address = self.tcp.getsockname()
        if self.use_thread:
            self.start()
        return self

    def __exit__(self, ex_ty, ex_va, ex_tr):
        if self.left:
            self.left.close()
        if self.use_thread and self.is_alive():
            self.join()
        if self.right:
            self.right.close()
        if self.udp:
            self.udp.close()
        if self.tcp:
            self.tcp.close()

    async def wait_for_input_or_eof(self):
        #
        # This trio task just waits for input on the right half of the
        # socketpair (the left half is owned by the context manager
        # returned by launch).  As soon as something is read, or the
        # socket returns EOF, EOFError is raised, causing a the
        # nursery to cancel all other nursery tasks, in particular the
        # listeners.
        #
        try:
            with trio.socket.from_stdlib_socket(self.right) as sock:
                self.right = None  # we own cleanup
                await sock.recv(1)
        finally:
            raise EOFError

    def handle(self, message, peer, connection_type):
        #
        # Handle message 'message'.  Override this method to change
        # how the server behaves.
        #
        # The return value is either a dns.message.Message, a bytes,
        # None, or a list of one of those.  We allow a bytes to be
        # returned for cases where handle wants to return an invalid
        # DNS message for testing purposes.  We allow None to be
        # returned to indicate there is no response.  If a list is
        # returned, then the output code will run for each returned
        # item.
        #
        try:
            r = dns.message.make_response(message)
            r.set_rcode(dns.rcode.REFUSED)
            return r
        except Exception:
            return None

    def maybe_listify(self, thing):
        if isinstance(thing, list):
            return thing
        else:
            return [thing]

    def handle_wire(self, wire, peer, connection_type):
        #
        # This is the common code to parse wire format, call handle() on
        # the message, and then generate response wire format (if handle()
        # didn't do it).
        #
        # It also handles any exceptions from handle()
        #
        # Returns a (possibly empty) list of wire format message to send.
        #
        # XXXRTH It might be nice to have a "debug mode" in the server
        # where we'd print something in all the places we're eating
        # exceptions.  That way bugs in handle() would be easier to
        # find.
        #
        items = []
        r = None
        try:
            q = dns.message.from_wire(wire, keyring=self.keyring)
        except dns.message.ShortHeader:
            # There is no hope of answering this one!
            return
        except Exception:
            # Try to make a FORMERR using just the question section.
            try:
                q = dns.message.from_wire(wire, question_only=True)
                r = dns.message.make_response(q)
                r.set_rcode(dns.rcode.FORMERR)
                items.append(r)
            except Exception:
                # We could try to make a response from only the header
                # if dnspython had a header_only option to
                # from_wire(), or if we truncated wire outselves, but
                # for now we just drop.
                return
        try:
            # items might have been appended to above, so skip
            # handle() if we already have a response.
            if not items:
                items = self.maybe_listify(self.handle(q, peer,
                                                       connection_type))
        except Exception:
            # Exceptions from handle get a SERVFAIL response.
            r = dns.message.make_response(q)
            r.set_rcode(dns.rcode.SERVFAIL)
            items = [r]

        tsig_ctx = None
        multi = len(items) > 1
        for thing in items:
            if isinstance(thing, dns.message.Message):
                out = thing.to_wire(self.origin, multi=multi, tsig_ctx=tsig_ctx)
                tsig_ctx = thing.tsig_ctx
                yield out
            else:
                yield thing

    async def serve_udp(self):
        with trio.socket.from_stdlib_socket(self.udp) as sock:
            self.udp = None  # we own cleanup
            while True:
                try:
                    (wire, peer) = await sock.recvfrom(65535)
                    for wire in self.handle_wire(wire, peer,
                                                 ConnectionType.UDP):
                        await sock.sendto(wire, peer)
                except Exception:
                    pass

    async def serve_tcp(self, stream):
        try:
            peer = stream.socket.getpeername()
            while True:
                ldata = await read_exactly(stream, 2)
                (l,) = struct.unpack("!H", ldata)
                wire = await read_exactly(stream, l)
                for wire in self.handle_wire(wire, peer, ConnectionType.TCP):
                    l = len(wire)
                    stream_message = struct.pack("!H", l) + wire
                    await stream.send_all(stream_message)
        except Exception:
            pass

    async def orchestrate_tcp(self):
        with trio.socket.from_stdlib_socket(self.tcp) as sock:
            self.tcp = None  # we own cleanup
            listener = trio.SocketListener(sock)
            async with trio.open_nursery() as nursery:
                serve = functools.partial(trio.serve_listeners, self.serve_tcp,
                                          [listener], handler_nursery=nursery)
                nursery.start_soon(serve)

    async def main(self):
        try:
            async with trio.open_nursery() as nursery:
                if self.use_thread:
                    nursery.start_soon(self.wait_for_input_or_eof)
                if self.enable_udp:
                    nursery.start_soon(self.serve_udp)
                if self.enable_tcp:
                    nursery.start_soon(self.orchestrate_tcp)
        except Exception:
            pass

    def run(self):
        if not self.use_thread:
            raise RuntimeError('start() called on a use_thread=False Server')
        trio.run(self.main)

if __name__ == "__main__":
    import sys
    import time

    async def trio_main():
        try:
            with Server(port=5354, use_thread=False) as server:
                print(f'Trio mode: listening on UDP: {server.udp_address}, ' +
                      f'TCP: {server.tcp_address}')
                async with trio.open_nursery() as nursery:
                    nursery.start_soon(server.main)
        except Exception:
            pass

    def threaded_main():
        with Server(port=5354) as server:
            print(f'Thread Mode: listening on UDP: {server.udp_address}, ' +
                  f'TCP: {server.tcp_address}')
            time.sleep(300)

    if len(sys.argv) > 1 and sys.argv[1] == 'trio':
        trio.run(trio_main)
    else:
        threaded_main()
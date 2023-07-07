import asyncio
import argparse
import curses
from .common import Service, ServiceManager, CursesService
from asyncio import StreamReader, StreamWriter
from time import time

class NotificationService(Service):
    def __init__(self, ntfy_full_url: str) -> None:
        super().__init__('notification')
        self._ntfy_full_url = ntfy_full_url

    def __post_init__(self) -> None:
        self.sub('log', self.notification)

    async def notification(self, log: tuple[int, str]): pass

class ListenerService(Service):
    def __init__(self, ip, port) -> None:
        super().__init__('listener')
        self._ip = ip
        self._port = port
        self._server = None

    async def run(self):
        def connect(reader, writer):
            self.pub('listener', (reader, writer))

        self._server = await asyncio.start_server(connect, self._ip, self._port, reuse_address=True)

class ClientHandlerService(Service):
    def __init__(self) -> None:
        super().__init__('handler')
        self._counter = 0

    def __post_init__(self) -> None:
        self.create_topic('client-join')
        self.create_topic('client-leave')
        self.create_topic('client-done')
        self.create_topic('client-progress')
        self.create_topic('client-alert')

        self.sub('listener', self.new_client)

    async def new_client(self, rw_pair: tuple[StreamReader, StreamWriter]):
        idx = self._counter
        self._counter += 1

        prefix = f'{self.name}/{idx}'
        self.pub('client-join', (idx, prefix))
        self.log(0, f'client-join {idx} {prefix}')

        self.create_topic(f'{prefix}/progress')
        self.create_topic(f'{prefix}/alert')
        self.create_topic(f'{prefix}/recv')
        self.create_topic(f'{prefix}/send')
        self.create_topic(f'{prefix}/keepalive')

        reader, writer = rw_pair

        async def client_update(line: bytes):
            args = line.decode().split()
            if args[0] == 'progress':
                if len(args) == 3:
                    progress = int(args[1]) / int(args[2])
                else:
                    try:
                        progress = float(args[1]) / 100
                    except:
                        progress = args[1]
                self.log(0, f'progress {idx} {progress}')
                self.pub(f'{prefix}/progress', progress)
                self.pub(f'client-progress', (idx, progress))
            elif args[0] == 'alert':
                self.log(0, f'alert {idx} {args[1]}')
                self.pub(f'{prefix}/alert', args[1])
                self.pub(f'client-alert', (idx, args[1]))
            elif args[0] == 'done':
                self.log(0, f'done {idx}')
                self.pub(f'client-done', idx)
            elif args[0] == 'keepalive':
                self.log(0, f'keepalive {idx}')
                self.pub(f'{prefix}/keepalive', None)

        async def _writer(line: bytes):
            writer.write(line)

        async def _reader():
            try:
                while True:
                    line = await reader.readuntil(b'\n')
                    self.pub(f'{prefix}/recv', line)
            except asyncio.IncompleteReadError:
                self.pub('client-leave', idx)
        def keepalive():
            last_time = 0
            async def _ka(n):
                nonlocal last_time
                if last_time == 0: last_time = time()
                if time() - last_time > 10:
                    self.pub(f'client-leave', idx)

        self.sub(f'{prefix}/recv', client_update)
        self.sub(f'{prefix}/send', _writer)

        self.run_as_task(_reader())

from dataclasses import dataclass

class ClientService(Service):
    @dataclass
    class Client:
        idx: int
        status: int
        name: str
        progress: float | str
        listidx: int

        def reset(self):
            self.idx = -1
            self.status = 1
            self.progress = 0

    def __init__(self) -> None:
        super().__init__('client')
        self.clients: list[ClientService.Client] = []

    def __post_init__(self) -> None:
        self.create_topic('client-refresh')
        self.create_topic('client-rename')

        self.sub('client-rename', self.on_rename)

        self.sub('client-join', self.on_client_join)
        self.sub('client-leave', self.on_client_leave)
        self.sub('client-done', self.on_client_done)
        self.sub('client-progress', self.on_client_progress)

    def _refresh(self, idx: int):
        self.pub('client-refresh', self.clients[idx])

    async def on_rename(self, clientobj: tuple[int, str]):
        idx, newname = clientobj
        self.clients[idx].name = newname
        self._refresh(idx)

    async def on_client_join(self, clientobj: tuple[int, str]):
        idx, prefix = clientobj
        if c := self.get_client(-1):
            c.idx = idx
            c.status = 0
            c.progress = 0
        else:
            c = ClientService.Client(idx, 0, prefix, 0, len(self.clients))
            self.log(0, f'client-join {c}')

            self.clients.append(c)

        self._refresh(c.listidx)

    async def on_client_progress(self, clientobj: tuple[int, float]):
        idx, progress = clientobj
        if c := self.get_client(idx):
            c.progress = progress
            self._refresh(c.listidx)
        else:
            self.log(2, f'client {idx} progressed without joining')

    def get_client(self, idx: int):
        for c in self.clients:
            if c.idx == idx:
                return c

    async def on_client_done(self, idx: int):
        if c := self.get_client(idx):
            c.status = 1
            self._refresh(c.listidx)
        else:
            self.log(2, f'client {idx} was done without joining')

    async def on_client_leave(self, idx: int):
        if c := self.get_client(idx):
            c.idx = -1

            if c.status != 1:
                self.log(2, f'client {idx} ({c.name}) left without being done')
                c.status = 2

            self._refresh(c.listidx)
        else:
            self.log(2, f'client {idx} left without joining')

class ScreenService(Service):
    def __init__(self) -> None:
        super().__init__('screen')
        self._names: dict[int, str] = {}
        self._cursor: int = 0

    def __post_init__(self) -> None:
        async def on_cursor(c):
            self._cursor = c

        async def on_input(c):
            if c != '\n': return
            cursor = self._cursor
            if cursor in self._names:
                name = self._names[cursor]
                self.pub('edit/start', name)

        async def on_finish_edit(newname):
            self.pub('client-rename', (self._cursor, newname))

        async def on_refresh(client: ClientService.Client):
            self._names[client.listidx] = client.name
            statuses = [
                '-- GRAVANDO --',
                '!! SUCESSO !! ',
                '#### ERRO ####'
            ]
            status = statuses[client.status]
            if isinstance(client.progress, float):
                progress = f'{client.progress*100:.2f}%'
            else:
                progress = client.progress
            line = f'{status} | {client.name:<16}| {progress}'
            self.pub('line', (client.listidx, line))

        self.sub('cursor', on_cursor)
        self.sub('input', on_input)
        self.sub('edit/end', on_finish_edit)
        self.sub('client-refresh', on_refresh)


class Server(ServiceManager):
    def __init__(self, stdscr: curses.window, ip, port, ntfy_full_url) -> None:
        super().__init__()
        self._notif_svc = self.register(NotificationService(ntfy_full_url))
        self._curses_svc = self.register(CursesService(stdscr))
        self._listener_svc = self.register(ListenerService(ip, port))
        self._handler_svc = self.register(ClientHandlerService())
        self._client_svc = self.register(ClientService())
        self._screen_svc = self.register(ScreenService())

        self._listener_svc.start()

DEFAULT_NTFY_URL = 'https://ntfy.sh/{topic}'

async def amain(*,
                stdscr: curses.window,
                ip_block: str,
                interface: str,
                listen_ip: str,
                listen_port: int,
                ntfy_url: str = DEFAULT_NTFY_URL,
                ntfy_topic: str | None = None):
    server = Server(stdscr, listen_ip, listen_port, ntfy_full_url=ntfy_url)

    await server.wait()

def main(argv: list[str], prog: str = 'server'):
    parser = argparse.ArgumentParser(prog)
    parser.add_argument('--ip-block')
    parser.add_argument('--interface')
    parser.add_argument('--listen-ip', default='0.0.0.0')
    parser.add_argument('--listen-port', type=int, default=12345)
    parser.add_argument('--ntfy-url', default=DEFAULT_NTFY_URL)
    parser.add_argument('--ntfy-topic')

    args = parser.parse_args(argv)

    if args.ntfy_url:
        assert '{topic}' in args.ntfy_url

    def wrapped_main(stdscr: curses.window):
        asyncio.run(amain(
            stdscr=stdscr,
            ip_block=args.ip_block,
            interface=args.interface,
            listen_ip=args.listen_ip,
            listen_port=args.listen_port,
            ntfy_url=args.ntfy_url,
            ntfy_topic=args.ntfy_topic
        ))

    curses.wrapper(wrapped_main)

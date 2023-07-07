import asyncio
from asyncio import Queue, Task
import curses
import sys
from typing import Callable

class QuitEv: pass
QUIT = QuitEv()

class Service:
    def __init__(self, name: str) -> None:
        self._subs: dict[str, Queue] = {}
        self._name = name
        self._queue_consumers: list[Task] = []
        self._service_task: Task | None = None
        self._manager: 'ServiceManager'

    def __post_init__(self) -> None: ...

    async def on_pub(self, topic: str, obj):
        assert topic in self._subs
        await self._subs[topic].put(obj)

    async def run(self):
        raise NotImplementedError()

    def start(self):
        self._service_task = asyncio.create_task(self.run())

    @property
    def name(self):
        return self._name

    def sub(self, topic: str, func):
        assert topic not in self._subs
        assert self._manager is not None

        queue = Queue()

        async def _topic_listener():
            while True:
                e = await queue.get()
                if e == QUIT: break

                await func(e)

        self._subs[topic] = queue
        task = asyncio.create_task(_topic_listener())
        self._queue_consumers.append(task)
        self._manager._register_sub(topic, self)

    def pub(self, topic: str, obj):
        assert self._manager is not None

        self._manager.pub(topic, obj)

    def create_topic(self, topic: str):
        assert self._manager is not None

        self._manager.create_topic(topic)

    def run_as_task(self, coro):
        assert self._manager is not None
        self._manager.run_as_task(coro)

    def log(self, level, txt):
        self.pub('log', (level, txt))

class TaskAwaiter:
    def __init__(self, tasks, manager):
        self._tasks = tasks
        self._own_task = asyncio.create_task(self._await_tasks())
        self._manager = manager
        self._idx = manager._awaiter_counter
        manager._task_awaiters[self._idx] = self
        manager._awaiter_counter += 1
        self._done = asyncio.Event()

    async def _await_tasks(self):
        if self._tasks:
            await asyncio.wait(self._tasks)
        del self._manager._task_awaiters[self._idx]
        self._done.set()

    def done(self):
        return self._done.wait()

class ServiceManager:
    def __init__(self) -> None:
        self._services: dict[str, Service] = {}
        self._topics: dict[str, set[str]] = {}
        self._task_awaiters: dict[int, TaskAwaiter] = {}
        self._awaiter_counter = 0
        self._quit = asyncio.Event()
        self.create_topic('log')

    def _get_topic(self, topic: str):
        assert topic in self._topics

        return self._topics[topic]

    def create_topic(self, topic: str):
        assert topic not in self._topics

        self._topics[topic] = set()

    def pub(self, topic: str, obj):
        tasks = []
        for sub in self._get_topic(topic):
            task = asyncio.create_task(self._services[sub].on_pub(topic, obj))
            tasks.append(task)
        return self.run_tasks(tasks)

    def close_topic(self, topic: str):
        self.pub(topic, QUIT)
        del self._topics[topic]

    def _register_sub(self, topic: str, service: Service):
        assert service.name in self._services

        self._get_topic(topic).add(service.name)

    def register(self, service: Service):
        self._services[service.name] = service
        self.create_topic(service.name)
        service._manager = self
        service.__post_init__()
        return service

    def run_tasks(self, tasks):
        return TaskAwaiter(tasks, self)

    def run_as_task(self, coro):
        return TaskAwaiter([asyncio.create_task(coro)], self)

    async def wait(self):
        await self._quit.wait()
        while self._task_awaiters:
            awaiter = list(self._task_awaiters.values())[0]
            await awaiter.done()

class InputService(Service):
    def __init__(self, window: curses.window) -> None:
        super().__init__('raw-input')
        self._window = window

    async def run(self):
        self._window.nodelay(True)
        self._window.keypad(True)

        loop = asyncio.get_event_loop()
        input_ev = asyncio.Event()
        loop.add_reader(sys.stdin, input_ev.set)

        while True:
            await input_ev.wait()
            input_ev.clear()

            try:
                c = self._window.get_wch()
            except curses.error:
                continue

            self.pub('raw-input', c)

class CursesService(Service):
    def __init__(self, stdscr: curses.window) -> None:
        super().__init__('curses')
        self._inp_win = curses.newwin(1, 1, 0, 0)
        self._stdscr = stdscr
        self._buf = []
        self._editing = False
        self._edit_cursor = 0
        self._cursor = 0
        self._color: int = 0

    def __post_init__(self) -> None:
        self._input_svc = self._manager.register(InputService(self._inp_win))
        self._input_svc.start()

        curses.init_pair(1, curses.COLOR_RED, curses.COLOR_BLACK)
        self._stdscr.refresh()

        self.create_topic('line')
        self.create_topic('cursor')
        self.create_topic('color')
        self.create_topic('edit/start')
        self.create_topic('edit/end')
        self.create_topic('input')

        self.sub('line', self.on_line)
        self.sub('color', self.on_color)
        self.sub('raw-input', self.on_input)
        self.sub('edit/start', self.on_start_edit)
        self.sub('log', self.on_log)

    async def on_input(self, c):
        if self._editing:
            if c == curses.KEY_LEFT:
                self._edit_cursor = max(self._edit_cursor - 1, 0)
            elif c == curses.KEY_RIGHT:
                self._edit_cursor = min(self._edit_cursor + 1, len(self._buf))
            elif c == curses.KEY_BACKSPACE:
                if self._edit_cursor > 0:
                    self._edit_cursor -= 1
                    del self._buf[self._edit_cursor]
            elif c == '\n' or c == curses.KEY_ENTER:
                self.pub('edit/end', ''.join(self._buf))
                self._buf = []
                self._edit_cursor = 0
                self._editing = False
            elif isinstance(c, str):
                self._buf.insert(self._edit_cursor, c)
                self._edit_cursor += 1
            self._stdscr.addstr(curses.LINES-1, 0, ''.join(self._buf), self._color)
            self._stdscr.clrtoeol()

            self.refresh()
        else:
            self.log(0, f'input {c!r}')

            if c == curses.KEY_UP:
                self.set_cursor(self._cursor - 1)
            elif c == curses.KEY_DOWN:
                self.set_cursor(self._cursor + 1)
            self.pub('input', c)

    def refresh(self):
        if self._editing:
            self._stdscr.move(curses.LINES-1, self._edit_cursor)
        else:
            self._stdscr.move(self._cursor, 0)
        self._stdscr.refresh()

    def set_cursor(self, cursor: int):
        if cursor != self._cursor:
            self._cursor = max(min(cursor, curses.LINES-3), 0)
            self.pub('cursor', cursor)
        self.refresh()

    async def on_start_edit(self, text):
        self._editing = True
        self._buf = list(text)
        self._stdscr.addstr(curses.LINES-1, 0, ''.join(self._buf), self._color)
        self.refresh()

    async def on_line(self, lineobj: tuple[int, str]):
        row, line = lineobj
        self._stdscr.addstr(row, 1, line, self._color)
        self._stdscr.clrtoeol()
        self.refresh()

    async def on_log(self, logobj: tuple[int, str]):
        severity = logobj[0]
        old_color = self._color
        #if severity == 0: return
        if severity >= 2:
            self._color = curses.color_pair(1)
        self._stdscr.addstr(curses.LINES-2, 0, logobj[1], self._color)
        self._color = old_color
        self._stdscr.clrtoeol()
        self.refresh()

    async def on_color(self, color):
        self._color = color

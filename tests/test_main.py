"""Process supervision tests. Child bot/database operations are mocked."""

import asyncio
import multiprocessing
import os
import queue
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import main as app


def _fake_child(role, stop_event, output):
    """Exercise the real child lifecycle without Telegram or PostgreSQL access."""
    async def service(*args):
        output.put((role, "started", os.getpid()))
        try:
            await asyncio.Future()
        finally:
            output.put((role, "stopped", os.getpid()))
    async def close_bot():
        output.put((role, "bot closed", os.getpid()))
    async def close_database():
        output.put((role, "database closed", os.getpid()))
    module = types.ModuleType("bot.bot")
    module.bot = types.SimpleNamespace(session=types.SimpleNamespace(close=close_bot))
    module.start_polling = service
    with patch.dict(sys.modules, {"bot.bot": module}), patch("reminder.poll_users", service), patch.object(app, "close_db", close_database):
        app._run_worker(role, stop_event)


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def context(self, exitcode=0):
        context = Mock()
        event = context.Event.return_value
        event.is_set.return_value = False
        processes = [Mock(name="bot process"), Mock(name="notification process")]
        for process, name in zip(processes, ("bot", "notifications")):
            process.name = name
            process.exitcode = exitcode if name == "bot" else None
            process.is_alive.return_value = False
        context.Process.side_effect = processes
        return context, event, processes

    async def test_starts_both_processes_and_stops_them_when_one_exits(self):
        context, event, processes = self.context()
        with patch.object(app, "init_db", AsyncMock()) as init, patch.object(app, "close_db", AsyncMock()) as close, patch.object(app.multiprocessing, "get_context", return_value=context) as get_context, patch.object(app.signal, "signal"):
            await app.main()
        init.assert_awaited_once()
        close.assert_awaited_once()
        get_context.assert_called_once_with("spawn")
        self.assertEqual([call.kwargs["name"] for call in context.Process.call_args_list], ["bot", "notifications"])
        for call in context.Process.call_args_list:
            self.assertIs(call.kwargs["target"], app._run_worker)
        for process in processes:
            process.start.assert_called_once()
            process.join.assert_called_once_with(timeout=10)
            process.close.assert_called_once()
        event.set.assert_called_once()

    async def test_child_failure_stops_sibling_and_is_reported(self):
        context, event, processes = self.context(exitcode=7)
        with patch.object(app, "init_db", AsyncMock()), patch.object(app, "close_db", AsyncMock()), patch.object(app.multiprocessing, "get_context", return_value=context), patch.object(app.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "bot process exited with code 7"):
                await app.main()
        event.set.assert_called_once()
        processes[1].join.assert_called_once_with(timeout=10)

    async def test_unresponsive_process_is_terminated(self):
        context, event, processes = self.context()
        processes[1].is_alive.side_effect = [True, False]
        with patch.object(app, "init_db", AsyncMock()), patch.object(app, "close_db", AsyncMock()), patch.object(app.multiprocessing, "get_context", return_value=context), patch.object(app.signal, "signal"):
            await app.main()
        processes[1].terminate.assert_called_once()
        processes[1].kill.assert_not_called()
        self.assertEqual(processes[1].join.call_count, 2)

    async def test_failed_initialization_does_not_start_processes(self):
        with patch.object(app, "init_db", AsyncMock(side_effect=RuntimeError("offline"))), patch.object(app, "close_db", AsyncMock()) as close, patch.object(app.multiprocessing, "get_context") as get_context:
            with self.assertRaisesRegex(RuntimeError, "offline"):
                await app.main()
        close.assert_awaited_once()
        get_context.assert_not_called()


class SpawnTests(unittest.TestCase):
    def test_real_processes_have_distinct_pids_and_close_connections(self):
        context = multiprocessing.get_context("spawn")
        stop_event = context.Event()
        output = context.Queue()
        processes = [context.Process(target=_fake_child, args=(role, stop_event, output)) for role in ("bot", "notifications")]
        messages = []
        try:
            for process in processes:
                process.start()
            for _ in range(2):
                messages.append(output.get(timeout=15))
            self.assertEqual({m[0] for m in messages}, {"bot", "notifications"})
            self.assertEqual({m[1] for m in messages}, {"started"})
            self.assertEqual(len({m[2] for m in messages}), 2)
            self.assertNotIn(os.getpid(), {m[2] for m in messages})
            stop_event.set()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            for _ in range(6):
                messages.append(output.get(timeout=5))
            for role in ("bot", "notifications"):
                self.assertEqual([m[1] for m in messages if m[0] == role], ["started", "stopped", "bot closed", "database closed"])
        finally:
            stop_event.set()
            for process in processes:
                if process.pid is not None:
                    if process.is_alive():
                        process.terminate()
                    process.join(timeout=5)
                    process.close()
            output.close()
            output.join_thread()


if __name__ == "__main__":
    unittest.main()

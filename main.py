import asyncio
import multiprocessing
import signal

from dotenv import load_dotenv

from database.crud import close_db, init_db

load_dotenv()


async def _wait_for_stop(stop_event) -> None:
    while not stop_event.is_set():
        await asyncio.sleep(0.2)


async def _worker(role: str, stop_event) -> None:
    # Import inside the spawned child: no Bot or HTTP session is shared.
    from bot.bot import bot, start_polling
    from reminder import poll_users

    service = asyncio.create_task(
        start_polling() if role == "bot" else poll_users(bot), name=role
    )
    stopping = asyncio.create_task(_wait_for_stop(stop_event))
    try:
        done, _ = await asyncio.wait({service, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if service in done:
            await service  # Propagate failures to the child's exit status.
    finally:
        service.cancel()
        stopping.cancel()
        await asyncio.gather(service, stopping, return_exceptions=True)
        try:
            await bot.session.close()
        finally:
            await close_db()


def _run_worker(role: str, stop_event) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    asyncio.run(_worker(role, stop_event))


async def main() -> None:
    try:
        await init_db()
    finally:
        # Initialization's connection pool belongs only to the parent.
        await close_db()

    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    processes = [
        context.Process(target=_run_worker, args=(role, stop_event), name=role)
        for role in ("bot", "notifications")
    ]
    previous_handlers = {}
    started = []
    failed = None
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, lambda *_: stop_event.set())
        for process in processes:
            process.start()
            started.append(process)
        while not stop_event.is_set():
            exited = next((p for p in started if p.exitcode is not None), None)
            if exited is not None:
                if exited.exitcode != 0:
                    failed = (exited.name, exited.exitcode)
                break
            await asyncio.sleep(0.2)
    finally:
        stop_event.set()
        try:
            for process in started:
                process.join(timeout=10)
            for process in started:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
        finally:
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)
    if failed is not None:
        raise RuntimeError(f"{failed[0]} process exited with code {failed[1]}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    asyncio.run(main())

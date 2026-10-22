"""Single hosting command for web + worker: python run.py."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main():
    if not os.getenv('MAX_BOT_TOKEN','').strip():
        raise SystemExit('Добавьте MAX_BOT_TOKEN в защищённые переменные хостинга')
    root = Path(__file__).resolve().parent
    processes = []
    stopping = False

    def stop(signum=None, frame=None):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        processes.append(subprocess.Popen([sys.executable, '-m', 'gunicorn', '--bind',
            '0.0.0.0:'+os.getenv('PORT','3000'), '--workers', '1', '--access-logfile', '-', 'app:app'], cwd=root))
        processes.append(subprocess.Popen([sys.executable, 'worker.py'], cwd=root))
        while not stopping:
            for process in processes:
                code = process.poll()
                if code is not None:
                    print(f'Дочерний процесс завершился (код {code}); остановка приложения.', file=sys.stderr)
                    return code or 1
            time.sleep(.5)
        return 0
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == '__main__':
    sys.exit(main())

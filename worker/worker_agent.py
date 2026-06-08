import json
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


# Абсолютный путь к корню проекта:
# F:\Dev\AutomationUnrealPipeline
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# Локальный config worker'а.
# Этот файл НЕ коммитим в Git, потому что позже в нём будут:
# - реальные пути к UE на твоём ПК;
# - адрес VPS backend;
# - worker token.
CONFIG_PATH = PROJECT_ROOT / "config" / "worker.local.json"


def load_worker_config() -> Dict[str, Any]:
    """
    Загружает настройки worker'а из config/worker.local.json.

    Почему config отдельно от кода:
    - код остаётся одинаковым на всех машинах;
    - пути к UE могут отличаться;
    - backend_url может быть локальным или серверным;
    - секреты не попадают в Git.
    """

    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Worker config not found: {CONFIG_PATH}. "
            f"Create it from config/worker.local.example.json"
        )

    with CONFIG_PATH.open("r", encoding="utf-8") as config_file:
        return json.load(config_file)


CONFIG = load_worker_config()


# Эти значения теперь берутся из config-файла, а не зашиты в код.
BACKEND_URL = CONFIG["backend_url"]
WORKER_NAME = CONFIG["worker_name"]

POLL_INTERVAL_SECONDS = CONFIG.get("poll_interval_seconds", 2)
PROCESS_TIMEOUT_SECONDS = CONFIG.get("process_timeout_seconds", 60)


# Пока engines не используются напрямую.
# На следующем этапе добавим job check_engine_path,
# который проверит существование UnrealEditor-Cmd.exe / UE4Editor-Cmd.exe.
ENGINES = CONFIG.get("engines", {})


def log(job_id: str, message: str) -> None:
    """
    Логирует сообщение локально в консоль worker'а
    и одновременно отправляет это сообщение в backend.

    Это важно: backend должен видеть историю выполнения job,
    даже если мы не смотрим в терминал worker'а.
    """

    print(message)

    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/logs",
        params={"message": message},
        timeout=10,
    )

    # Если backend вернул ошибку, сразу выбрасываем исключение.
    # Так мы не будем молча игнорировать проблемы синхронизации.
    response.raise_for_status()


def update_status(job_id: str, status: str) -> None:
    """
    Обновляет статус job на backend.

    Пример жизненного цикла:
    queued -> claimed -> running -> success
    queued -> claimed -> running -> failed
    """

    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/status",
        params={"status": status},
        timeout=10,
    )

    response.raise_for_status()


def complete_job(job_id: str, result: Dict[str, Any]) -> None:
    """
    Завершает job успешным результатом.

    result — это машинно-читаемый отчёт.
    Позже здесь будут:
    - exit_code Unreal-процесса
    - путь к report.json
    - путь к build artifact
    - статистика validation/build
    """

    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/complete",
        json=result,
        timeout=10,
    )

    response.raise_for_status()


def fail_job(job_id: str, result: Dict[str, Any]) -> None:
    """
    Завершает job ошибкой.

    Важно: failed job — это не авария всей системы.
    Это нормальное состояние pipeline:
    validation может не пройти,
    build может упасть,
    процесс может вернуть exit_code != 0.
    """

    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/fail",
        json=result,
        timeout=10,
    )

    response.raise_for_status()


def get_next_job() -> Dict[str, Any]:
    """
    Запрашивает у backend следующую задачу.

    Backend сам меняет статус первой queued-задачи на claimed.
    Это значит, что другой worker уже не должен взять эту же job.

    Пока у нас один worker, но архитектурно мы уже готовимся
    к нескольким worker-машинам.
    """

    response = requests.get(
        f"{BACKEND_URL}/jobs/next",
        timeout=10,
    )

    response.raise_for_status()
    return response.json()


def execute_ping_job(job: Dict[str, Any]) -> None:
    """
    Тестовая job.

    Она не запускает внешний процесс и не трогает Unreal.
    Нужна только для проверки базовой связки:

    Backend -> Worker -> Logs -> Result -> Backend

    Если ping работает, значит коммуникационный слой исправен.
    """

    job_id = job["id"]

    try:
        update_status(job_id, "running")

        log(job_id, "[Worker] Starting ping job")
        time.sleep(1)

        payload = job.get("payload", {})
        message = payload.get("message", "No message")

        log(job_id, f"[Worker] Payload message: {message}")
        time.sleep(1)

        log(job_id, "[Worker] Ping job completed")

        complete_job(
            job_id,
            {
                "worker": WORKER_NAME,
                "result": "pong",
                "received_message": message,
            },
        )

    except Exception as error:
        # Любая ошибка внутри job должна превратиться в failed result,
        # а не убить весь worker-процесс.
        fail_job(
            job_id,
            {
                "worker": WORKER_NAME,
                "error": str(error),
            },
        )


def validate_process_payload(payload: Dict[str, Any]) -> tuple[List[str], Optional[str]]:
    """
    Проверяет payload для run_process job.

    Мы специально требуем command как list[str], а не строку.

    Хорошо:
        ["cmd", "/c", "echo", "Hello"]

    Плохо:
        "cmd /c echo Hello"

    Почему list лучше:
    - меньше проблем с кавычками;
    - безопаснее;
    - проще переносить на UnrealEditor-Cmd.exe;
    - subprocess получает аргументы явно.
    """

    command = payload.get("command")
    cwd = payload.get("cwd")

    if not isinstance(command, list):
        raise ValueError(
            "payload.command must be a list, for example: "
            "['cmd', '/c', 'echo', 'hello']"
        )

    if len(command) == 0:
        raise ValueError("payload.command cannot be empty")

    for item in command:
        if not isinstance(item, str):
            raise ValueError("Every payload.command item must be a string")

    if cwd is not None and not isinstance(cwd, str):
        raise ValueError("payload.cwd must be a string or null")

    return command, cwd


def execute_run_process_job(job: Dict[str, Any]) -> None:
    """
    Запускает внешний процесс на локальной машине.

    Сейчас мы тестируем на простой команде:
        cmd /c echo Hello

    Позже через этот же механизм будем запускать:
        UnrealEditor-Cmd.exe
        UE4Editor-Cmd.exe
        RunUAT.bat
        BuildGraph

    Это ключевой переход:
    worker становится не просто тестовым клиентом,
    а локальным исполнителем pipeline-задач.
    """

    job_id = job["id"]

    try:
        update_status(job_id, "running")

        payload = job.get("payload", {})
        command, cwd = validate_process_payload(payload)

        log(job_id, "[Worker] Starting run_process job")
        log(job_id, f"[Worker] Command: {command}")

        if cwd:
            log(job_id, f"[Worker] Working directory: {cwd}")

        # Запускаем процесс.
        #
        # shell=False — принципиально:
        # мы не отдаём строку в shell-интерпретатор,
        # а передаём аргументы напрямую.
        #
        # stdout=subprocess.PIPE — читаем вывод процесса.
        # stderr=subprocess.STDOUT — объединяем stderr и stdout,
        # чтобы все сообщения шли в один pipeline-log.
        process = subprocess.Popen(
            command,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
        )

        captured_output: List[str] = []

        try:
            # Читаем stdout построчно и сразу отправляем строки в backend.
            #
            # Для Unreal это будет особенно полезно:
            # можно будет видеть лог commandlet/build прямо в dashboard.
            if process.stdout is not None:
                for line in process.stdout:
                    clean_line = line.rstrip()
                    captured_output.append(clean_line)
                    log(job_id, f"[Process] {clean_line}")

            # wait() возвращает exit code.
            # 0 обычно означает успех.
            # Всё, что не 0, считаем ошибкой job.
            exit_code = process.wait(timeout=PROCESS_TIMEOUT_SECONDS)

        except subprocess.TimeoutExpired:
            # Если процесс завис, убиваем его.
            # Для production позже стоит делать более аккуратный shutdown,
            # но для MVP kill() достаточно.
            process.kill()

            log(job_id, "[Worker] Process timeout. Process killed.")

            fail_job(
                job_id,
                {
                    "worker": WORKER_NAME,
                    "error": "Process timeout",
                    "timeout_seconds": PROCESS_TIMEOUT_SECONDS,
                    "command": command,
                    "cwd": cwd,
                },
            )
            return

        log(job_id, f"[Worker] Process finished with exit code: {exit_code}")

        # В result не кладём весь огромный stdout.
        # Пока сохраняем только последние 20 строк.
        #
        # Позже полный лог лучше писать в файл-артефакт:
        # artifacts/job_id/process.log
        result = {
            "worker": WORKER_NAME,
            "command": command,
            "cwd": cwd,
            "exit_code": exit_code,
            "output_tail": captured_output[-20:],
        }

        if exit_code == 0:
            complete_job(job_id, result)
        else:
            fail_job(job_id, result)

    except Exception as error:
        # Если ошибка возникла до старта процесса или во время обработки payload,
        # тоже переводим job в failed.
        fail_job(
            job_id,
            {
                "worker": WORKER_NAME,
                "error": str(error),
            },
        )


def execute_check_engine_path_job(job: Dict[str, Any]) -> None:
    """
    Проверяет, что worker видит исполняемый файл Unreal Engine.

    Это первый Unreal-specific шаг pipeline.

    Backend говорит:
        engine_version = "5.5"

    Worker смотрит в config/worker.local.json:
        engines["5.5"]["editor_cmd"]

    Потом проверяет:
        существует ли этот файл на диске.

    Мы пока не запускаем Unreal.
    Только проверяем, что путь корректный.
    """

    job_id = job["id"]

    try:
        update_status(job_id, "running")

        engine_version = job.get("engine_version")

        if not engine_version:
            raise ValueError("engine_version is required for check_engine_path job")

        engine_config = ENGINES.get(engine_version)

        if not engine_config:
            raise ValueError(f"Engine version '{engine_version}' is not configured in worker.local.json")

        editor_cmd = engine_config.get("editor_cmd")

        if not editor_cmd:
            raise ValueError(f"editor_cmd is missing for engine version '{engine_version}'")

        editor_path = Path(editor_cmd)

        log(job_id, "[Worker] Starting check_engine_path job")
        log(job_id, f"[Worker] Engine version: {engine_version}")
        log(job_id, f"[Worker] Editor command path: {editor_path}")

        exists = editor_path.exists()
        is_file = editor_path.is_file()

        result = {
            "worker": WORKER_NAME,
            "engine_version": engine_version,
            "editor_cmd": str(editor_path),
            "exists": exists,
            "is_file": is_file,
        }

        if exists and is_file:
            log(job_id, "[Worker] Engine executable found")
            complete_job(job_id, result)
        else:
            log(job_id, "[Worker] Engine executable not found")

            result["error"] = "Editor command not found"
            fail_job(job_id, result)

    except Exception as error:
        fail_job(
            job_id,
            {
                "worker": WORKER_NAME,
                "error": str(error),
            },
        )

def execute_launch_engine_version_job(job: Dict[str, Any]) -> None:
    """
    Запускает Unreal Engine executable для указанной версии движка.

    Это следующий шаг после check_engine_path.

    check_engine_path проверял:
        существует ли UnrealEditor-Cmd.exe / UE4Editor-Cmd.exe

    launch_engine_version проверяет:
        можем ли мы реально запустить этот .exe через worker

    На этом этапе мы НЕ запускаем .uproject и НЕ запускаем Commandlet.
    Мы используем безопасный аргумент -help, чтобы проверить сам запуск процесса.
    """

    job_id = job["id"]

    try:
        update_status(job_id, "running")

        engine_version = job.get("engine_version")

        if not engine_version:
            raise ValueError("engine_version is required for launch_engine_version job")

        engine_config = ENGINES.get(engine_version)

        if not engine_config:
            raise ValueError(f"Engine version '{engine_version}' is not configured in worker.local.json")

        editor_cmd = engine_config.get("editor_cmd")

        if not editor_cmd:
            raise ValueError(f"editor_cmd is missing for engine version '{engine_version}'")

        editor_path = Path(editor_cmd)

        if not editor_path.exists() or not editor_path.is_file():
            raise FileNotFoundError(f"Editor executable not found: {editor_path}")

        payload = job.get("payload", {})

        # По умолчанию запускаем движок с -help.
        # Это безопасный smoke-test: процесс должен стартовать, вывести help/log и завершиться.
        args = payload.get("args", ["-help"])

        if not isinstance(args, list):
            raise ValueError("payload.args must be a list of strings")

        for arg in args:
            if not isinstance(arg, str):
                raise ValueError("Every payload.args item must be a string")

        # Для первого запуска Unreal может быть медленнее обычного,
        # поэтому даём возможность переопределить timeout из payload.
        timeout_seconds = payload.get("timeout_seconds", 120)

        if not isinstance(timeout_seconds, int):
            raise ValueError("payload.timeout_seconds must be an integer")

        command = [str(editor_path)] + args

        log(job_id, "[Worker] Starting launch_engine_version job")
        log(job_id, f"[Worker] Engine version: {engine_version}")
        log(job_id, f"[Worker] Editor executable: {editor_path}")
        log(job_id, f"[Worker] Launch args: {args}")
        log(job_id, f"[Worker] Timeout seconds: {timeout_seconds}")

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            shell=False,
        )

        captured_output: List[str] = []

        try:
            if process.stdout is not None:
                for line in process.stdout:
                    clean_line = line.rstrip()
                    captured_output.append(clean_line)

                    # Не спамим слишком сильно: Unreal может выводить много строк.
                    # Но на текущем этапе полезно видеть хотя бы основной stdout.
                    log(job_id, f"[Unreal] {clean_line}")

            exit_code = process.wait(timeout=timeout_seconds)

        except subprocess.TimeoutExpired:
            process.kill()

            log(job_id, "[Worker] Unreal process timeout. Process killed.")

            fail_job(
                job_id,
                {
                    "worker": WORKER_NAME,
                    "engine_version": engine_version,
                    "editor_cmd": str(editor_path),
                    "args": args,
                    "error": "Unreal process timeout",
                    "timeout_seconds": timeout_seconds,
                    "output_tail": captured_output[-30:],
                },
            )
            return

        log(job_id, f"[Worker] Unreal process finished with exit code: {exit_code}")

        result = {
            "worker": WORKER_NAME,
            "engine_version": engine_version,
            "editor_cmd": str(editor_path),
            "args": args,
            "exit_code": exit_code,
            "output_tail": captured_output[-30:],
        }

        if exit_code == 0:
            complete_job(job_id, result)
        else:
            fail_job(job_id, result)

    except Exception as error:
        fail_job(
            job_id,
            {
                "worker": WORKER_NAME,
                "error": str(error),
            },
        )

def dispatch_job(job: Dict[str, Any]) -> None:
    """
    Центральный диспетчер job-типов.

    Backend присылает job с полем "type".
    Worker по этому полю решает, какой обработчик вызвать.

    Сейчас поддерживаются:
    - ping
    - run_process

    Позже добавим:
    - unreal_commandlet
    - buildgraph
    - asset_validation
    - import_assets
    - generate_level
    """

    job_id = job["id"]
    job_type = job["type"]

    print(f"[Worker] Claimed job: {job_id}")
    print(f"[Worker] Job type: {job_type}")

    if job_type == "ping":
        execute_ping_job(job)
        return

    if job_type == "run_process":
        execute_run_process_job(job)
        return

    if job_type == "check_engine_path":
        execute_check_engine_path_job(job)
        return

    if job_type == "launch_engine_version":
        execute_launch_engine_version_job(job)
        return

    # Если backend прислал неизвестный тип job,
    # worker не должен падать.
    # Он должен корректно пометить задачу как failed.
    fail_job(
        job_id,
        {
            "worker": WORKER_NAME,
            "error": f"Unsupported job type: {job_type}",
        },
    )


def main() -> None:
    """
    Главный цикл worker'а.

    Worker работает постоянно:
    1. спрашивает backend о новой job;
    2. если job есть — выполняет;
    3. если job нет — ждёт;
    4. если backend недоступен — не падает, а пробует снова.

    Это pull-модель:
    домашний ПК сам подключается к серверу.

    Это важно для будущей серверной архитектуры:
    нам не нужно открывать входящие порты на домашнем ПК.
    """

    print("[Worker] Started")
    print(f"[Worker] Name: {WORKER_NAME}")
    print(f"[Worker] Backend URL: {BACKEND_URL}")

    last_idle_message_time = 0.0

    while True:
        try:
            job = get_next_job()

            if "id" not in job:
                current_time = time.time()

                # Чтобы не засорять терминал, сообщение об ожидании
                # выводим не каждые 2 секунды, а примерно раз в 10 секунд.
                if current_time - last_idle_message_time > 10:
                    print("[Worker] No queued jobs. Waiting...")
                    last_idle_message_time = current_time

                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            dispatch_job(job)

        except requests.exceptions.ConnectionError:
            # Backend может быть временно выключен.
            # Worker не должен завершаться из-за этого.
            print("[Worker] Cannot connect to backend. Is FastAPI running?")
            time.sleep(3)

        except Exception as error:
            # Последний защитный слой.
            # Любая неожиданная ошибка не должна убивать worker навсегда.
            print(f"[Worker] Error: {error}")
            time.sleep(3)


if __name__ == "__main__":
    main()
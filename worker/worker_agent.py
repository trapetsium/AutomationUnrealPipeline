import time
from typing import Any, Dict

import requests


BACKEND_URL = "http://127.0.0.1:8010"
WORKER_NAME = "local-home-pc"


def log(job_id: str, message: str) -> None:
    print(message)

    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/logs",
        params={"message": message},
        timeout=10,
    )

    response.raise_for_status()


def update_status(job_id: str, status: str) -> None:
    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/status",
        params={"status": status},
        timeout=10,
    )

    response.raise_for_status()


def complete_job(job_id: str, result: Dict[str, Any]) -> None:
    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/complete",
        json=result,
        timeout=10,
    )

    response.raise_for_status()


def fail_job(job_id: str, result: Dict[str, Any]) -> None:
    response = requests.post(
        f"{BACKEND_URL}/jobs/{job_id}/fail",
        json=result,
        timeout=10,
    )

    response.raise_for_status()


def execute_ping_job(job: Dict[str, Any]) -> None:
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
        fail_job(
            job_id,
            {
                "worker": WORKER_NAME,
                "error": str(error),
            },
        )


def get_next_job() -> Dict[str, Any]:
    response = requests.get(
        f"{BACKEND_URL}/jobs/next",
        timeout=10,
    )

    response.raise_for_status()
    return response.json()


def main() -> None:
    print("[Worker] Started")
    print(f"[Worker] Name: {WORKER_NAME}")
    print(f"[Worker] Backend URL: {BACKEND_URL}")

    while True:
        try:
            job = get_next_job()

            if "id" not in job:
                print("[Worker] No queued jobs. Waiting...")
                time.sleep(2)
                continue

            job_id = job["id"]
            job_type = job["type"]

            print(f"[Worker] Claimed job: {job_id}")
            print(f"[Worker] Job type: {job_type}")

            if job_type == "ping":
                execute_ping_job(job)
            else:
                fail_job(
                    job_id,
                    {
                        "worker": WORKER_NAME,
                        "error": f"Unsupported job type: {job_type}",
                    },
                )

        except requests.exceptions.ConnectionError:
            print("[Worker] Cannot connect to backend. Is FastAPI running?")
            time.sleep(3)

        except Exception as error:
            print(f"[Worker] Error: {error}")
            time.sleep(3)


if __name__ == "__main__":
    main()
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


app = FastAPI(title="Unreal Automation Pipeline Backend")


class JobStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class JobCreateRequest(BaseModel):
    type: str
    engine_version: Optional[str] = None
    payload: Dict = Field(default_factory=dict)


class UnrealEngineLaunchRequest(BaseModel):
    engine_version: str = Field(
        ...,
        description="Configured Unreal Engine version, for example 4.27 or 5.5",
        examples=["5.5"],
    )

    args: List[str] = Field(
        default_factory=lambda: ["-help"],
        description="Additional command-line arguments passed to Unreal Editor",
    )


class Job(BaseModel):
    id: str
    type: str
    status: JobStatus
    engine_version: Optional[str] = None
    payload: Dict = Field(default_factory=dict)
    logs: List[str] = Field(default_factory=list)
    result: Optional[Dict] = None


jobs: Dict[str, Job] = {}


@app.get("/")
def root():
    return {
        "service": "Unreal Automation Pipeline Backend",
        "status": "running",
    }


@app.post("/jobs")
def create_job(request: JobCreateRequest):
    job_id = str(uuid4())

    job = Job(
        id=job_id,
        type=request.type,
        status=JobStatus.QUEUED,
        engine_version=request.engine_version,
        payload=request.payload,
    )

    jobs[job_id] = job
    return job

@app.post("/unreal/launch-engine")
def launch_unreal_engine(request: UnrealEngineLaunchRequest):
    """
    Создаёт pipeline job для запуска выбранной версии Unreal Engine.

    FastAPI НЕ запускает Unreal напрямую.

    Архитектура:
        API
          -> queued job
          -> local worker
          -> UnrealEditor-Cmd.exe / UE4Editor-Cmd.exe

    Это позволяет backend находиться даже на отдельном сервере,
    в то время как Unreal установлен только на worker-машине.
    """

    job_request = JobCreateRequest(
        type="launch_engine_version",
        engine_version=request.engine_version,
        payload={
            "args": request.args,
        },
    )

    return create_job(job_request)


@app.get("/jobs")
def list_jobs():
    return list(jobs.values())


@app.get("/jobs/next")
def get_next_job():
    for job in jobs.values():
        if job.status == JobStatus.QUEUED:
            job.status = JobStatus.CLAIMED
            return job

    return {"message": "no queued jobs"}


@app.post("/jobs/{job_id}/status")
def update_job_status(job_id: str, status: JobStatus):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = status
    return job


@app.post("/jobs/{job_id}/logs")
def append_job_log(job_id: str, message: str):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.logs.append(message)
    return job


@app.post("/jobs/{job_id}/complete")
def complete_job(job_id: str, result: Dict):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.SUCCESS
    job.result = result
    return job


@app.post("/jobs/{job_id}/fail")
def fail_job(job_id: str, result: Dict):
    job = jobs.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    job.status = JobStatus.FAILED
    job.result = result
    return job
"""Projects API — group resources into persistent projects (AB-417).

Mirrors Stripe Projects' state.json concept.

POST   /v1/projects                — create project
GET    /v1/projects                — list projects
GET    /v1/projects/{id}           — detail + resources
GET    /v1/projects/{id}/state     — state.json export (CI/CD friendly)
POST   /v1/projects/{id}/resources — add resource
DELETE /v1/projects/{id}/resources/{resource_id} — remove resource
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...database import get_db
from ...models import Project, ProjectResource
from ...schemas import (
    ProjectCreate,
    ProjectResponse,
    ProjectStateExport,
    ProjectResourceResponse,
    ProjectResourceCreate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    project = Project(name=body.name, agent_id=body.agent_id, description=body.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(
    agent_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(Project)
    if agent_id:
        q = q.filter(Project.agent_id == agent_id)
    return q.order_by(Project.created_at.desc()).all()


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: uuid.UUID, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.get("/projects/{project_id}/state", response_model=ProjectStateExport)
async def export_project_state(project_id: uuid.UUID, db: Session = Depends(get_db)):
    """Export project as state.json — machine-readable for CI/CD."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    resources = []
    for res in project.resources:
        resources.append({
            "id": str(res.id),
            "resource_type": res.resource_type,
            "resource_ref": res.resource_ref,
            "provider": res.provider,
            "status": res.status,
            "created_at": res.created_at.isoformat() if res.created_at else None,
        })

    return ProjectStateExport(
        project_id=str(project.id),
        name=project.name,
        agent_id=str(project.agent_id) if project.agent_id else None,
        description=project.description,
        resources=resources,
        created_at=project.created_at,
    )


@router.post("/projects/{project_id}/resources", response_model=ProjectResourceResponse, status_code=201)
async def add_project_resource(
    project_id: uuid.UUID,
    body: ProjectResourceCreate,
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    res = ProjectResource(
        project_id=project_id,
        resource_type=body.resource_type,
        resource_ref=body.resource_ref,
        provider=body.provider,
        scoped_token_id=body.scoped_token_id,
    )
    db.add(res)
    db.commit()
    db.refresh(res)
    return res


@router.delete("/projects/{project_id}/resources/{resource_id}", status_code=204)
async def remove_project_resource(
    project_id: uuid.UUID,
    resource_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    res = db.query(ProjectResource).filter(
        ProjectResource.id == resource_id,
        ProjectResource.project_id == project_id,
    ).first()
    if not res:
        raise HTTPException(status_code=404, detail="Resource not found")
    db.delete(res)
    db.commit()
    return None

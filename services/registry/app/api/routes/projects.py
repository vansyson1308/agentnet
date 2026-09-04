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
from ...auth import get_current_user
from ...authz import require_owned_agent
from ...models import Agent, User

logger = logging.getLogger(__name__)
router = APIRouter()


def _owned_project(db: Session, project_id: uuid.UUID, user: User) -> Project:
    """A project belongs to the owner of its agent. Non-owners get 404 so
    project ids cannot be enumerated."""
    project = (
        db.query(Project)
        .join(Agent, Agent.id == Project.agent_id)
        .filter(Project.id == project_id, Agent.user_id == user.id)
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/projects", response_model=ProjectResponse, status_code=201)
async def create_project(body: ProjectCreate, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if body.agent_id is None:
        raise HTTPException(status_code=422, detail="agent_id is required: projects belong to one of your agents")
    require_owned_agent(db, current_user, body.agent_id, detail="projects can only be created for agents you own")
    project = Project(name=body.name, agent_id=body.agent_id, description=body.description)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


@router.get("/projects", response_model=list[ProjectResponse])
async def list_projects(
    agent_id: uuid.UUID | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Project).join(Agent, Agent.id == Project.agent_id).filter(Agent.user_id == current_user.id)
    if agent_id:
        q = q.filter(Project.agent_id == agent_id)
    return q.order_by(Project.created_at.desc()).all()


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    return _owned_project(db, project_id, current_user)


@router.get("/projects/{project_id}/state", response_model=ProjectStateExport)
async def export_project_state(project_id: uuid.UUID, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    """Export project as state.json — machine-readable for CI/CD."""
    project = _owned_project(db, project_id, current_user)

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
    current_user: User = Depends(get_current_user),
):
    _owned_project(db, project_id, current_user)

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
    current_user: User = Depends(get_current_user),
):
    _owned_project(db, project_id, current_user)
    res = db.query(ProjectResource).filter(
        ProjectResource.id == resource_id,
        ProjectResource.project_id == project_id,
    ).first()
    if not res:
        raise HTTPException(status_code=404, detail="Resource not found")
    db.delete(res)
    db.commit()
    return None
